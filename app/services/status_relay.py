"""Forward Twilio call status events to the Bloom portal.

Twilio POSTs to /twiml/status whenever a call's CallStatus transitions:
queued → initiated → ringing → in-progress → completed (or busy / no-answer
/ failed / canceled). We translate the wire fields into a structured update
and POST it to the portal so the CallLog in the database tracks the live
state — and the frontend can show a live progression.

Best-effort: the portal being down does NOT prevent Twilio from completing
the call. Errors are logged, never raised.
"""

from __future__ import annotations

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


# Map Twilio's status strings to our internal enum (in_progress, no_answer
# use underscores; Twilio sends hyphens). Anything else we pass through.
_STATUS_MAP = {
    "queued": "queued",
    "initiated": "queued",  # Twilio's initiated event arrives as CallStatus=queued
    "ringing": "ringing",
    "in-progress": "in_progress",
    "completed": "completed",
    "busy": "busy",
    "no-answer": "no_answer",
    "failed": "failed",
    "canceled": "canceled",
}


# Friendly drop-reason text for terminal non-completed statuses. Used when
# Twilio doesn't supply an explicit ErrorMessage / SipResponseCode.
_TERMINAL_REASONS = {
    "no_answer": "She didn't pick up.",
    "busy": "Her line was busy.",
    "canceled": "Call was canceled before she answered.",
    "failed": "Call failed before connecting.",
}


def normalize_status(twilio_status: str) -> str:
    return _STATUS_MAP.get((twilio_status or "").strip().lower(), "queued")


def synthesize_failure_reason(
    status: str,
    error_code: str | None,
    error_message: str | None,
    sip_response_code: str | None,
    answered_by: str | None,
) -> str | None:
    """Compose a human-readable drop reason for terminal non-completed states.
    Returns None for in-flight or successfully-completed calls.
    """
    if status == "completed":
        return None
    if status in ("queued", "ringing", "in_progress"):
        return None

    parts: list[str] = [_TERMINAL_REASONS.get(status, status.replace("_", " ").title())]
    if error_code:
        parts.append(f"Twilio error {error_code}")
    if error_message:
        parts.append(error_message)
    if sip_response_code and sip_response_code not in {"200", "0"}:
        parts.append(f"SIP {sip_response_code}")
    if answered_by and answered_by.startswith("machine"):
        parts.append("answered by voicemail")
    return " · ".join(parts)


async def relay_status_to_portal(
    call_sid: str,
    status: str,
    *,
    duration_seconds: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    sip_response_code: str | None = None,
    answered_by: str | None = None,
    sequence_number: int | None = None,
    timestamp: str | None = None,
) -> None:
    if not call_sid:
        logger.warning("relay_status_to_portal skipped: no call_sid")
        return

    url = (
        f"{settings.portal_base_url.rstrip('/')}"
        f"/api/calls/by-sid/{call_sid}/status"
    )
    payload = {
        "status": status,
        "duration_seconds": duration_seconds,
        "error_code": error_code,
        "error_message": error_message,
        "sip_response_code": sip_response_code,
        "answered_by": answered_by,
        "sequence_number": sequence_number,
        "timestamp": timestamp,
        "failure_reason": synthesize_failure_reason(
            status, error_code, error_message, sip_response_code, answered_by
        ),
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.post(url, json=payload)
        if r.status_code >= 400:
            logger.warning(
                f"Portal status relay returned {r.status_code} for "
                f"sid={call_sid} status={status}: {r.text[:200]}"
            )
    except httpx.HTTPError as e:
        logger.warning(f"Could not relay status to {url}: {e}")
