"""Post-call severity classifier.

Takes a conversation transcript and the mother's onboarding context, returns a
4-level severity verdict plus structured signals — the shape that the Bloom
portal's `/api/calls/by-sid/{call_sid}/post-call` endpoint expects.

Strategy:
  - If `OPENAI_API_KEY` is set, ask the configured OpenAI model to classify
    with the PRD §5 rubric. Uses response_format=json_object so the output
    is reliably valid JSON.
  - If no key is available, fall back to a rule-based classifier scanning for
    red-flag phrases (e.g. "soaking a pad", "thoughts of harm"). Never fails
    closed — always returns at least L1 routine.

Severity rubric (PRD §5 Component 2):
  L1 routine     — no concerns surfaced, recovery on track.
  L2 monitor     — minor concerns to watch (mild pain, tiredness, short-term
                   feeding wobbles) — log, no escalation.
  L3 elevated    — clinically meaningful signal (mood red flags, possible
                   secondary PPH, infection signs, breakdown in support) —
                   alerts hospital. Never left with the AI.
  L4 crisis      — immediate danger (suicidal ideation, heavy bleeding +
                   instability, chest pain) — escalates to human responder.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)


RUBRIC = """\
You are an experienced postpartum nurse reviewing a phone-call transcript
between a new mother and an AI-assisted check-in agent. Classify the call into
exactly one severity level, drawn from this 4-level rubric:

L1 routine    - she is doing fine. No flags. Recovery on track.
L2 monitor    - small concerns worth noting (tiredness, mild pain, latch
                fatigue) but no clinical action required.
L3 elevated   - clinically meaningful signals - hospital must follow up:
                * mood red flags (intrusive thoughts, sustained low mood,
                  bonding difficulty, mentions of harm)
                * physical: heavy or returning bleeding, pad in <1h, severe
                  pain, signs of infection, returning hypertension symptoms
                * unable to feed / baby refusing feeds for an extended window
                * loss of support / isolation
L4 crisis     - immediate danger requiring a human responder NOW:
                * active suicidal ideation or thoughts of harming the baby
                * uncontrolled bleeding with dizziness/fainting
                * chest pain, severe breathing difficulty
                * eclampsia symptoms (seizure, sudden severe headache + vision)

Be conservative: when undecided between L2 and L3, choose L3 if any clinical
signal is present, even if the mother downplays it. The downside of an
unnecessary clinician call is small; missing a real concern is large.

