"""Fetch remaining quota / balance from Twilio and ElevenLabs.

Used by the post-call hook to log spend after every call, and by
``check_balance.py`` as a standalone CLI. All calls are best-effort —
network or auth errors return a row with an ``error`` field instead of
raising, so a flaky API never breaks call cleanup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from twilio.base.exceptions import TwilioRestException

from app.config import settings
from app.services.twilio_service import get_twilio_client

logger = logging.getLogger(__name__)


async def get_twilio_balance() -> dict[str, Any]:
    """Twilio account balance — single number in account currency."""
    try:
        # The Balance endpoint isn't on the v2010 client root; fetch directly.
        client = get_twilio_client()
        bal = client.api.v2010.accounts(settings.twilio_account_sid).balance.fetch()
        return {
            "balance": float(bal.balance),
            "currency": bal.currency,
            "account_sid": bal.account_sid,
        }
    except TwilioRestException as e:
        return {"error": f"Twilio {e.status}: {e.msg}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def get_elevenlabs_subscription() -> dict[str, Any]:
    """ElevenLabs subscription/usage. Conversational AI is billed in
    characters; the same character_count / character_limit pair tracks all
    TTS + convai consumption against the period quota."""
    if not settings.elevenlabs_api_key:
        return {"error": "ELEVENLABS_API_KEY not set"}
    url = "https://api.elevenlabs.io/v1/user/subscription"
    headers = {"xi-api-key": settings.elevenlabs_api_key}
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(url, headers=headers)
        if r.status_code >= 400:
            return {"error": f"ElevenLabs HTTP {r.status_code}: {r.text[:160]}"}
        data = r.json()
        used = int(data.get("character_count", 0))
        limit = int(data.get("character_limit", 0))
        remaining = max(0, limit - used)
        reset_unix = data.get("next_character_count_reset_unix")
        reset_iso = (
            datetime.fromtimestamp(reset_unix, tz=timezone.utc).isoformat()
            if reset_unix
            else None
        )
        return {
            "tier": data.get("tier"),
            "status": data.get("status"),
            "characters_used": used,
            "characters_limit": limit,
            "characters_remaining": remaining,
            "percent_used": round(100 * used / limit, 1) if limit else None,
            "resets_at": reset_iso,
        }
    except httpx.HTTPError as e:
        return {"error": f"ElevenLabs unreachable: {e}"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


async def fetch_all_balances() -> dict[str, dict[str, Any]]:
    """Fetch both vendors in one call. Used by the post-call hook + CLI."""
    return {
        "twilio": await get_twilio_balance(),
        "elevenlabs": await get_elevenlabs_subscription(),
    }


async def log_balances_after_call(call_sid: str | None = None) -> None:
    """Best-effort: fetch balances and write a single-line summary to the log.
    Called from the WebSocket bridge's finally block."""
    balances = await fetch_all_balances()
    tw = balances["twilio"]
    el = balances["elevenlabs"]

    tw_part = (
        f"Twilio ${tw['balance']:.2f} {tw['currency']}"
        if "balance" in tw
        else f"Twilio ?? ({tw.get('error')})"
    )
    if "characters_remaining" in el:
        el_part = (
            f"ElevenLabs {el['characters_used']:,}/{el['characters_limit']:,} chars "
            f"({el['percent_used']}% used, {el['characters_remaining']:,} left)"
        )
    else:
        el_part = f"ElevenLabs ?? ({el.get('error')})"

    suffix = f" (after callSid={call_sid})" if call_sid else ""
    logger.info(f"[BALANCE]{suffix} {tw_part} · {el_part}")
