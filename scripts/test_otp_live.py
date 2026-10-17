#!/usr/bin/env python3
"""LIVE end-to-end test of the OTP lookup path against the real Gmail account.

Unlike scripts/test_otp_extraction.py (pure string parsing, no network), this
exercises the whole chain: token refresh -> Gmail search -> message fetch ->
MIME/base64 decode -> code extraction -> recency filtering.

It does that WITHOUT depending on some third party to send us a code: it
inserts a synthetic verification email directly into the mailbox with
users.messages.insert (permitted by the gmail.modify scope the collector
already holds), then asks otp_lookup to find it exactly as it would during a
real apply run. The test message is deleted again at the end.

Verifies:
  1. A freshly inserted code is found, and the code matches what was inserted.
  2. Sender filtering works (a lookup scoped to a different domain misses it).
  3. The recency window is enforced (minutes=0 finds nothing).
  4. Cleanup: the test message is removed from the mailbox.

Run: uv run python scripts/test_otp_live.py
"""

from __future__ import annotations

import base64
import random
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from module4 import otp_lookup  # noqa: E402

TEST_SENDER_DOMAIN = "jobo-otp-selftest.example.com"


def _insert_test_message(service, code: str) -> str:
    body = (
        "Hello Alex,\n\n"
        "Please use the following verification code to continue creating your "
        "candidate account:\n\n"
        f"    {code}\n\n"
        "This code expires in 15 minutes. Copyright 2026 Jobo Selftest.\n"
    )
    msg = MIMEText(body)
    msg["To"] = "alex.example@example.com"
    msg["From"] = f"Careers Verification <no-reply@{TEST_SENDER_DOMAIN}>"
    msg["Subject"] = "Verify your email address"
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    inserted = (
        service.users()
        .messages()
        .insert(userId="me", body={"raw": raw, "labelIds": ["INBOX", "UNREAD"]})
        .execute()
    )
    return inserted["id"]


def _delete_test_message(service, msg_id: str) -> bool:
    try:
        service.users().messages().delete(userId="me", id=msg_id).execute()
        return True
    except Exception:
        # delete needs full mail scope on some setups; trashing is enough to
        # keep the inbox clean and is covered by gmail.modify.
        try:
            service.users().messages().trash(userId="me", id=msg_id).execute()
            return True
        except Exception as e:
            print(f"  WARN: could not remove test message {msg_id}: {e}")
            return False


def main() -> int:
    failures: list[str] = []
    code = f"{random.randint(100000, 999999)}"
    print(f"Live OTP end-to-end test (synthetic code {code})\n")

    print("[0] Authenticating with the collector's existing Gmail token...")
    try:
        service = otp_lookup._gmail_service()
        print("    OK: token valid, Gmail service built.")
    except Exception as e:
        print(f"    FAIL: {e}")
        return 1

    print("[1] Inserting a synthetic verification email into the inbox...")
    try:
        msg_id = _insert_test_message(service, code)
        print(f"    OK: inserted message id={msg_id}")
    except Exception as e:
        print(f"    FAIL: could not insert test message: {e}")
        return 1

    try:
        # Gmail's search index needs a moment before the message is findable.
        print("[2] Looking the code up via otp_lookup.wait_for_code()...")
        result = otp_lookup.wait_for_code(
            sender=TEST_SENDER_DOMAIN, minutes=10, timeout_sec=60, poll_interval_sec=4
        )
        if not result.found:
            failures.append(f"lookup did not find the code: {result.error}")
            print(f"    FAIL: {result.error}  (query={result.searched_query})")
        elif result.code != code:
            failures.append(f"wrong code: expected {code}, got {result.code}")
            print(f"    FAIL: expected {code}, got {result.code}")
        else:
            print(f"    OK: found {result.code} from {result.sender!r}")
            print(f"        subject={result.subject!r} received={result.received_at}")

        print("[3] Sender filter must NOT match an unrelated domain...")
        other = otp_lookup.find_code(sender="definitely-not-this-domain.invalid", minutes=10)
        if other.found:
            failures.append(f"sender filter leaked a code: {other.code} from {other.sender}")
            print(f"    FAIL: got {other.code} from {other.sender!r}")
        else:
            print("    OK: no code returned for an unrelated sender.")

        print("[4] Recency window must exclude the message at minutes=0...")
        stale = otp_lookup.find_code(sender=TEST_SENDER_DOMAIN, minutes=0)
        if stale.found:
            failures.append("recency filter did not exclude the message at minutes=0")
            print(f"    FAIL: got {stale.code} despite a 0-minute window")
        else:
            print("    OK: 0-minute window returned nothing.")

    finally:
        print("[5] Cleaning up the test message...")
        if _delete_test_message(service, msg_id):
            print("    OK: test message removed.")
            time.sleep(1)
            leftover = otp_lookup.find_code(sender=TEST_SENDER_DOMAIN, minutes=10)
            if leftover.found:
                failures.append("test message still findable after cleanup")
                print("    FAIL: message still findable after cleanup.")
            else:
                print("    OK: verified gone.")

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ALL LIVE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
