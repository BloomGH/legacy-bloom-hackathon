"""End-to-end test of the post-call pipeline, no Twilio, no voice.

What it exercises:
  1. Prompt rendering with a representative mother_context (matches the
     payload twiml.py:285 sends as conversation_config_override).
  2. Severity classification via OpenAI (matches twiml.py:443).
  3. Portal relay (matches twiml.py:446 — best-effort POST to backend).

What it does NOT exercise:
  - The live voice conversation with ElevenLabs (use /test-call/setup from the
    frontend for that — browser becomes the mic + TTS layer).
  - Twilio dialing, TwiML, or the Media Stream WebSocket.

Run from this directory:
    python3 test_e2e.py
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.prompts.postpartum import render_first_message, render_system_prompt
from app.services.post_call import send_post_call
from app.services.severity import classify

# Representative mother context — mirrors the dict built in
# backend/app/routers/mothers.py:_place_call_for_mother
SAMPLE_CTX = {
    "preferred_name": "Ama",
    "preferred_language": "english",
    "days_since_delivery": 7,
    "delivery_type": "csection_planned",
    "delivery_outcome": "live_birth",
    "parity": "primip",
    "plurality": "singleton",
    "gestational_age_weeks": 39,
    "feeding_plan": "exclusive_breastfeeding",
    "feeding_challenges_noted": [],
    "mental_health_history": ["anxiety"],
    "chronic_conditions": [],
    "allergies": None,
    "discharge_medications": [
        {"name": "paracetamol", "dose": "500mg", "frequency": "as needed", "duration_days": 7},
    ],
    "primary_support_name": "Kojo",
    "primary_support_relationship": "partner",
    "baby_name": "Akua",
    "baby_sex": "female",
    "baby_birth_weight_grams": 3200,
    "nicu_admission": False,
    "delivery_complications": [],
}

HOSPITAL = "Ridge Hospital"

# Three transcripts at different severity levels.
TRANSCRIPTS: dict[str, str] = {
    "L1": """\
Bloom: Hi Ama, it's Bloom from Ridge Hospital. How are you finding things this week?
Mother: Honestly, pretty good. Tired but Akua is feeding well and Kojo is here helping.
Bloom: Glad to hear it. Any pain or bleeding worth mentioning?
Mother: No, all that's settled down. Healing well.
Bloom: That gives me a good picture. I'll check in again next time. Take care, Ama.""",
    "L3": """\
Bloom: Hi Ama, how are you finding things this week?
Mother: Honestly, not great. I haven't slept properly in five days.
Bloom: That sounds exhausting. Can you tell me a bit more?
Mother: I just can't seem to feel anything for Akua. I keep imagining something bad happening to her.
Bloom: I appreciate you sharing that with me. Are these thoughts something you'd ever act on?
Mother: No, of course not, they just scare me.
Bloom: Thank you for being honest. Would you like me to let your hospital know about this so they can reach out?
Mother: Yes, please.
Bloom: I'll make sure they know — your care team at Ridge Hospital will be alerted after this call. Take care, Ama.""",
    "L4": """\
Bloom: Hi Ama, how are you doing today?
Mother: Bloom, my chest has been hurting for an hour and I can't catch my breath properly. I'm scared.
Bloom: That is serious. Stay on the line with me. Can I let your team at Ridge Hospital know right now so they can reach out?
Mother: Yes please call them.
Bloom: I'll make sure they know immediately. Try to sit down somewhere safe and stay with me.""",
}


def step(label: str) -> None:
    bar = "=" * 70
    print(f"\n{bar}\n{label}\n{bar}")


async def main() -> None:
    # ── Step 1: Prompt rendering ─────────────────────────────────────────
    step("STEP 1 — Prompt rendering")
    prompt = render_system_prompt(SAMPLE_CTX, HOSPITAL)
    first = render_first_message(SAMPLE_CTX, HOSPITAL)
    print(f"first_message → {first}")
    print(f"prompt length → {len(prompt)} chars")
    # Spot-check that DB context made it into the prompt
    expectations = [
        ("Ama", "preferred_name"),
        ("Kojo", "primary_support_name"),
        ("Akua", "baby_name"),
        ("anxiety", "mental_health_history"),
        ("C-section", "delivery_type"),
        ("paracetamol", "discharge_medications"),
        ("page the hospital", "safety guidance"),
        ("intrusive thoughts", "mood red flag from rubric"),
        ("When in doubt", "conservative-classification rule"),
    ]
    for needle, why in expectations:
        ok = needle in prompt
        print(f"  {'OK ' if ok else 'XX '} {needle!r:30s} ({why})")

    # ── Step 2: Severity classification ──────────────────────────────────
    step(f"STEP 2 — Severity classification (model={settings.openai_model!r})")
    verdicts: dict[str, dict] = {}
    for expected, transcript in TRANSCRIPTS.items():
        t0 = time.perf_counter()
        verdict = await classify(transcript, SAMPLE_CTX)
        elapsed = time.perf_counter() - t0
        verdicts[expected] = verdict
        actual = f"L{verdict.get('severity_level')}"
        match = "OK " if actual == expected else "XX "
        print(f"  {match} expected {expected} → got {actual} in {elapsed:.1f}s")
        print(f"      summary: {verdict.get('summary','')[:140]}")
        sigs = verdict.get("signals", {})
        for area in ("mood", "physical"):
            if sigs.get(area):
                print(f"      {area}: {sigs[area]}")
        if verdict.get("reason"):
            print(f"      reason: {verdict['reason'][:160]}")

    # ── Step 3: Portal relay (best-effort) ──────────────────────────────
    step(f"STEP 3 — Portal relay (PORTAL_BASE_URL={settings.portal_base_url!r})")
    print("  Note: send_post_call is best-effort — failures log but don't raise.")
    print("  Watch for 'Posted post-call ingest' (success) or 'Could not POST' (portal unreachable).")
    fake_run_id = int(time.time())
    for label, verdict in verdicts.items():
        sid = f"TEST_E2E_{fake_run_id}_{label}"
        print(f"\n  → POST {label} as call_sid={sid}")
        await send_post_call(sid, TRANSCRIPTS[label], verdict)


if __name__ == "__main__":
    asyncio.run(main())
