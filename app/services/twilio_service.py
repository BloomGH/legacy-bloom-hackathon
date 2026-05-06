"""
Twilio service — initiates outbound calls and configures inbound webhooks.
"""

import logging
from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client

from app.config import settings

logger = logging.getLogger(__name__)

_client: Client | None = None

# Twilio status events we want pushed back to /twiml/status.
# initiated → ringing → answered → completed gives us the full lifecycle.
STATUS_CALLBACK_EVENTS = ["initiated", "ringing", "answered", "completed"]


class TwilioCallError(RuntimeError):
    """Raised when Twilio rejects an outbound call request.

    Carries the structured Twilio error details so callers (and the frontend)
    can render a meaningful drop reason instead of a generic 500.
    """

    def __init__(self, code: int | None, message: str, more_info: str | None, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.more_info = more_info
        self.status = status


def get_twilio_client() -> Client:
    global _client
    if _client is None:
        _client = Client(settings.twilio_account_sid, settings.twilio_auth_token)
    return _client


def initiate_call(to: str) -> dict:
    """Trigger an outbound Twilio call to ``to``.

    Registers a statusCallback so Twilio pushes lifecycle events
    (initiated/ringing/answered/completed) back to /twiml/status — that's how
    we know whether the recipient picked up, was busy, didn't answer, or the
    call failed for a network/geo/auth reason.
    """
    client = get_twilio_client()
    logger.info(
        f"Placing call to={to} from={settings.twilio_from_number} "
        f"answer_url={settings.twiml_answer_url} status_url={settings.twiml_status_url}"
    )
    try:
        call = client.calls.create(
            to=to,
            from_=settings.twilio_from_number,
            url=settings.twiml_answer_url,  # TwiML on answer
            method="POST",
            status_callback=settings.twiml_status_url,
            status_callback_event=STATUS_CALLBACK_EVENTS,
            status_callback_method="POST",
        )
    except TwilioRestException as e:
        logger.warning(
            f"Twilio rejected call to={to}: code={e.code} status={e.status} "
            f"message={e.msg!r} more_info={e.uri!r}"
        )
        raise TwilioCallError(
            code=e.code,
            message=e.msg or str(e),
            more_info=getattr(e, "uri", None) or f"https://www.twilio.com/docs/errors/{e.code}",
            status=e.status or 400,
        ) from e

    logger.info(f"Initiated call sid={call.sid} status={call.status} to={to}")
    return {"call_sid": call.sid, "status": call.status, "to": to}


def configure_inbound_webhook(answer_url: str) -> dict:
    """Auto-configure the Twilio phone number so inbound calls hit /twiml/answer.
    Idempotent — safe on every startup.
    """
    client = get_twilio_client()

    numbers = client.incoming_phone_numbers.list(
        phone_number=settings.twilio_from_number, limit=1
    )
    if not numbers:
        raise RuntimeError(
            f"No Twilio phone number found matching {settings.twilio_from_number}. "
            "Check your TWILIO_FROM_NUMBER in .env."
        )

    number = numbers[0]
    number.update(
        voice_url=answer_url,
        voice_method="POST",
    )
    logger.info(
        f"Twilio inbound webhook configured: {settings.twilio_from_number} → {answer_url}"
    )
    return {"phone_number_sid": number.sid, "voice_url": answer_url}
