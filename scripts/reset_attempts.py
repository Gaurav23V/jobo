#!/usr/bin/env python3
"""Reset Module 4 attempt counters so retryable jobs aren't dropped.

Why this exists
---------------
A job is permanently dropped (`should_apply=0`) after
`module4.constants.max_attempts_per_job()` failed apply attempts. That rule is
correct when the failures are the job's fault (broken portal, login wall we
cannot pass). It is WRONG when the failures were OUR fault - a bug or a
missing capability on our side - because the job would have succeeded if the
pipeline had been working.

After the 2026-09-02 run, 15 of 28 pending jobs sat at attempts=2. One more
run before the upload fix landed would have pushed them to 3 and deleted a
month of collected, triaged, resume-generated jobs for reasons that were our
bugs, not theirs.

Use this after shipping a fix that addresses a known failure class, to give
the affected jobs their attempts back.

Safety
------
- Dry-run by default; needs --apply to write.
- Only touches jobs still in the queue (`should_apply=1 AND applied=0`), so it
  can never resurrect a job that was deliberately dropped or already applied.
- `--only-our-fault` (default) restricts the reset to failures whose recorded
  reason matches a known pipeline-side cause (upload failures, budget/timeout
  exhaustion, OTP walls). Genuinely dead ends - captcha walls, unpassable
  logins, structurally broken portals - keep their attempts, because retrying
  those is just burning time.
- Prints exactly what it will change, per job, before doing it.

Run:
  uv run python scripts/reset_attempts.py                 # preview
  uv run python scripts/reset_attempts.py --apply         # do it
  uv run python scripts/reset_attempts.py --requeue 1266,1489          # preview
  uv run python scripts/reset_attempts.py --requeue 1266,1489 --apply  # do it

--requeue: jobs already DROPPED (should_apply=0) by max attempts or by
module4/blockers.py, named one by one after a fix lands for their failure. The
pending-queue mode above never sees them. Puts should_apply back to 1 and the
attempts to --to; module4_last_status is kept, so the retry still gets its
retry notice (and a parked Google Form stays parked). Refuses anything that
was sent, closed, skipped by the user or blocklisted. Back up data/jobs.db first.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DB = REPO / "data" / "jobs.db"

# Employers we will NEVER apply to. Resetting their attempts is pure waste:
# module4 refuses them before an agent is ever spawned, so a reset just parks
# dead rows in the pending queue forever. Checked BEFORE any pattern matching,
# because their failure text ("account already exists, password unknown")
# looks exactly like a normal retryable account wall.
sys.path.insert(0, str(REPO))
try:
    from policy import employer_blocklist as _blocklist
except Exception:  # pragma: no cover - keep the script usable standalone
    _blocklist = None

# Failure reasons that were OUR limitation, not the employer's portal being
# genuinely impassable. These are the ones worth handing attempts back for.
OUR_FAULT_PATTERNS = [
    # File-upload machinery (fixed by the deterministic upload handoff)
    r"upload",
    r"file input",
    r"setfileinputfiles",
    r"backendnodeid",
    r"nodeid",
    r"objectid",
    r"resume/cv",
    r"dropzone",
    # Budget/time exhaustion (retry budget was inverted; also relieved by
    # moving uploads out of the agent's turn budget)
    r"tool-call",
    r"tool call",
    r"iteration",
    r"turns",
    r"budget",
    r"timed out",
    r"ran out",
    # Email verification codes (fixed by the OTP handoff)
    r"\botp\b",
    r"verification code",
    r"confirmation code",
    r"email verification",
    # Our browser infrastructure broke - never the job's fault. Added
    # 2026-09-03 after a dead Chromium charged 36 jobs a real attempt in ten
    # minutes; the runner now avoids counting these, and this pattern hands
    # back the ones that were already charged.
    r"cdp connection",
    r"connection refused",
    r"browser backend",
    r"browser tooling",
    r"os error 111",
    r"no chrome instance",
    r"browser tool backend",
    r"tab is not responding",
    r"tab is unresponsive",
    r"browser session is unreachable",
    # Added 2026-09-11: job611 (Apple) reported "Browser session unresponsive
    # at CDP layer" and matched NOTHING here or in runner._is_infra_failure,
    # so a browser fault was charged to the job. Keep these two lists in step -
    # the runner decides whether to charge an attempt, this hands back the ones
    # that were charged before the wording was known.
    r"browser session unresponsive",
    r"browser (session |instance )?became unresponsive",
    r"browser is unresponsive",
    r"unresponsive at cdp",
    r"cdp layer",
    r"browser (session )?crashed",
    r"lost connection to the browser",
    # Added 2026-09-15: model-provider outage, not a site problem. Must stay in
    # step with runner._INFRA_FAILURE_MARKERS - the runner stops charging these
    # going forward, this hands back the 18 attempts already charged on the
    # 2026-09-15 run.
    r"can'?t reach the model provider",
    r"cannot reach the model provider",
    r"api call failed after",
    r"model provider",
    r"you may be offline",
    r"agent produced no verdict",
    r"agent produced no output",
    r"browser disconnected",
    # Email-gated account walls. These USED to be dead ends ("an account
    # already exists and the password is unknown"), and they matched the OTP
    # patterns above only by accident. Since the NEED_PASSWORD_RESET handoff
    # shipped (2026-09-05) the pipeline can read the reset mail and set a known
    # password, so they are genuinely retryable now - listed explicitly rather
    # than relying on an incidental match.
    r"account already exists",
    r"already an account",
    r"account exists",
    r"password is unknown",
    r"password unknown",
    r"requires sign-?in",
    r"sign-?in wall",
    r"account creation silently",
    r"wrong email address or password",
]

# Reasons to LEAVE ALONE - retrying these wastes a slot.
# NOTE: keep these patterns SPECIFIC. An early version used a bare
# `phone|sms`, which matched job526's "name/email/phone/location filled" - a
# list of successfully filled fields - and wrongly classified a plain
# out-of-budget failure as an impassable SMS wall. Match the blocking
# CONDITION, never a word that could appear incidentally.
GENUINELY_BLOCKED_PATTERNS = [
    r"captcha",
    r"recaptcha",
    r"turnstile",
    r"flagged as (possible )?spam",
    r"403 forbidden",
    r"err_http2_protocol_error",
    r"(no longer|not currently) accepting",
    # SMS/phone verification specifically - we can read email, not texts.
    # "via email/SMS" is deliberately NOT matched here: if email is one of the
    # offered channels, the OTP handoff can pass it, so it is retryable.
    r"(sms|text message)[^.]{0,30}(otp|code|verification|verify)",
    r"(otp|code|verification)[^.]{0,40}\b(via|to|by)\s+(sms|text|mobile)\b(?!/)",
    r"\bsms\s+(only|required)\b",
    # OAuth-only sign-in walls. There are no Google/Microsoft/Apple/Facebook
    # credentials available to the agent, so no amount of retrying helps -
    # Laurelton, Microsoft (Eightfold) and Apple each burned a full turn budget
    # discovering this on the 2026-09-05 run. Match the CONDITION (only/no
    # alternative), never a bare provider name: plenty of portals offer OAuth
    # alongside a perfectly usable email/password form.
    r"only offers? oauth",
    r"only offers? (google|microsoft|apple|facebook|linkedin)",
    r"requires? \"?continue with (google|microsoft|apple)\"? .{0,40}no alternative",
    r"no email/?password (option|signup|sign-?up|login|form)",
    r"(oauth|social) (sign-?in|sign-?up|login) (only|required)",
    r"no (email|password)-based (signup|sign-?up|account creation)",
]


def classify(status: str, company: str = "", url: str = "") -> str:
    """Return 'blocklist', 'blocked', 'ours', or 'unknown' for a failure.

    'blocklist' takes precedence over everything: a blocklisted employer is
    never retried regardless of why it failed, so handing it attempts back
    only clutters the queue.
    """
    if _blocklist is not None and (
        _blocklist.is_blocked(company, url) or _blocklist.destination_block_reason(status)
    ):
        return "blocklist"
    s = (status or "").lower()
    # A LinkedIn sign-out is the browser's state, not the job's (2026-10-03:
    # 1140, 1173, 1220 lost their last try to it). Checked before the
    # genuinely-blocked list, whose SMS/OAuth patterns the agent's wording
    # for this case can match.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from module4.linkedin_session import is_signed_out_detail

        if is_signed_out_detail(s):
            return "ours"
    except Exception:
        pass
    for pat in GENUINELY_BLOCKED_PATTERNS:
        if re.search(pat, s):
            return "blocked"
    for pat in OUR_FAULT_PATTERNS:
        if re.search(pat, s):
            return "ours"
    # Shape match as a backstop, mirroring runner._is_infra_failure. The two
    # must agree: the runner decides whether to CHARGE an attempt, this decides
    # whether to HAND ONE BACK, and a wording only one of them knows leaves the
    # job quietly poorer. Literal lists lose here - "Browser/CDP session
    # unresponsive" defeated a literal added one run earlier, purely because of
    # the slash.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from module4.runner import _is_infra_failure

        if _is_infra_failure(s):
            return "ours"
    except Exception:
        pass
    return "unknown"


def requeue_refusal(applied, closed_at, outcome, status, company, url) -> str | None:
    """Why a named job must NOT be put back in the queue, else None."""
    if applied or outcome == "submitted":
        return "already submitted"
    if closed_at or outcome == "closed":
        return "listing closed"
    head = (status or "").split(":", 1)[0]
    if head in ("skipped", "already_applied"):
        return "skipped by the user" if head == "skipped" else "ATS says already applied"
    if classify(status or "", company or "", url or "") == "blocklist":
        return "blocklisted employer or destination"
    return None


def requeue(conn: sqlite3.Connection, ids_arg: str, *, to: int, apply: bool) -> int:
    try:
        ids = sorted({int(x) for x in ids_arg.replace(" ", "").split(",") if x})
    except ValueError:
        print(f"--requeue wants comma-separated job ids, got {ids_arg!r}")
        return 2
    marks = ",".join("?" * len(ids))
    rows = {
        r[0]: r[1:]
        for r in conn.execute(
            "select id, company_name, applied, should_apply, module4_attempts, "
            "module4_closed_at, module4_outcome, module4_last_status, job_url "
            f"from jobs where id in ({marks})",
            ids,
        )
    }
    go, refused = [], []
    for jid in ids:
        if jid not in rows:
            refused.append((jid, "", "no such job"))
            continue
        company, applied, should_apply, attempts, closed_at, outcome, status, url = rows[jid]
        why = requeue_refusal(applied, closed_at, outcome, status, company, url)
        if why:
            refused.append((jid, company, why))
        else:
            go.append((jid, company, should_apply, attempts, status))
    print(f"--- WILL REQUEUE ({len(go)}): should_apply=1, attempts={to} ---")
    for jid, company, should_apply, attempts, status in go:
        reason = (status or "")[:58].replace("\n", " ")
        print(f"  job{jid:<4} sa={should_apply} attempts={attempts} {(company or '')[:20]:20} {reason}")
    if refused:
        print(f"\n--- REFUSED ({len(refused)}) ---")
        for jid, company, why in refused:
            print(f"  job{jid:<4} {(company or '')[:20]:20} {why}")
    if not apply:
        print(f"\nDRY RUN. Re-run with --apply to requeue {len(go)} job(s).")
        return 0
    if not go:
        print("\nNothing to do.")
        return 0
    conn.executemany(
        "update jobs set should_apply = 1, module4_attempts = ? where id = ?",
        [(to, jid) for jid, *_ in go],
    )
    conn.commit()
    done = [jid for jid, *_ in go]
    check = conn.execute(
        f"select count(*) from jobs where id in ({','.join('?' * len(done))}) "
        "and should_apply = 1 and module4_attempts = ?",
        (*done, to),
    ).fetchone()[0]
    print(f"\nRequeued {check}/{len(done)} job(s).")
    return 0 if check == len(done) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="actually write the reset")
    ap.add_argument(
        "--all",
        action="store_true",
        help="reset every pending job, not just our-fault failures",
    )
    ap.add_argument(
        "--to",
        type=int,
        default=0,
        help="attempt count to reset to (default 0)",
    )
    ap.add_argument(
        "--requeue",
        metavar="IDS",
        help="comma-separated ids of DROPPED jobs to put back in the queue",
    )
    ap.add_argument("--db", type=Path, default=DB, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"no database at {args.db}")
        return 1

    conn = sqlite3.connect(str(args.db))
    if args.requeue:
        return requeue(conn, args.requeue, to=args.to, apply=args.apply)
    rows = conn.execute(
        "select id, company_name, job_title, module4_attempts, module4_last_status, "
        "job_url from jobs where should_apply=1 and applied=0 and module4_attempts > ? "
        "order by module4_attempts desc, id",
        (args.to,),
    ).fetchall()

    if not rows:
        print("Nothing to reset: no pending jobs above the target attempt count.")
        return 0

    to_reset, to_keep = [], []
    for jid, company, title, attempts, status, url in rows:
        # The blocklist check is NOT skippable with --all: --all means "be
        # generous about failure reasons", never "apply to companies the user ruled
        # out".
        kind = classify(status or "", company or "", url or "")
        if kind != "blocklist" and args.all:
            kind = "ours"
        if kind in ("blocked", "blocklist"):
            to_keep.append((jid, company, attempts, status, kind))
        else:
            to_reset.append((jid, company, attempts, status, kind))

    print(f"Pending jobs above attempts={args.to}: {len(rows)}\n")
    print(f"--- WILL RESET to attempts={args.to} ({len(to_reset)}) ---")
    for jid, company, attempts, status, kind in to_reset:
        reason = (status or "")[:58].replace("\n", " ")
        print(f"  job{jid:<4} attempts={attempts} [{kind:7}] {(company or '')[:20]:20} {reason}")

    if to_keep:
        print(
            f"\n--- LEAVING ALONE ({len(to_keep)}) - blocklisted employers and "
            f"genuinely blocked jobs; retry wastes a slot ---"
        )
        for jid, company, attempts, status, kind in to_keep:
            reason = (status or "")[:58].replace("\n", " ")
            print(f"  job{jid:<4} attempts={attempts} [{kind:7}] {(company or '')[:20]:20} {reason}")

    if not args.apply:
        print(f"\nDRY RUN. Re-run with --apply to reset {len(to_reset)} job(s).")
        return 0

    ids = [r[0] for r in to_reset]
    if not ids:
        print("\nNothing to do.")
        return 0
    conn.executemany(
        "update jobs set module4_attempts = ? where id = ?",
        [(args.to, jid) for jid in ids],
    )
    conn.commit()

    # Verify what actually landed rather than trusting the update.
    check = conn.execute(
        f"select count(*) from jobs where id in ({','.join('?' * len(ids))}) "
        "and module4_attempts = ?",
        (*ids, args.to),
    ).fetchone()[0]
    print(f"\nReset {check}/{len(ids)} job(s) to attempts={args.to}.")
    remaining = conn.execute(
        "select module4_attempts, count(*) from jobs "
        "where should_apply=1 and applied=0 group by 1 order by 1"
    ).fetchall()
    print("Pending queue now:")
    for attempts, count in remaining:
        print(f"  attempts={attempts}: {count}")
    return 0 if check == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
