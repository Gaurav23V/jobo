#!/usr/bin/env python3
"""Fetch a one-time verification code from the user's Gmail. Used by the apply agent.

This is the ONLY Gmail entry point the apply agent is given (the
agent-approval-gates pattern): it can read a fresh verification code and
nothing else. It cannot list mail, read bodies, send, delete, or reach any
message older than the `--minutes` window. Even if the agent were steered by a
malicious page into trying, this interface exposes no verb for it.

Consumers:
  1. The Module 4 apply agent, via a single whitelisted terminal command.
  2. Humans debugging the OTP path (`--sender northwind.com --minutes 30 -v`).

Exit codes: 0 = code found (printed to stdout), 2 = no code, 3 = auth/API error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import otp_lookup  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--sender",
        default="",
        help="Filter by sender domain or address, e.g. northwind.com. Strongly recommended.",
    )
    ap.add_argument(
        "--minutes",
        type=int,
        default=10,
        help="Only consider mail received in the last N minutes (default 10).",
    )
    ap.add_argument("--subject-hint", default="", help="Optional subject substring filter.")
    ap.add_argument(
        "--wait",
        type=int,
        default=0,
        help="Poll for up to N seconds for the code to arrive (default 0 = single lookup).",
    )
    ap.add_argument("--json", action="store_true", help="Emit the full result as JSON.")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.wait > 0:
        result = otp_lookup.wait_for_code(
            sender=args.sender,
            minutes=args.minutes,
            subject_hint=args.subject_hint,
            timeout_sec=args.wait,
        )
    else:
        result = otp_lookup.find_code(
            sender=args.sender,
            minutes=args.minutes,
            subject_hint=args.subject_hint,
        )

    if args.json:
        print(
            json.dumps(
                {
                    "code": result.code,
                    "found": result.found,
                    "sender": result.sender,
                    "subject": result.subject,
                    "received_at": result.received_at,
                    "query": result.searched_query,
                    "candidates_scanned": result.candidates_scanned,
                    "error": result.error,
                },
                indent=2,
            )
        )
    elif result.found:
        print(result.code)
    else:
        print(f"NO_CODE: {result.error}", file=sys.stderr)

    if result.found:
        return 0
    if "credential" in result.error.lower() or "token" in result.error.lower():
        return 3
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
