"""The closed-set apply outcome, derived from (applied, module4_last_status).

`module4_last_status` is free text the model/agent writes - dozens of distinct
`closed:...`/`stuck:...` sentences, ungroupable without string matching (see
GHO-2). `module4_outcome` is the canonical bucket computed from it; the free
text stays in `module4_last_status` as the human-readable reason.

Classification was derived from the real distribution of all 1668 rows in
data/jobs.db (2026-10-14), not guessed:
  - `applied=True` is 342 rows and maps 1:1 onto "a real application went
    out" - it covers the 338 rows whose status text is literally "submitted",
    plus 2 rows whose trailing status text is a stale "stuck:" from an
    unrelated later event, plus 2 legacy rows that predate this status
    column entirely. `applied` is the authoritative signal; always trust it
    over the trailing text.
  - `already_applied:...` (module4/agent_driver.py VALID_STATUSES) means the
    ATS itself confirmed an application already exists for this job - the
    goal state (an application is on file) is true, so it counts as
    `submitted` even though our own `applied` flag is False (we did not send
    this particular one).
  - `closed`/`closed:...` is a dead listing: nothing was sent, not our fault.
  - `skipped:...` (the user's manual decisions), `paused:...`
    (policy/paused_sites.py), `linkedin_signed_out:...`
    (module4/linkedin_session.py) and `blocked_employer:...`
    (policy/employer_blocklist.py, module4/runner.py) are all outcomes we
    chose or a standing policy chose, not failures - `skipped_by_policy`.
  - Everything else with a non-null status (`stuck:...`, `captcha_failed:...`,
    `failed:...`, and any future prefix not listed above) is a genuine
    attempt that did not reach a terminal success - `stuck`. That includes
    `captcha_wall:...` (module4/captcha_walls.py): parked, but a wall the site
    put up, not a choice.
  - A null status (and `applied=False`) means the job was never attempted:
    `not_attempted`.
"""

from __future__ import annotations

OUTCOMES = frozenset(
    {"submitted", "closed", "stuck", "skipped_by_policy", "not_attempted"}
)

_CLOSED_PREFIXES = ("closed",)
_SUBMITTED_PREFIXES = ("already_applied",)
_POLICY_PREFIXES = (
    "skipped",
    "paused",
    "linkedin_signed_out",
    "blocked_employer",
)


def _prefix(status: str) -> str:
    return status.split(":", 1)[0]


def classify_outcome(*, applied: bool, last_status: str | None) -> str:
    """The canonical outcome bucket for one job row.

    `applied` always wins: it is the field every other part of the pipeline
    (pacing, run summaries) already treats as ground truth for "a real
    application was sent", so the taxonomy must agree with it rather than
    re-deriving the same fact from text and risking the two disagreeing.
    """
    if applied:
        return "submitted"
    if not last_status:
        return "not_attempted"
    prefix = _prefix(last_status)
    if prefix in _CLOSED_PREFIXES:
        return "closed"
    if prefix in _SUBMITTED_PREFIXES:
        return "submitted"
    if prefix in _POLICY_PREFIXES:
        return "skipped_by_policy"
    return "stuck"
