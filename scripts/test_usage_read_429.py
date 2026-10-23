#!/usr/bin/env python
"""Usage reads that hit HTTP 429 must not leave the 5h gate blind (GHO-86).

On 23 Oct 22:00 four apply lanes read the Claude usage endpoint before every
job; 45 of 79 reads got HTTP 429, each logged as UNREADABLE and treated as
"proceed". The 5h session gate ran blind for two stretches while session
usage went 11% -> 25%, and the run stopped at 32% against a 30% line.

Covers:
  * check_budget_in_run: one shared reading per 60 s across lanes, Retry-After
    and doubling backoff after a failed read, the last good reading while
    reads fail, the 10-minute and 5-points-from-the-line stops, one notice per
    blind stretch, a stretch left by an earlier run
  * fetch_usage_detail: Retry-After off a real urllib HTTPError
  * runner: a replay of the run with a fake fetcher that keeps returning 429
    stops claiming instead of going on blind, and resumes when reads recover
  * check_budget (the pipeline budget-gate): unreadable still means proceed

Offline: temporary cache file and SQLite DB, fake clock, fake fetcher.
"""
from __future__ import annotations

import email.message
import io
import logging
import sys
import tempfile
import urllib.error
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))

from module4 import budget_guard, runner  # noqa: E402
import test_session_limit as sl  # noqa: E402  (its fake-clock run_module4 harness)

PASS = 0
FAIL = 0

T0 = sl.T0
RESETS = sl.iso(T0 + 4 * 3600)  # the 5h window in play resets well after the test
ERR_429 = "usage endpoint unreadable: HTTPError: HTTP Error 429: Too Many Requests"


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def payload(session_pct: float, weekly_pct: float = 7.0, resets: str = RESETS) -> dict:
    return {"five_hour": {"utilization": session_pct, "resets_at": resets},
            "seven_day": {"utilization": weekly_pct, "resets_at": sl.iso(T0 + 5 * 86400)}}


