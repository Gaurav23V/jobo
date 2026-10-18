#!/usr/bin/env python3
"""J&J Workday ran out of step budget on Voluntary Disclosures (GHO-56).

Job 1885 (2026-10-18) used 80 of 80 model steps and stopped on step 4 of 5.
The session shows where they went:
  * ~12 steps creating the Workday account by typing. No handoff was asked
    for, so the account-setup allowance (GHO-20) never applied.
  * ~25 steps on one "School or University" search box. The catalogue has no
    entry for the candidate's school; the agent typed spellings by hand, and
    the one NEED_FIELD that "worked" was the bare acronym, which the answer
    book matched to a DIFFERENT school by token containment.

Covers:
  [1] a search box that finds nothing walks the candidate's own spellings and
      then the form's escape "Other - See Resume", in one NEED_FIELD
  [2] the bare acronym never picks a same-acronym different school
  [3] a listed school under another true spelling is preferred over Other
  [4] answer_book: no containment match for schools; search query plan
  [5] a Workday tab (seen by a handoff helper) grants the account-setup
      allowance once, with no account handoff; other sites get nothing

[1]-[3] run against scripts/workday_school_probe.html in an own headless
Chromium on a random port (never 9333). [4]-[5] are offline.
"""
import os
import subprocess
import sys
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("JOBO_PERSONAL_DIR", str(Path(__file__).resolve().parents[1] / "personal.example"))
from module4 import agent_driver, answer_book as ab, browser_session, form_filler as f  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PROBE = Path(__file__).with_name("workday_school_probe.html")
FULL = "Lakeside Institute of Information Technology Springfield"
results: list[bool] = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" {detail}" if detail and not ok else ""), flush=True)


