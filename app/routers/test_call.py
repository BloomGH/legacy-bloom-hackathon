"""Browser-based stress-test endpoint — bypass Twilio, talk to Bloom directly.

The frontend hits POST /test-call/setup with a (optional) mother_context, and
this returns a one-shot signed WebSocket URL plus the rendered system prompt
and first message. The browser then opens that URL with the ElevenLabs
Conversational AI JS client, capturing mic + playing TTS via WebAudio.

No Twilio, no phone number, no SIP — just you and the agent.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.prompts.postpartum import render_first_message, render_system_prompt
from app.routers.calls import MotherContext  # reuse the same shape

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/test-call", tags=["test-call"])

ELEVENLABS_BASE_URL = "https://api.elevenlabs.io/v1"


class TestCallSetupRequest(BaseModel):
    """Optional mother context — when present, the agent gets the same
    per-mother personalization as a real Twilio call."""

    hospital_name: str | None = None
    mother_context: MotherContext | None = None


class TestCallSetupResponse(BaseModel):
    signed_url: str
    agent_id: str
    system_prompt: str
    first_message: str
    dynamic_variables: dict


@router.post("/setup", response_model=TestCallSetupResponse)
async def setup_test_call(body: TestCallSetupRequest) -> TestCallSetupResponse:
    if not settings.elevenlabs_agent_id:
        raise HTTPException(
            status_code=503,
            detail="ELEVENLABS_AGENT_ID is not configured on the server.",
        )

    agent_id = settings.elevenlabs_agent_id
    hospital_name = body.hospital_name or "your clinic"
    ctx_dict = body.mother_context.model_dump() if body.mother_context else None

    rendered_prompt = render_system_prompt(ctx_dict, hospital_name)
    rendered_first_message = render_first_message(ctx_dict, hospital_name)

    # Get a one-shot signed URL the browser can connect to. This is how
    # ElevenLabs lets you connect from the client side without exposing the
    # API key.
    url = f"{ELEVENLABS_BASE_URL}/convai/conversation/get-signed-url"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                url,
                params={"agent_id": agent_id},
                headers={"xi-api-key": settings.elevenlabs_api_key},
            )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"ElevenLabs unreachable: {e}") from e

    if r.status_code >= 400:
        raise HTTPException(
            r.status_code,
            f"ElevenLabs signed-url failed: {r.text[:200]}",
        )

    data = r.json()
    signed_url = data.get("signed_url") or data.get("signedUrl")
    if not signed_url:
        raise HTTPException(502, f"ElevenLabs returned no signed_url: {data}")

    preferred_name = (ctx_dict or {}).get("preferred_name") or "there"
    days_since = str((ctx_dict or {}).get("days_since_delivery") or 0)

    logger.info(
        f"[TEST-CALL] signed URL issued for agent={agent_id} "
        f"patient_name={preferred_name!r} has_context={ctx_dict is not None}"
    )

    return TestCallSetupResponse(
        signed_url=signed_url,
        agent_id=agent_id,
        system_prompt=rendered_prompt,
        first_message=rendered_first_message,
        dynamic_variables={
            "patient_name": preferred_name,
            "days_since_delivery": days_since,
            "hospital_name": hospital_name,
        },
    )