class Endpoint:
    """A fake fetcher: a script of replies, then the last one forever."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, dict):
            return reply, None, None
        retry_after = reply if isinstance(reply, (int, float)) else None
        return None, ERR_429, retry_after


LIMIT = object()  # a 429 with no Retry-After


class Reader:
    """check_budget_in_run on a fresh cache file and a settable clock."""

    def __init__(self, endpoint, now=T0):
        self.endpoint = endpoint
        self.now = now
        self.cache = Path(tempfile.mkdtemp(prefix="jobo_usage_cache_")) / "usage.json"

    def read(self, at: float | None = None):
        if at is not None:
            self.now = at
        return budget_guard.check_budget_in_run(
            threshold=0.5, session_threshold=0.3, cache_path=self.cache,
            fetcher=self.endpoint, now=lambda: self.now)


def test_shared_reading():
    print("\n[1] one reading shared across lanes for 60 s")
    ep = Endpoint(payload(11))
    r = Reader(ep)
    checks = [r.read(T0 + s) for s in (0, 2, 5, 30, 59)]  # four lanes + a straggler
    check("one request for five reads inside 60 s", ep.calls == 1, str(ep.calls))
    check("every lane sees the reading", all(c.session_utilization == 0.11 for c in checks))
    check("a cached reading is not blind", not any(c.blind or c.blind_stop for c in checks))
    r.read(T0 + 61)
    check("re-read once it is over 60 s old", ep.calls == 2, str(ep.calls))
    other = Reader(ep)
    other.cache = r.cache
    other.read(T0 + 70)
    check("another process on the same file reuses it", ep.calls == 2, str(ep.calls))


def test_backoff():
    print("\n[2] backoff after a 429")
    ep = Endpoint(payload(11), 120.0)  # good, then 429 with Retry-After: 120
    r = Reader(ep)
    r.read(T0)
    r.read(T0 + 61)
    check("first 429 fetched", ep.calls == 2)
    for s in (70, 100, 150, 180):
        r.read(T0 + s)
    check("no request before Retry-After passes", ep.calls == 2, str(ep.calls))
    r.read(T0 + 182)
    check("one request once it passes", ep.calls == 3, str(ep.calls))

    ep = Endpoint(LIMIT)
    r = Reader(ep)
    t, waits = T0, []
    for _ in range(5):
        r.read(t)
        nxt = budget_guard._load_state(r.cache)["retry_at"]
        waits.append(nxt - t)
        t = nxt
    check("no Retry-After: 60, 120, 240 s, capped at 300 s", waits == [60, 120, 240, 300, 300], str(waits))
    check("Retry-After over the cap is capped", budget_guard._backoff(3600, 1) == budget_guard.BACKOFF_MAX_SEC)

    check("Retry-After seconds", budget_guard.retry_after_seconds("120") == 120.0)
    check("Retry-After HTTP date",
          budget_guard.retry_after_seconds("Thu, 15 Oct 2026 09:02:00 GMT", now=T0) == 120.0)
    check("Retry-After garbage", budget_guard.retry_after_seconds("soon") is None)

    hdrs = email.message.Message()
    hdrs["Retry-After"] = "90"
    err = urllib.error.HTTPError(budget_guard.USAGE_URL, 429, "Too Many Requests", hdrs, io.BytesIO(b""))
    with patch.object(budget_guard.urllib.request, "urlopen", side_effect=err):
        got, error, retry = budget_guard.fetch_usage_detail("tok")
        old = budget_guard.fetch_usage("tok")
    check("fetch_usage_detail reads Retry-After off the 429", got is None and retry == 90.0 and "429" in error,
          f"{retry} {error}")
    check("fetch_usage keeps its two-value shape", old == (None, error))


def test_blind_stretch():
    print("\n[3] reads failing: last good reading, then stop")
    ep = Endpoint(payload(11), LIMIT)
    r = Reader(ep)
    r.read(T0)
    c = r.read(T0 + 120)
    check("429: decides on the last good reading", c.blind and c.session_utilization == 0.11
          and c.reading_age == 120, str(c))
    check("... and goes on", not c.blind_stop and not c.session_limited)
    check("the start of the stretch is announced", bool(c.notice) and "429" in c.notice, str(c.notice))
    check("the usage line says the reading is stale", "last good reading, 2 min old" in budget_guard.usage_summary(c))
    later = [r.read(T0 + s) for s in range(150, 600, 30)]
    check("announced once, not per job", not any(x.notice for x in later), str([x.notice for x in later if x.notice]))
    check("still going at 9.5 min", not any(x.blind_stop for x in later))
    c = r.read(T0 + 601)
    check("no good reading for over 10 min: stop claiming", bool(c.blind_stop) and "10 min" in c.blind_stop,
          str(c.blind_stop))
    check("a reading over 10 min old is not used", c.session_utilization is None)

    ep.replies = [payload(14)]
    c = r.read(T0 + 900)
    check("reads recover: live again", not c.blind and not c.blind_stop and c.session_utilization == 0.14)
    check("the end of the stretch is announced", bool(c.notice) and "readable again" in c.notice, str(c.notice))
    ep.replies = [LIMIT]
    c = r.read(T0 + 961)
    check("a new stretch is announced again", bool(c.notice) and "429" in c.notice)


def test_near_the_line():
    print("\n[4] last good reading within 5 points of the session line")
    r = Reader(Endpoint(payload(26), LIMIT))
    r.read(T0)
    c = r.read(T0 + 61)
    check("26% vs a 30% line, read fails: stop claiming at once", bool(c.blind_stop) and "26%" in c.blind_stop,
          str(c.blind_stop))
    r = Reader(Endpoint(payload(24), LIMIT))
    r.read(T0)
    check("24%: 6 points away, goes on", not r.read(T0 + 61).blind_stop)
    r = Reader(Endpoint(payload(29, resets=sl.iso(T0 + 30)), LIMIT))
    r.read(T0)
    check("29% in a window that has since reset: not held against this one", not r.read(T0 + 61).blind_stop)
    r = Reader(Endpoint(payload(40), LIMIT))
    r.read(T0)
    c = r.read(T0 + 61)
    check("last good over the line: stop", bool(c.blind_stop) and c.session_limited)


def test_earlier_run():
    print("\n[5] a cache left by an earlier run")
    r = Reader(Endpoint(payload(11), LIMIT))
    r.read(T0)
    r.read(T0 + 120)
    c = r.read(T0 + 900)
    check("that run ended blind", bool(c.blind_stop))
    c = r.read(T0 + 12 * 3600)
    check("12 h later, a 429 at the start: a new stretch, not an instant stop", not c.blind_stop, str(c.blind_stop))
    check("and it is announced", bool(c.notice))
    check("the 12 h old reading is not used", c.session_utilization is None)
    check("stops 10 min into this stretch", bool(r.read(T0 + 12 * 3600 + 601).blind_stop))


def test_pipeline_gate_unchanged():
    print("\n[6] pipeline budget-gate: unreadable still proceeds")
    with patch.object(budget_guard, "read_access_token", return_value="tok"), \
         patch.object(budget_guard, "fetch_usage", return_value=(None, ERR_429)):
        c = budget_guard.check_budget(threshold=0.5, session_threshold=0.3)
    check("check_budget: no stop on 429", not c.should_stop and not c.session_limited and not c.blind_stop)
    check("gate record says PROCEED_UNREADABLE", budget_guard.gate_record(c, "pipeline")["decision"] == "PROCEED_UNREADABLE")


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


def run_replay(endpoint, ids):
    clock = sl.Clock()
    reader = Reader(endpoint)

    def usage(c):
        return reader.read(c.wall)

    cap = Capture()
    runner.logger.addHandler(cap)
    try:
        result, spawned, _ = sl.run(ids, clock=clock, usage=usage)
    finally:
        runner.logger.removeHandler(cap)
    return result, spawned, cap.lines, clock


def test_runner_replay():
    print("\n[7] runner: 23 Oct replay, the endpoint keeps answering 429")
    sl.FAKE_PDF.write_bytes(b"%PDF-fake")
    ep = Endpoint(payload(11), LIMIT)
    result, spawned, lines, clock = run_replay(ep, list(range(1, 9)))
    # Job 1 on a live 11%; job 2 on the 7-min-old reading; before job 3 the
    # last good reading is 14 min old: hold, re-read each minute, then stop.
    check("jobs stop once the last good reading is over 10 min old", spawned == [1, 2], str(spawned))
    check("stage stopped on it", "no good Claude usage reading" in result.session_limit_stop,
          result.session_limit_stop)
    check("held for the hold limit before stopping", abs(clock.slept - runner.BLIND_HOLD_MAX_SEC) < 1, str(clock.slept))
    unreadable = [x for x in lines if "UNREADABLE" in x]
    check("no per-job UNREADABLE lines", not unreadable, str(unreadable[:3]))
    check("one 'reads failing' notice", sum("reads failing" in x for x in lines) == 1,
          str([x for x in lines if "reads failing" in x]))
    check("requests stay at the backoff pace, not one per read",
          ep.calls <= 2 + int(runner.BLIND_HOLD_MAX_SEC // budget_guard.BACKOFF_BASE_SEC), str(ep.calls))
    check("health names the stop", not runner.health_verdict(result).healthy)

    print("\n[8] runner: reads recover during the hold")
    ep = Endpoint(payload(11), LIMIT, LIMIT, LIMIT, payload(15))
    result, spawned, lines, _ = run_replay(ep, [1, 2, 3, 4])
    check("claims resume and the queue finishes", spawned == [1, 2, 3, 4], str(spawned))
    check("no stop recorded", not result.session_limit_stop, result.session_limit_stop)
    check("recovery logged", any("readable again" in x for x in lines))

    print("\n[9] runner: last good reading near the line")
    ep = Endpoint(payload(11), payload(27), LIMIT)
    result, spawned, _, _ = run_replay(ep, [1, 2, 3, 4])
    check("27% then 429: nothing claimed until a read succeeds; stops", spawned == [1, 2], str(spawned))


def main() -> int:
    """Entrypoint. The isolated regression runner imports this module and calls main()."""
    print("Usage reads under HTTP 429 (GHO-86)")
    test_shared_reading()
    test_backoff()
    test_blind_stretch()
    test_near_the_line()
    test_earlier_run()
    test_pipeline_gate_unchanged()
    test_runner_replay()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
