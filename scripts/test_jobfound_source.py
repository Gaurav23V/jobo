"""Offline tests for the jobfound.org source (sources/jobfound.py, GHO-41a).

No network: every HTTP request goes to an httpx.MockTransport serving the
fixtures in scripts/fixtures/jobfound/ (real feed/page STRUCTURE, fictional
employers, ids and URLs). Databases are throwaway SQLite files; inserts go through the
real sources.store.insert_candidates.

Run: bash scripts/run_regression_tests.sh test_jobfound_source
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
_TMP = Path(tempfile.mkdtemp(prefix="jobfound-test-"))
# main.py / db.database bind their engine at import; point them at a temp DB.
os.environ["JOBO_DB_PATH"] = str(_TMP / "cli.db")

import httpx  # noqa: E402
from lxml import etree  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from db.models import Base, JobModel  # noqa: E402
from module3.job_bundle import build_job_bundle_text  # noqa: E402
from policy import job_filters as jfl  # noqa: E402
from sources import jobfound as jf  # noqa: E402
from sources import store, urls  # noqa: E402
from sources.store import InsertResult  # noqa: E402

FIX = REPO / "scripts" / "fixtures" / "jobfound"
FEED = (FIX / "feed_sample.xml").read_bytes()
PAGE_GAP = (FIX / "page_gap.html").read_text()
PAGE_THIN = (FIX / "page_thin.html").read_text()
GAP_URL = "https://jobfound.org/job/wonka-platforms-is-hiring-for-site-reliability-engineer-india-17-october-2026"
THIN_URL = "https://jobfound.org/job/oscorp-web-is-hiring-for-frontend-developer-india-17-october-2026"
OLD_URL = "https://jobfound.org/job/old-co-is-hiring-for-engineer-india-01-october-2026"
NOW = datetime(2026, 10, 17, 21, 0, 0)
MALFORMED_ID = "f00d0000000000000012"

PASS = FAIL = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {label}")
    else:
        FAIL += 1
        print(f"  FAIL {label} {extra}")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def sitemap_xml(feed_entries) -> bytes:
    urls_ = [("https://jobfound.org/remote", "2026-10-17T20:40:33.409Z")]
    urls_ += [(e.page_url, e.published) for e in feed_entries]
    urls_ += [(GAP_URL, "2026-10-17T05:10:00.000Z"), (OLD_URL, "2026-10-01T05:00:00.000Z")]
    body = "".join(f"<url><loc>{u}</loc><lastmod>{lm}</lastmod></url>\n" for u, lm in urls_)
    return f'<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}</urlset>\n'.encode()


FEED_ENTRIES = jf.parse_feed(FEED).entries
SITEMAP = sitemap_xml(FEED_ENTRIES)


class Site:
    """Mock jobfound.org. ``overrides[url]`` = list of (status, headers) served first."""

    def __init__(self, *, feed_status=200, sitemap_status=200, overrides=None):
        self.feed_status = feed_status
        self.sitemap_status = sitemap_status
        self.overrides = {k: list(v) for k, v in (overrides or {}).items()}
        self.seen: list[str] = []
        self.user_agents: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.seen.append(url)
        self.user_agents.add(request.headers.get("user-agent", ""))
        if self.overrides.get(url):
            status, headers = self.overrides[url].pop(0)
            return httpx.Response(status, headers=headers, text="slow down")
        if url == jf.FEED_URL:
            return httpx.Response(self.feed_status, content=FEED if self.feed_status == 200 else b"err")
        if url == jf.SITEMAP_URL:
            return httpx.Response(self.sitemap_status, content=SITEMAP if self.sitemap_status == 200 else b"err")
        if url == GAP_URL:
            return httpx.Response(200, text=PAGE_GAP)
        if url == THIN_URL:
            return httpx.Response(200, text=PAGE_THIN)
        return httpx.Response(404, text="not found")

    def fetcher(self, sleeps: list[float]) -> jf.Fetcher:
        client = httpx.Client(transport=httpx.MockTransport(self.handler))
        return jf.Fetcher(client, sleep=sleeps.append)


def temp_db(name: str):
    path = _TMP / name
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return path, sessionmaker(bind=engine)()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_feed_parse() -> None:
    print("\n[1] lenient feed parser")
    try:
        ET.fromstring(FEED)
        check("ElementTree fails on the fixture (unescaped &)", False)
    except ET.ParseError:
        check("ElementTree fails on the fixture (unescaped &)", True)
    fp = jf.parse_feed(FEED)
    check("all 20 entries recovered, none skipped", len(fp.entries) == 20 and not fp.skipped,
          f"{len(fp.entries)} {dict(fp.skipped)}")
    bad = next((e for e in fp.entries if e.id == MALFORMED_ID), None)
    check("malformed entry keeps its full apply URL",
          bad is not None and bad.apply_url.endswith("/job?in_iframe=1&source=jobfound.org"),
          repr(bad and bad.apply_url))
    # Why the bare-& escape exists: lxml recover alone mangles that URL.
    root = etree.fromstring(FEED, etree.XMLParser(recover=True))
    hrefs = [link.get("href") for link in root.iter("{http://www.w3.org/2005/Atom}link")]
    check("lxml recover alone would lose '&source' (guard is needed)",
          not any(h and h.endswith("in_iframe=1&source=jobfound.org") for h in hrefs))
    e1 = next(e for e in fp.entries if e.id.endswith("01"))
    check("entry fields", (e1.title, e1.company, e1.published) == ("Data Engineer", "Acme Analytics", "2026-10-17T18:07:00.000Z")
          and e1.page_url.startswith("https://jobfound.org/job/") and "Data & AI" in e1.labels, repr(e1))

    # One structurally broken entry and one without an id: the rest survive.
    broken = FEED.replace(b"<title type=\"text\">Customer Service Representative</title>",
                          b"<title type=\"text\">Customer Service Representative<title>", 1)
    broken = broken.replace(b"<id>urn:jobfound:job:f00d0000000000000014</id>", b"<id></id>", 1)
    fp2 = jf.parse_feed(broken)
    ids = {e.id for e in fp2.entries}
    others = {e.id for e in fp.entries} - {"f00d0000000000000005", "f00d0000000000000014"}
    check("broken document: every other entry still parsed", others <= ids, f"missing {others - ids}")
    check("broken document: entries + skipped == 20 and no-id entry counted",
          len(fp2.entries) + sum(fp2.skipped.values()) == 20 and fp2.skipped.get("no id") == 1,
          f"{len(fp2.entries)} {dict(fp2.skipped)} mode={fp2.mode}")


def test_urls() -> None:
    print("\n[2] URL normalization")
    li = "https://www.linkedin.com/jobs/view/4000000001/"
    cases = [
        ("https://in.linkedin.com/jobs/view/data-engineer-at-acme-analytics-4000000001?source=jobfound.org", li),
        ("https://www.linkedin.com/jobs/view/4000000001/", li),
        ("https://www.linkedin.com/comm/jobs/view/4000000001?trackingId=AbC%3D%3D", li),
        ("http://linkedin.com/jobs/view/4000000001", li),
        ("https://www.linkedin.com/jobs/view/senior-engineer-at-globex-labs-4000000001/?refId=Xy12%3D&source=jobfound.org", li),
        ("https://job-boards.greenhouse.io/acmeanalytics/jobs/7000001?gh_jid=7000001&source=jobfound.org",
         "https://job-boards.greenhouse.io/acmeanalytics/jobs/7000001?gh_jid=7000001"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/External/job/Pune/Data-Engineer_R-10001?source=jobfound.org",
         "https://acme.wd5.myworkdayjobs.com/en-US/External/job/Pune/Data-Engineer_R-10001"),
        ("https://careers-acme.icims.com/jobs/10018/data-engineer/job?in_iframe=1&source=jobfound.org",
         "https://careers-acme.icims.com/jobs/10018/data-engineer/job?in_iframe=1"),
        ("https://careers.acme.example/jobs/ml-1?utm_source=jobfound&jobId=55&source=jobfound.org#apply",
         "https://careers.acme.example/jobs/ml-1?jobId=55"),
        ("https://job-boards.greenhouse.io/embed/job_app?for=acmeanalytics&token=7000002&gh_src=jobfound.org",
         "https://job-boards.greenhouse.io/embed/job_app?for=acmeanalytics&token=7000002"),
        ("https://job-boards.greenhouse.io/acmeanalytics/jobs/7000003?gh_src=a1b2c3d4",
         "https://job-boards.greenhouse.io/acmeanalytics/jobs/7000003?gh_src=a1b2c3d4"),
    ]
    for raw, want in cases:
        got = urls.normalize_apply_url(raw)
        check(f"{raw[:70]}", got == want, f"-> {got}")


def facts(title, *labels):
    return jfl.facts_from_labels(title, list(labels))


def test_filters() -> None:
    print("\n[3] collection-time filter rules")
    eng = "Engineering and Development"
    cases = [
        ("10-12 LPA kept", facts("Software Engineer", eng, "Salary: 10-12 LPA"), None),
        ("8-11 LPA dropped", facts("Software Engineer", eng, "Salary: 8-11 LPA"), "low_pay"),
        ("12-16 LPA kept", facts("Software Engineer", eng, "Salary: 12-16 LPA"), None),
        ("5++ years dropped", facts("Platform Engineer", "DevOps", "Experience: 5++ years"), "too_senior"),
        ("2-4+ years kept", facts("Platform Engineer", "DevOps", "Experience: 2-4+ years"), None),
        ("no experience tag kept", facts("Platform Engineer", "DevOps"), None),
        ("Entry Level only + tech title kept", facts("Python Developer", "Entry Level"), None),
        ("Entry Level only + Customer Service Representative dropped",
         facts("Customer Service Representative", "Entry Level"), "non_tech"),
        ("Backend Engineer, Testing Infrastructure kept", facts("Backend Engineer, Testing Infrastructure", eng), None),
        ("QA Automation Engineer dropped", facts("QA Automation Engineer", eng), "qa_testing"),
        ("SDET dropped", facts("SDET", eng), "qa_testing"),
        ("Manual Testing dropped", facts("Manual Testing Engineer", eng), "qa_testing"),
        ("Quality Assurance category dropped", facts("Engineer II", "Quality Assurance"), "qa_testing"),
        ("internship by job type", facts("Software Engineer Trainee", eng, "Job Type: Internship"), "internship"),
        ("internship by category", facts("Data Analyst", "Internship", "Data &amp; AI"), "internship"),
        ("internship by title", facts("Graduate Intern - Machine Learning", "Data & AI"), "internship"),
        ("USD pay never dropped for pay", facts("Backend Engineer", eng, "Salary: $26.58/hr - $34.38/hr"), None),
        ("GBP pay never dropped for pay", facts("ML Engineer", "Data & AI", "Salary: £35K-£50K"), None),
        ("Competitive never dropped for pay", facts("Frontend Developer", eng, "Salary: Competitive"), None),
        ("no location filter (USA job passes)", facts("Backend Engineer", eng, "Location: USA", "Salary: $120K-$240K"), None),
        ("non-tech category dropped", facts("Sales Executive", "Sales", "Salary: 3-5 LPA"), "non_tech"),
    ]
    for label, f, want in cases:
        got = jfl.drop_reason(f)
        check(label, got == want, f"got {got}")


def test_page_and_text() -> None:
    print("\n[4] job page parser and description text")
    p = jf.parse_job_page(PAGE_GAP, GAP_URL)
    check("page id/title/company/apply",
          (p.id, p.title, p.company, p.via) == ("f00d0000000000000015", "Site Reliability Engineer", "Wonka Platforms", "page")
          and p.apply_url.endswith("4000000021?source=jobfound.org"), repr(p))
    check("page labels rebuilt in feed form",
          p.labels == ["DevOps", "Job Type: Full-time", "Location: India", "Salary: 12-16 LPA", "Experience: 2-4+ years"],
          repr(p.labels))
    check("page location + workplace type", (p.location, p.workplace_type) == ("Springfield, Northshire, India", "hybrid"))
    text_ = jf.html_to_text(p.description_html)
    check("payload HTML description resolved ($1f text chunk), paragraphs kept",
          "\n- Design, build and ship features end to end." in text_ and "Responsibilities\n" in text_ and "<" not in text_,
          repr(text_[:200]))
    ld_only = PAGE_GAP.split("<script>self.__next_f")[0] + "</body></html>"
    p2 = jf.parse_job_page(ld_only, GAP_URL)
    check("JSON-LD only page still parses", p2.id == p.id and len(jf.html_to_text(p2.description_html)) > 300
          and "Experience: 2-4 years" in p2.labels, repr(p2.labels))
    try:
        jf.parse_job_page("<html>nothing</html>", GAP_URL)
        check("page without job data raises", False)
    except ValueError:
        check("page without job data raises", True)
    check("html_to_text unescapes and keeps breaks",
          jf.html_to_text("<p>A &amp; B</p><p>C<br>D</p>") == "A & B\nC\nD")


def test_fetcher() -> None:
    print("\n[5] fetcher: UA, robots, retry, Retry-After, page delay")
    site = Site(overrides={GAP_URL: [(429, {"Retry-After": "7"}), (503, {})]})
    sleeps: list[float] = []
    f = site.fetcher(sleeps)
    try:
        f.get("https://jobfound.org/api/jobs")
        check("refuses /api/", False)
    except jf.FetchError:
        check("refuses /api/", not site.seen)
    body = f.get(GAP_URL, page=True)
    check("429 then 503 then 200", "initialJob" in body and site.seen.count(GAP_URL) == 3)
    check("Retry-After honoured, then backoff", sleeps[:2] == [7.0, jf.BACKOFF_S * 2], repr(sleeps))
    check("UA is the jobo UA", site.user_agents == {jf.USER_AGENT}, repr(site.user_agents))
    f.get(THIN_URL, page=True)
    check("delay between page fetches", len(sleeps) == 3 and sleeps[2] >= 1.0, repr(sleeps))
    site404 = Site()
    try:
        site404.fetcher([]).get(OLD_URL, page=True)
        check("404 fails without retries", False)
    except jf.FetchError:
        check("404 fails without retries", site404.seen == [OLD_URL])
    site500 = Site(feed_status=500)
    s500: list[float] = []
    try:
        site500.fetcher(s500).get(jf.FEED_URL)
        check("500 retried MAX_RETRIES times", False)
    except jf.FetchError:
        check("500 retried MAX_RETRIES times", len(site500.seen) == jf.MAX_RETRIES + 1 and s500 == [2.0, 4.0, 8.0], repr(s500))


def _cursor(path: Path):
    with sqlite3.connect(path) as c:
        has = c.execute("SELECT 1 FROM sqlite_master WHERE name='source_state'").fetchone()
        return c.execute("SELECT cursor FROM source_state WHERE source='jobfound'").fetchone() if has else "NO TABLE"


def test_collect() -> None:
    print("\n[6] collector (mock HTTP, temp DB, real insert_candidates)")
    path, session = temp_db("collect.db")
    site = Site(overrides={GAP_URL: [(429, {"Retry-After": "5"})]})
    sleeps: list[float] = []
    res = jf.collect(session, fetcher=site.fetcher(sleeps), now=NOW)
    check("fetched 21 (20 feed + 1 gap page)", (res.feed_entries, res.fetched) == (20, 21), f"{res.feed_entries} {res.fetched}")
    check("one gap URL, fetched once after a 429", res.gap_urls == 1 and site.seen.count(GAP_URL) == 2 and 5.0 in sleeps,
          f"{res.gap_urls} {sleeps}")
    check("only feed, sitemap, gap page, thin page requested",
          set(site.seen) == {jf.FEED_URL, jf.SITEMAP_URL, GAP_URL, THIN_URL}, repr(sorted(set(site.seen))))
    check("old sitemap URL not fetched", OLD_URL not in site.seen)
    check("kept 9", res.kept == 9, str(res.kept))
    want = {"low_pay": 2, "too_senior": 1, "non_tech": 2, "internship": 4, "qa_testing": 3}
    check("dropped per reason", res.dropped_by_reason == want, repr(res.dropped_by_reason))
    check("thin fallback used once", (res.thin_fallbacks, res.thin_fallback_errors, res.pages_fetched) == (1, 0, 2),
          f"{res.thin_fallbacks} {res.thin_fallback_errors} {res.pages_fetched}")
    check("inserted 9", res.insert.inserted == 9, repr(res.insert))

    rows = {r.job_url: r for r in session.query(JobModel).all()}
    check("9 rows in DB", len(rows) == 9, str(len(rows)))
    r = rows.get("https://www.linkedin.com/jobs/view/4000000001/")
    meta = json.loads(r.metadata_json) if r else {}
    check("row pre-enriched (module2_attempted/enriched_at/model)",
          r is not None and r.module2_attempted and r.module2_enriched_at is not None and r.module2_model == "jobfound")
    check("row columns", r is not None and (r.source_platform, r.company_name, r.job_title, r.location)
          == ("jobfound", "Acme Analytics", "Data Engineer", "India")
          and r.date_released == datetime(2026, 10, 17, 18, 7))
    check("metadata in module-2 shape",
          meta.get("employment_type") == "Full-time" and meta.get("salary") == "12-16 LPA"
          and meta.get("experience") == "2-4+ years" and meta.get("categories") == ["Data & AI"]
          and len(meta.get("description_full", "")) > 300 and "remote_hybrid_onsite" in meta, repr(meta)[:300])
    check("_facts + _jobfound provenance",
          meta.get("_facts") == {"job_type": "Full-time", "categories": ["Data & AI"], "salary": "12-16 LPA", "experience": "2-4+ years"}
          and meta.get("_jobfound", {}).get("id") == "f00d0000000000000001"
          and meta["_jobfound"].get("via") == "feed"
          and meta["_jobfound"].get("apply_url_raw", "").endswith("?source=jobfound.org")
          and "Location: India" in meta["_jobfound"].get("categories_raw", []), repr(meta.get("_jobfound")))
    check("facts round-trip through policy.facts_from_row",
          jfl.facts_from_row(r.job_title, r.metadata_json).as_dict() == meta["_facts"])
    bundle = build_job_bundle_text(r) if r else ""
    check("module 3 job bundle carries the description", "Design, build and ship features" in bundle)
    gap_row = rows.get("https://www.linkedin.com/jobs/view/4000000021/")
    gmeta = json.loads(gap_row.metadata_json) if gap_row else {}
    check("gap job stored from its page (via=page, hybrid)",
          gmeta.get("_jobfound", {}).get("via") == "page" and gmeta.get("remote_hybrid_onsite") == "hybrid"
          and gap_row.location == "India", repr(gmeta.get("_jobfound")))
    thin_row = rows.get("https://www.linkedin.com/jobs/view/4000000016/")
    tmeta = json.loads(thin_row.metadata_json) if thin_row else {}
    check("thin description replaced from the page, facts filled",
          len(tmeta.get("description_full", "")) > 300 and tmeta.get("remote_hybrid_onsite") == "remote"
          and tmeta.get("experience") == "1-3+ years" and thin_row.module2_last_error is None, repr(tmeta)[:300])
    check("iCIMS/Workday/Greenhouse URLs keep ATS params",
          "https://tyrell.wd5.myworkdayjobs.com/en-US/External/job/Remote-USA/Backend-Engineer--Testing-Infrastructure_R-10013" in rows
          and "https://job-boards.greenhouse.io/embed/job_app?for=monsterssoftware&token=7000020" in rows)
    check("cursor advanced to newest published", _cursor(path) == ("2026-10-17T18:07:00",), repr(_cursor(path)))

    # Second run: cursor bounds the gap; the gap page is not refetched; all dups.
    site2 = Site()
    res2 = jf.collect(session, fetcher=site2.fetcher([]), now=NOW)
    check("second run: no gap URLs, no page fetches",
          res2.gap_urls == 0 and set(site2.seen) == {jf.FEED_URL, jf.SITEMAP_URL, THIN_URL}, repr(sorted(set(site2.seen))))
    check("second run: everything is a duplicate", res2.insert.inserted == 0 and res2.insert.duplicates == 8, repr(res2.insert))

    # Sitemap down: feed still inserted, cursor NOT moved.
    with sqlite3.connect(path) as c:
        c.execute("UPDATE source_state SET cursor='2026-10-17T00:00:00'")
    site3 = Site(sitemap_status=500)
    res3 = jf.collect(session, fetcher=site3.fetcher([]), now=NOW)
    check("sitemap failure: not fatal, cursor unchanged",
          res3.sitemap_error and not res3.failed and _cursor(path) == ("2026-10-17T00:00:00",), repr(_cursor(path)))

    # Insert errors: cursor NOT moved.
    real = store.insert_candidates
    store.insert_candidates = lambda s, c, **kw: InsertResult(seen=len(c), errors=["boom"])
    try:
        jf.collect(session, fetcher=Site().fetcher([]), now=NOW)
    finally:
        store.insert_candidates = real
    check("insert errors: cursor unchanged", _cursor(path) == ("2026-10-17T00:00:00",), repr(_cursor(path)))

    # Both down: failed.
    res5 = jf.collect(session, fetcher=Site(feed_status=500, sitemap_status=500).fetcher([]), now=NOW)
    check("feed AND sitemap down -> failed", res5.failed and res5.kept == 0)
    empty = Site()
    empty_feed = b'<?xml version="1.0"?><html><body>maintenance</body></html>'
    handler = empty.handler
    empty.handler = lambda req: httpx.Response(200, content=empty_feed) if str(req.url) == jf.FEED_URL else handler(req)
    res5b = jf.collect(session, dry_run=True, fetcher=empty.fetcher([]), now=NOW)
    check("feed with zero entries counts as a feed failure", res5b.feed_error == "feed parsed to zero entries")

    # Feed only: no sitemap request, cursor untouched.
    site6 = Site()
    jf.collect(session, gap_fill=False, fetcher=site6.fetcher([]), now=NOW)
    check("--no-gap-fill: no sitemap, cursor untouched",
          jf.SITEMAP_URL not in site6.seen and _cursor(path) == ("2026-10-17T00:00:00",))

    # Forced gap fill: every window URL fetched as a page (only gap+thin pages exist here).
    site7 = Site()
    res7 = jf.collect(session, dry_run=True, force_gap_fill=True, fetcher=site7.fetcher([]), now=NOW)
    check("--force-gap-fill: feed URLs in the window fetched as pages",
          res7.gap_urls == 21 and site7.seen.count(THIN_URL) >= 1 and res7.page_errors == 19, f"{res7.gap_urls} {res7.page_errors}")


def test_dry_run() -> None:
    print("\n[7] dry run writes nothing")
    path, session = temp_db("dry.db")
    before = path.read_bytes()
    res = jf.collect(session, dry_run=True, fetcher=Site().fetcher([]), now=NOW)
    session.close()
    check("dry run counts", res.kept == 9 and res.insert.inserted == 9 and res.cursor_after is None, repr(res.insert))
    check("dry run: DB file byte-identical (no rows, no source_state)", path.read_bytes() == before and _cursor(path) == "NO TABLE")


def test_cli() -> None:
    print("\n[8] CLI: jobo collect-jobfound")
    from click.testing import CliRunner

    import main as jobo_main

    orig = jf.Fetcher
    site = {"s": Site()}
    jf.Fetcher = lambda *a, **kw: orig(httpx.Client(transport=httpx.MockTransport(site["s"].handler)), sleep=lambda s: None)
    try:
        temp_db("cli.db")
        runner = CliRunner()
        out = runner.invoke(jobo_main.cli, ["collect-jobfound", "--dry-run", "--quiet", "--since-hours", "100000"])
        check("dry run exits 0", out.exit_code == 0, out.output + repr(out.exception))
        check("dry run prints kept and per-reason drops",
              "Kept: 9" in out.output and "internship: 4" in out.output and "qa_testing: 3" in out.output, out.output)
        check("dry run wrote nothing", _cursor(_TMP / "cli.db") == "NO TABLE")
        site["s"] = Site(feed_status=500, sitemap_status=500)
        out2 = runner.invoke(jobo_main.cli, ["collect-jobfound", "--dry-run", "--quiet", "--since-hours", "100000"])
        check("feed AND sitemap down -> non-zero exit", out2.exit_code == 1, out2.output)
        site["s"] = Site(feed_status=500)
        out3 = runner.invoke(jobo_main.cli, ["collect-jobfound", "--quiet", "--since-hours", "100000"])
        check("feed down, sitemap up -> exit 0", out3.exit_code == 0, out3.output + repr(out3.exception))
    finally:
        jf.Fetcher = orig


def main() -> int:
    test_feed_parse()
    test_urls()
    test_filters()
    test_page_and_text()
    test_fetcher()
    test_collect()
    test_dry_run()
    test_cli()
    print(f"\n{PASS}/{PASS + FAIL} passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
