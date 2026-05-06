#!/usr/bin/env python3
"""Print remaining Twilio + ElevenLabs balances.

Run any time (with the venv activated):

    python check_balance.py

Reads the same .env the call server uses. Exits 0 on success, 1 if either
API was unreachable / auth-rejected.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from app.services.balance_check import fetch_all_balances


def _format_resets(iso: str | None) -> str:
    if not iso:
        return "—"
    dt = datetime.fromisoformat(iso)
    delta = dt - datetime.now(tz=timezone.utc)
    days = delta.days
    when = dt.astimezone().strftime("%Y-%m-%d %H:%M")
    return f"{when} (in {days}d)" if days >= 0 else when


def main() -> int:
    balances = asyncio.run(fetch_all_balances())
    tw = balances["twilio"]
    el = balances["elevenlabs"]
    rc = 0

    print()
    print("Twilio")
    if "balance" in tw:
        print(f"  Balance:    {tw['balance']:.2f} {tw['currency']}")
        print(f"  Account:    {tw['account_sid']}")
    else:
        print(f"  ERROR:      {tw.get('error')}")
        rc = 1

    print()
    print("ElevenLabs")
    if "characters_remaining" in el:
        print(f"  Tier:       {el.get('tier')}  (status: {el.get('status')})")
        print(
            f"  Characters: {el['characters_used']:,} / {el['characters_limit']:,}  "
            f"({el['percent_used']}% used)"
        )
        print(f"  Remaining:  {el['characters_remaining']:,}")
        print(f"  Resets at:  {_format_resets(el.get('resets_at'))}")
    else:
        print(f"  ERROR:      {el.get('error')}")
        rc = 1
    print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