Output STRICT JSON only, no prose, with this shape:
{
  "severity_level": 1 | 2 | 3 | 4,
  "summary": "one short paragraph for the clinician's dashboard",
  "signals": {
    "physical": [],
    "mood": [],
    "feeding": [],
    "baby": [],
    "support": []
  },
  "reason": "one sentence explaining the severity choice (only required for L3/L4)"
}
"""


# ── Rule-based fallback ──────────────────────────────────────────────────────


L4_PATTERNS = [
    r"\b(kill(ing)?|hurt(ing)?|harm(ing)?)\s+(myself|the\s+baby|him|her|them|baby)\b",
    r"\bthinking\s+about\s+(hurt|harm|kill)",
    r"\b(suicid(e|al)|end\s+(my\s+life|it\s+all))\b",
    r"\bchest\s+pain\b",
    r"\bcan'?t\s+breathe\b",
    r"\bseizure\b",
]

L3_PATTERNS = [
    r"\bsoak(ing|ed)?\s+(through\s+)?(a\s+)?pad\b",
    r"\bbleeding\s+(through|heavily|a\s+lot)\b",
    r"\b(clots?\s+(big|large|golf))",
    r"\bfever\b",
    r"\bwound\s+(red|swollen|smell|discharge|pus)\b",
    r"\b(severe|terrible)\s+(pain|headache)\b",
    r"\b(vision|blurred)\s+changes?\b",
    r"\b(don'?t|can'?t)\s+stop\s+crying\b",
    r"\bintrusive\s+thoughts?\b",
    r"\bcan'?t\s+(bond|connect)\s+with\s+(my\s+baby|him|her|them|baby)\b",
    r"\bcan'?t\s+(eat|sleep)\s+at\s+all\b",
]

L2_PATTERNS = [
    r"\btired\b",
    r"\bsore\b",
    r"\bhurts?\b",
    r"\blatch(ing)?\b",
    r"\bnipple\s+pain\b",
    r"\bengorged?\b",
    r"\bsleep(ing)?\s+poorly\b",
]


def _rule_based(transcript: str, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
    text = transcript.lower()
    physical: list[str] = []
    mood: list[str] = []
    feeding: list[str] = []
    severity = 1

    for pat in L4_PATTERNS:
        if re.search(pat, text):
            severity = max(severity, 4)
            mood.append("self-harm or crisis language detected")
            break
    for pat in L3_PATTERNS:
        m = re.search(pat, text)
        if m:
            severity = max(severity, 3)
            phrase = m.group(0)
            if any(k in phrase for k in ("pad", "blood", "bleeding", "clot", "wound")):
                physical.append(f"red-flag phrase: '{phrase}'")
            elif any(k in phrase for k in ("crying", "intrusive", "bond", "sleep", "eat")):
                mood.append(f"red-flag phrase: '{phrase}'")
            else:
                physical.append(f"red-flag phrase: '{phrase}'")
    if severity < 3:
        for pat in L2_PATTERNS:
            if re.search(pat, text):
                severity = max(severity, 2)
                physical.append(f"monitor: '{pat[2:-2]}'")
                break

    summary = {
        1: "Routine check-in. No red flags surfaced.",
        2: "Minor monitor signals — tiredness or mild physical discomfort.",
        3: "Clinical signal flagged — clinician follow-up recommended.",
        4: "Crisis-level signal — human responder needed immediately.",
    }[severity]

    return {
        "severity_level": severity,
        "summary": summary,
        "signals": {
            "physical": physical,
            "mood": mood,
            "feeding": feeding,
            "baby": [],
            "support": [],
        },
        "reason": summary if severity >= 3 else None,
    }


# ── OpenAI-powered classifier ────────────────────────────────────────────────


async def _openai_classify(
    transcript: str, ctx: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    if not settings.openai_api_key:
        return None
    try:
        from openai import AsyncOpenAI  # type: ignore[import-not-found]
    except ImportError:
        logger.warning("openai SDK not installed; skipping OpenAI severity pass")
        return None

    client = AsyncOpenAI(api_key=settings.openai_api_key)
    ctx_blob = json.dumps(ctx or {}, default=str)[:1500]
    user_msg = (
        f"Mother context: {ctx_blob}\n\nTranscript:\n{transcript[:6000]}"
    )
    # Reasoning models (gpt-5, o-series) consume the completion budget on hidden
    # reasoning tokens. Without `reasoning_effort=minimal`, default effort burns
    # ~600 tokens before producing visible output, returning an empty response.
    # Older chat models (gpt-4o family) do not accept this parameter.
    extra_params: dict[str, Any] = {}
    if any(settings.openai_model.startswith(p) for p in ("gpt-5", "o1", "o3", "o4")):
        extra_params["reasoning_effort"] = "minimal"
    try:
        resp = await client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": RUBRIC},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=2000,
            **extra_params,
        )
    except Exception as e:
        logger.warning(f"OpenAI severity pass failed: {e}")
        return None

    text = (resp.choices[0].message.content or "").strip()
    if not text:
        logger.warning("OpenAI severity output was empty")
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"OpenAI severity JSON parse failed: {e} — raw: {text[:200]}")
        return None
    sl = parsed.get("severity_level")
    if sl not in (1, 2, 3, 4):
        logger.warning(f"OpenAI returned invalid severity_level: {sl}")
        return None
    parsed.setdefault(
        "signals", {"physical": [], "mood": [], "feeding": [], "baby": [], "support": []}
    )
    return parsed


# ── Public entry point ───────────────────────────────────────────────────────


async def classify(
    transcript: str, ctx: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Always returns a populated PostCallIngest-shaped dict, even on error."""
    if transcript.strip():
        verdict = await _openai_classify(transcript, ctx)
        if verdict is not None:
            logger.info(
                f"Severity (OpenAI): L{verdict.get('severity_level')} — "
                f"{verdict.get('summary', '')[:80]}"
            )
            return verdict

    verdict = _rule_based(transcript, ctx)
    logger.info(
        f"Severity (rule): L{verdict['severity_level']} — {verdict['summary']}"
    )
    return verdict
