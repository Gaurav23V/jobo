#!/usr/bin/env python
"""Enrich losses found in GHO-16 (2026-10-15).

  * A logged-in LinkedIn page that never renders company/location/description
    used to end as `incomplete_extraction` and the job never reached triage
    (1687, 1727, 1738). The guest endpoint now fills the gaps; a guest page
    that says the job is closed becomes `posting_closed`.
  * The 3B enrichment model drops up to half of a long description, so the
    scraped text is kept verbatim as job_metadata["description_full"].
  * `jobo enrich --job-id N --force` re-enriches one row.

Offline: guest pages come from saved fixtures, the DB is a temp SQLite file,
no browser and no network.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module2 import linkedin_fetch, linkedin_guest, query  # noqa: E402
from module2 import runner as m2runner  # noqa: E402
from module3 import job_bundle  # noqa: E402

FIX = REPO / "scripts" / "fixtures"
OPEN_HTML = (FIX / "linkedin_guest_open.html").read_text(encoding="utf-8")
CLOSED_HTML = (FIX / "linkedin_guest_closed.html").read_text(encoding="utf-8")
URL = "https://www.linkedin.com/jobs/view/4000000101/"

PASS = 0
FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


def test_guest_parse():
    print("\n[1] guest fragment parsing")
    g = linkedin_guest.parse_guest_posting(OPEN_HTML)
    check("title", g.title == "Software Engineer – AI (Backend)", repr(g.title))
    check("company", g.company == "Northwind Example", repr(g.company))
    check("location", g.location == "Springfield, Illinois, United States", repr(g.location))
    check("description has the body", g.description and "LLM-powered applications" in g.description
          and len(g.description) > 1500, str(len(g.description or "")))
    check("open posting not closed", g.closed is False)
    c = linkedin_guest.parse_guest_posting(CLOSED_HTML)
    check("closed posting flagged", c.closed is True)
    check("closed posting still parses company", c.company == "Poplarco", repr(c.company))
    e = linkedin_guest.parse_guest_posting("")
    check("empty page = nothing, not closed", (e.title, e.company, e.description, e.closed) == (None, None, None, False))
    check("job id from view url", linkedin_guest.linkedin_job_id(URL) == "4000000101")
    check("job id from comm url",
          linkedin_guest.linkedin_job_id("https://www.linkedin.com/comm/jobs/view/123/?x=1") == "123")
    check("no id from other urls", linkedin_guest.linkedin_job_id("https://example.com/jobs/1") is None)
    check("no fetch without an id", linkedin_guest.fetch_guest_posting("https://example.com/x") is None)


def test_fill_from_guest():
    print("\n[2] browser scrape completed from the guest page")
    seen = []

    def fetch_open(url):
        seen.append(url)
        return linkedin_guest.parse_guest_posting(OPEN_HTML)

    # The 2026-10-14 shape: title only, everything else missing.
    out = linkedin_fetch._fill_from_guest(URL, "Browser Title", None, "", None, fetch=fetch_open)
    check("guest fetched once for the job url", seen == [URL], str(seen))
    check("result is complete", out is not None and out.error is None
          and not linkedin_fetch._missing_scrape_fields(out.title, out.company, out.location, out.description))
    check("browser value kept where present", out.title == "Browser Title", repr(out.title))
    check("missing fields filled from guest", (out.company, out.location) == ("Northwind Example", "Springfield, Illinois, United States"))

    closed = linkedin_fetch._fill_from_guest(
        URL, None, None, None, None, fetch=lambda u: linkedin_guest.parse_guest_posting(CLOSED_HTML))
    check("closed guest page -> posting_closed", closed is not None and closed.error == "posting_closed")

    check("guest fetch failure -> None (caller keeps its error)",
          linkedin_fetch._fill_from_guest(URL, None, None, None, None, fetch=lambda u: None) is None)
    check("guest page also incomplete -> None",
          linkedin_fetch._fill_from_guest(
              URL, None, None, None, None,
              fetch=lambda u: linkedin_guest.GuestPosting(title="T", company="C")) is None)


class _FakeLocator:
    def __init__(self):
        self.first = self

    def count(self):
        return 0


class _FakePage:
    """A logged-in job page whose top card never renders."""

    url = URL

    def __init__(self):
        self.closed = False

    def goto(self, *a, **k):
        return type("R", (), {"status": 200})()

    def wait_for_timeout(self, ms):
        pass

    def locator(self, sel):
        return _FakeLocator()

    def title(self):
        return ""

    def close(self):
        self.closed = True


def test_extract_uses_guest():
    print("\n[3] extract_linkedin_job falls back only after the browser retries")
    page = _FakePage()
    ctx = type("C", (), {"new_page": lambda self: page})()
    saved = (linkedin_fetch._linkedin_title_text, linkedin_fetch._linkedin_company_text,
             linkedin_fetch._linkedin_location_text, linkedin_fetch._linkedin_description_text,
             linkedin_guest.fetch_guest_posting)
    calls = []
    linkedin_fetch._linkedin_title_text = lambda p: "Browser Title"
    linkedin_fetch._linkedin_company_text = lambda p: None
    linkedin_fetch._linkedin_location_text = lambda p: None
    linkedin_fetch._linkedin_description_text = lambda p: None
    try:
        linkedin_guest.fetch_guest_posting = lambda u: (calls.append(u), linkedin_guest.parse_guest_posting(OPEN_HTML))[1]
        out = linkedin_fetch.extract_linkedin_job(ctx, URL)
        check("incomplete browser scrape completed by guest page",
              out.error is None and out.company == "Northwind Example" and out.title == "Browser Title", repr(out))
        check("guest called exactly once", calls == [URL], str(calls))
        check("page closed", page.closed)

        linkedin_guest.fetch_guest_posting = lambda u: None
        out2 = linkedin_fetch.extract_linkedin_job(ctx, URL)
        check("guest unavailable -> original incomplete_extraction error",
              (out2.error or "").startswith("incomplete_extraction: missing company, location, description"),
              repr(out2.error))

        calls.clear()
        linkedin_fetch._linkedin_company_text = lambda p: "BrowserCo"
        linkedin_fetch._linkedin_location_text = lambda p: "Pune"
        linkedin_fetch._linkedin_description_text = lambda p: "x" * 100
        linkedin_guest.fetch_guest_posting = lambda u: calls.append(u)
        out3 = linkedin_fetch.extract_linkedin_job(ctx, URL)
        check("complete browser scrape never calls the guest endpoint", out3.error is None and calls == [])
    finally:
        (linkedin_fetch._linkedin_title_text, linkedin_fetch._linkedin_company_text,
         linkedin_fetch._linkedin_location_text, linkedin_fetch._linkedin_description_text,
         linkedin_guest.fetch_guest_posting) = saved


def test_full_description():
    print("\n[4] verbatim description kept in metadata")
    meta = {"responsibilities": ["short summary"]}
    desc = "Line one of the posting.\nRequirement: Go and Kafka.\n" + "Detail. " * 50
    m2runner.attach_full_description(meta, desc)
    check("description_full stored verbatim", meta[m2runner.FULL_DESCRIPTION_KEY] == desc.strip())
    check("model fields kept", meta["responsibilities"] == ["short summary"])
    big = {}
    m2runner.attach_full_description(big, "y" * (m2runner.FULL_DESCRIPTION_MAX_CHARS + 500))
    check("long description capped and marked",
          len(big[m2runner.FULL_DESCRIPTION_KEY]) <= m2runner.FULL_DESCRIPTION_MAX_CHARS + 6
          and big[m2runner.FULL_DESCRIPTION_KEY].endswith("[...]"))
    empty = {}
    m2runner.attach_full_description(empty, None)
    check("no description -> no key", empty == {})
    job = JobModel(id=9, company_name="Co", job_title="T", job_url=URL, metadata_json=json.dumps(meta))
    check("triage bundle carries the full text", "Requirement: Go and Kafka." in job_bundle.build_job_bundle_text(job))


def test_enrich_job_id_selection():
    print("\n[5] enrich --job-id selection")
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        from datetime import datetime
        now = datetime.now()
        for jid, attempted in ((1, False), (2, True), (3, True)):
            session.add(JobModel(
                id=jid, job_url=f"https://www.linkedin.com/jobs/view/{jid}/", applied=False,
                referred_json="[]", first_seen_at=now, last_seen_at=now, metadata_json="{}",
                module2_attempted=attempted, module4_attempts=0,
            ))
        session.commit()
        ids = lambda **k: [j.id for j in query.list_jobs_for_enrichment(session, **k)]
        check("default: only never-attempted rows", ids() == [1], str(ids()))
        check("--job-id alone keeps the attempted filter", ids(job_id=2) == [], str(ids(job_id=2)))
        check("--job-id --force retries one failed row", ids(job_id=2, force=True) == [2])
        check("--force alone still means all rows", ids(force=True) == [1, 2, 3])
    finally:
        session.close()
        engine.dispose()
        os.unlink(path)


def main() -> int:
    print("enrich recovery regressions (GHO-16)")
    test_guest_parse()
    test_fill_from_guest()
    test_extract_uses_guest()
    test_full_description()
    test_enrich_job_id_selection()
    total = PASS + FAIL
    print(f"\n{PASS}/{total} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