def browser_checks() -> None:
    with tempfile.TemporaryDirectory(prefix="jobo-wd-school-", ignore_cleanup_errors=True) as profile:
        proc = subprocess.Popen(
            ["/usr/bin/chromium", "--headless", "--no-sandbox", "--disable-dev-shm-usage",
             "--remote-debugging-port=0", "--window-size=1280,900", "--user-data-dir=" + profile, "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            portfile = Path(profile) / "DevToolsActivePort"
            for _ in range(200):
                if portfile.exists():
                    break
                time.sleep(0.05)
            cdp = "http://127.0.0.1:" + portfile.read_text().splitlines()[0]
            assert not cdp.endswith(":9333")
            with sync_playwright() as p:
                page = p.chromium.connect_over_cdp(cdp).contexts[0].pages[0]

                def load():
                    page.goto(PROBE.as_uri())
                    page.wait_for_timeout(200)

                def put(label, value):
                    ls = f.list_fields(cdp)
                    f.remember_listing(cdp, ls)
                    fi = next((x for x in ls.fields if x.label.lower().startswith(label.lower())), None)
                    if not fi:
                        return f.SetFieldResult(False, note="not listed")
                    return f.set_field(cdp, "#" + str(fi.index), value, index=fi.index)

                pills = lambda id_: page.evaluate(
                    "(i)=>[...document.getElementById(i).closest('[data-automation-id=multiselectInputContainer]')"
                    ".querySelectorAll('[data-automation-id=selectedItem]')].map(n=>n.title)", id_)
                lists_open = lambda: page.evaluate("()=>document.querySelectorAll('.results').length")

                print("\n[1] school missing from the catalogue: one NEED_FIELD lands on the escape")
                load()
                r = put("School or University", FULL)
                check("set succeeds without another agent turn", r.ok, r.note)
                check("the form's escape is picked", pills("school") == ["Other - See Resume"], str(pills("school")))
                check("the agent is told what was substituted", r.substituted == "Other - See Resume"
                      and "Other - See Resume" in r.as_agent_message(), r.as_agent_message())
                check("no results list left open", lists_open() == 0)

                print("\n[2] bare acronym (job 1885's case): never a same-acronym different school")
                load()
                r = put("School or University", "LIIT")
                check("no different school picked", not any(t in ("LIIT Bombay", "LIIT Delhi") for t in pills("school")),
                      str(pills("school")))
                check("lands on the escape instead", r.ok and pills("school") == ["Other - See Resume"],
                      f"{r.note} {pills('school')}")

                print("\n[3] school listed under another true spelling: picked over Other")
                load()
                r = put("Previous School", "LIIT Springfield")
                check("comma spelling picked", r.ok and pills("school2") == [
                    "Lakeside Institute of Information Technology, Springfield"], f"{r.note} {pills('school2')}")
        finally:
            proc.terminate()
            proc.wait(timeout=10)


def answer_book_checks() -> None:
    print("\n[4] answer_book")
    check("acronym does not match a longer different school",
          ab.pick_from_options("LIIT", ["LIIT Bombay", "LIIT Delhi"]) is None,
          repr(ab.pick_from_options("LIIT", ["LIIT Bombay", "LIIT Delhi"])))
    check("a field of study never matches a school by containment",
          ab.pick_from_options("Information Technology", ["Lakeside Institute of Information Technology Bombay"])
          != "Lakeside Institute of Information Technology Bombay")
    check("Workday's escape is a school fallback",
          ab.pick_from_options(FULL, ["Harvard University", "Other - See Resume"]) == "Other - See Resume")
    check("non-school containment still works",
          ab.pick_from_options("Computer Science", ["Computer Science and Engineering", "Mechanical Engineering"])
          == "Computer Science and Engineering")
    q = ab.search_queries_for(FULL)
    check("search plan: own spellings first, then two escape queries",
          q == ["LIIT Springfield", "Lakeside Institute of Information Technology", "LIIT", "Other", "not listed"], str(q))
    check("search plan fits the cap", len(q) <= f.MAX_SEARCH_FALLBACKS, str(len(q)))
    check("a bare alias walks the school chain too", ab.search_queries_for("LIIT")[:1] == [FULL],
          str(ab.search_queries_for("LIIT")))
    check("free text gets no searches", ab.search_queries_for("24 LPA") == [])


def drive(outputs, counts, urls):
    """Run the driver on scripted rounds. Each NEED_FIELD handoff "lists" the
    worker's tabs the way the real helpers do (browser_session.note_tabs)."""
    calls = []
    requests = []
    outs, steps = iter(outputs), iter(counts)
    cdp = "http://127.0.0.1:9"

    def fake_run(*, prompt, cdp_url, timeout_sec, max_turns, resume_session=None, source_url=""):
        calls.append({"max_turns": int(max_turns), "prompt": prompt})
        return next(outs) + "\n\nsession_id: s1\n", False

    def fake_field(line, *, cdp_url, job_id, failures):
        browser_session.note_tabs(cdp_url, [{"type": "page", "url": u} for u in urls])
        return "FIELD_SET: #1"

    browser_session.note_tabs(cdp, [{"type": "page", "url": "https://jj.wd5.myworkdayjobs.com/old-job"}])
    with ExitStack() as st:
        st.enter_context(patch.object(agent_driver, "_run_hermes", side_effect=fake_run))
        st.enter_context(patch.object(agent_driver, "session_api_calls", side_effect=lambda sid, p: next(steps)))
        st.enter_context(patch.object(agent_driver, "_resolve_field", side_effect=fake_field))
        st.enter_context(patch.object(agent_driver.urllib.request, "urlopen",
                                      side_effect=lambda *a, **k: requests.append(a) or (_ for _ in ()).throw(OSError())))
        st.enter_context(patch.object(agent_driver, "_dump_debug_transcript"))
        st.enter_context(patch.object(agent_driver, "hermes_profile", return_value="jobo"))
        st.enter_context(patch.dict(os.environ, {"JOBO_APPLY_MAX_TURNS": ""}))
        out = agent_driver.run_apply_agent(
            system_prompt="S", user_prompt="U", cdp_url=cdp, job_id=1885,
            resume_path="/r.pdf", is_retry=False, budget_boost=False,
        )
    return out, calls, requests


def driver_checks() -> None:
    print("\n[5] Workday account allowance without an account handoff")
    base, extra = agent_driver.AGENT_STEPS_FIRST, agent_driver.ACCOUNT_SETUP_STEPS
    # 1885's shape: account created by typing, first handoff at step 27.
    out, calls, requests = drive(["NEED_FIELD: #10 = x", "NEED_FIELD: #11 = y", "NEED_FIELD: #12 = z"],
                                 [27, 79, base + extra],
                                 ["https://jj.wd5.myworkdayjobs.com/en-US/JJ/job/x/apply"])
    check("round 2 runs with the allowance", calls[1]["max_turns"] == base + extra - 27, str(calls[1]["max_turns"]))
    check("the agent is told its new total", f"now {base + extra}" in calls[1]["prompt"], calls[1]["prompt"])
    check("granted once only", f"now {base + extra}" not in calls[2]["prompt"])
    check("the job runs past the old 80-step cap", len(calls) == 3, f"{len(calls)} rounds")
    check("detail names the allowance", f"plus {extra} for account setup" in out.detail, out.detail)
    # The suite's guards forbid browser contact from the loop itself (offline
    # tests drive it with the production CDP URL).
    check("the driver loop makes no browser request of its own", requests == [], str(requests))
    _, calls, _ = drive(["NEED_FIELD: #1 = x", "NEED_FIELD: #2 = y"], [27, base],
                        ["https://boards.greenhouse.io/acme/jobs/1", "https://www.linkedin.com/jobs/view/1"])
    check("non-Workday sites get no allowance (a previous job's Workday tab is forgotten)",
          calls[1]["max_turns"] == base - 27, str(calls[1]["max_turns"]))
    check("lookalike host is not Workday", not agent_driver._WORKDAY_HOST.search("myworkdayjobs.com.evil.test"))
    check("myworkdaysite tenant is Workday", bool(agent_driver._WORKDAY_HOST.search("wd3.myworkdaysite.com")))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'[{"type":"page","url":"https://acme.wd1.myworkdayjobs.com/x","webSocketDebuggerUrl":"ws://x"}]'

    browser_session.forget_tabs("http://127.0.0.1:8")
    with patch.object(f.urllib.request, "urlopen", return_value=_Resp()):
        f._page_targets("http://127.0.0.1:8")
    check("form_filler's tab listing records the host",
          "acme.wd1.myworkdayjobs.com" in browser_session.seen_hosts("http://127.0.0.1:8"))


def main() -> int:
    answer_book_checks()
    driver_checks()
    browser_checks()
    print(f"\n{sum(results)}/{len(results)} passed", flush=True)
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
