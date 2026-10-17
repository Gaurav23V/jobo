"""jobfound.org source: Atom feed + sitemap gap fill -> pre-enriched JobCandidates.

Access (robots.txt allows everything except /api/, /_next/, /login; this
module never requests those):

  1. the Atom feed ``/feeds/jobs`` (always the newest 500 jobs, params ignored);
  2. the sitemap ``/sitemap.xml``: ``/job/<slug>`` URLs whose ``lastmod`` is
     newer than the cursor (minus ``CURSOR_OVERLAP``) and that are NOT an
     alternate link in this feed are the "gap" the feed no longer covers;
  3. only those job pages are fetched (one at a time, ``PAGE_DELAY_S`` apart)
     and parsed (schema.org JobPosting JSON-LD + the Next.js ``initialJob``
     payload) into the same entry shape as a feed entry.

The cursor (newest ``published``/``lastmod`` seen) lives in the
``source_state`` table and moves only after a successful insert.

The feed is NOT well-formed XML: apply URLs carry raw ``&`` (for example
``?in_iframe=1&source=jobfound.org``). lxml's recover mode keeps the entries
but silently drops ``&source`` from such URLs, so bare ``&`` outside CDATA is
escaped first. If the document still does not parse, each ``<entry>`` is
parsed alone; one bad entry never loses the batch.

No scraping beyond jobfound's own pages, no LLM: the feed/page data already
is the enrichment (module 2 ``job_metadata`` shape, see sources/base.py).
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

import httpx
from lxml import etree
from sqlalchemy import text
from sqlalchemy.orm import Session

from policy import job_filters
from sources import store, urls
from sources.base import FACTS_KEY, JobCandidate
from sources.store import InsertResult

logger = logging.getLogger(__name__)

SOURCE = "jobfound"
BASE_URL = "https://jobfound.org"
FEED_URL = f"{BASE_URL}/feeds/jobs"
SITEMAP_URL = f"{BASE_URL}/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (compatible; jobo/0.1; personal job-search pipeline)"
# robots.txt disallows these; the fetcher refuses them outright.
DISALLOWED_PATH_PREFIXES = ("/api/", "/_next/", "/login")

TIMEOUT_S = 30.0
MAX_RETRIES = 3
BACKOFF_S = 2.0  # 2, 4, 8 s
MAX_RETRY_AFTER_S = 120.0
PAGE_DELAY_S = 1.5
CURSOR_OVERLAP = timedelta(minutes=15)
DEFAULT_SINCE_HOURS = 24
# Upper bound on gap pages per run (~PAGE_DELAY_S each). Gap pages are fetched
# oldest first; when the cap cuts the gap, the cursor stops at the last page
# fetched so the next run continues from there.
MAX_GAP_PAGES = 200

ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class FetchError(RuntimeError):
    pass


def _retry_after_s(resp: httpx.Response) -> Optional[float]:
    raw = (resp.headers.get("Retry-After") or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


class Fetcher:
    """One request at a time, polite delay between pages, retry with backoff."""

    def __init__(
        self,
        client: Optional[httpx.Client] = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        page_delay_s: float = PAGE_DELAY_S,
    ):
        self.client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_S, follow_redirects=True
        )
        self.client.headers["User-Agent"] = USER_AGENT
        self.sleep = sleep
        self.page_delay_s = page_delay_s
        self.requests: list[str] = []
        self._last_page_at: Optional[float] = None

    def get(self, url: str, *, page: bool = False) -> str:
        path = urlsplit(url).path or "/"
        if any(path.startswith(p) for p in DISALLOWED_PATH_PREFIXES):
            raise FetchError(f"refusing robots-disallowed path {path}")
        if page and self._last_page_at is not None:
            wait = self.page_delay_s - (time.monotonic() - self._last_page_at)
            if wait > 0:
                self.sleep(wait)
        last_err = ""
        try:
            for attempt in range(MAX_RETRIES + 1):
                self.requests.append(url)
                try:
                    resp = self.client.get(url)
                except httpx.HTTPError as e:
                    last_err = f"{type(e).__name__}: {e}"
                    delay = BACKOFF_S * 2**attempt
                else:
                    if resp.status_code == 200:
                        return resp.text
                    last_err = f"HTTP {resp.status_code}"
                    if resp.status_code == 404:
                        break
                    if resp.status_code not in (429, 500, 502, 503, 504):
                        break
                    delay = _retry_after_s(resp)
                    if delay is None:
                        delay = BACKOFF_S * 2**attempt
                    delay = min(delay, MAX_RETRY_AFTER_S)
                if attempt < MAX_RETRIES:
                    logger.info("jobfound: %s for %s, retrying in %.0fs", last_err, url, delay)
                    self.sleep(delay)
        finally:
            if page:
                self._last_page_at = time.monotonic()
        raise FetchError(f"{url}: {last_err}")


# ---------------------------------------------------------------------------
# Entry shape (feed entry or job page)
# ---------------------------------------------------------------------------


@dataclass
class Entry:
    id: str  # jobfound hex id
    title: Optional[str] = None
    company: Optional[str] = None
    page_url: Optional[str] = None
    apply_url: Optional[str] = None
    published: Optional[str] = None
    updated: Optional[str] = None
    description_html: Optional[str] = None
    labels: list[str] = field(default_factory=list)  # raw category labels
    location: Optional[str] = None  # page jobLocation (feed: Location label)
    workplace_type: Optional[str] = None  # page only ("onsite", "remote", ...)
    via: str = "feed"


@dataclass
class FeedParse:
    entries: list[Entry] = field(default_factory=list)
    skipped: Counter = field(default_factory=Counter)  # reason -> count
    mode: str = "strict"  # strict | recover | per_entry


_CDATA_RE = re.compile(rb"(<!\[CDATA\[.*?\]\]>)", re.S)
_BARE_AMP_RE = re.compile(rb"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")
_ENTRY_RE = re.compile(rb"<entry\b.*?</entry>", re.S)


def _escape_bare_amps(data: bytes) -> bytes:
    parts = _CDATA_RE.split(data)
    return b"".join(p if p.startswith(b"<![CDATA[") else _BARE_AMP_RE.sub(b"&amp;", p) for p in parts)


def _entry_from_element(el: etree._Element) -> Entry:
    raw_id = (el.findtext(f"{ATOM}id") or "").strip()
    jid = raw_id.rsplit(":", 1)[-1]
    if not jid:
        raise ValueError("no id")
    e = Entry(id=jid)
    e.title = (el.findtext(f"{ATOM}title") or "").strip() or None
    e.company = (el.findtext(f"{ATOM}author/{ATOM}name") or "").strip() or None
    e.published = (el.findtext(f"{ATOM}published") or "").strip() or None
    e.updated = (el.findtext(f"{ATOM}updated") or "").strip() or None
    for link in el.findall(f"{ATOM}link"):
        rel, href = link.get("rel"), (link.get("href") or "").strip()
        if rel == "alternate":
            e.page_url = href or None
        elif rel == "related" and (link.get("title") or "").lower() == "apply":
            e.apply_url = href or None
    content = el.findtext(f"{ATOM}content")
    e.description_html = content if content and content.strip() else el.findtext(f"{ATOM}summary")
    e.labels = [c.get("label") or c.get("term") or "" for c in el.findall(f"{ATOM}category")]
    e.labels = [label for label in e.labels if label.strip()]
    return e


def parse_feed(data: bytes) -> FeedParse:
    """Every parseable ``<entry>``; bad ones are skipped and counted."""
    out = FeedParse()
    expected = len(_ENTRY_RE.findall(data))
    clean = _escape_bare_amps(data)
    root = None
    try:
        root = etree.fromstring(clean, etree.XMLParser(resolve_entities=False, huge_tree=True))
    except etree.XMLSyntaxError:
        out.mode = "recover"
        root = etree.fromstring(
            clean, etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
        )
    elements = root.findall(f"{ATOM}entry") if root is not None else []
    if len(elements) < expected:
        out.mode = "per_entry"
        elements = []
        for chunk in _ENTRY_RE.findall(clean):
            wrapped = b'<entry xmlns="http://www.w3.org/2005/Atom"' + chunk[len(b"<entry"):]
            try:
                elements.append(etree.fromstring(wrapped, etree.XMLParser(resolve_entities=False)))
            except etree.XMLSyntaxError:
                try:
                    el = etree.fromstring(wrapped, etree.XMLParser(recover=True, resolve_entities=False))
                except etree.XMLSyntaxError:
                    el = None
                if el is None:
                    out.skipped["unparseable"] += 1
                    continue
                elements.append(el)
    for el in elements:
        try:
            out.entries.append(_entry_from_element(el))
        except ValueError as e:
            out.skipped[str(e)] += 1
    if out.skipped:
        logger.warning("jobfound feed: skipped %s (mode=%s)", dict(out.skipped), out.mode)
    return out


def parse_sitemap(data: bytes) -> list[tuple[str, Optional[datetime]]]:
    """``(url, lastmod)`` for every ``/job/<slug>`` URL."""
    root = etree.fromstring(
        _escape_bare_amps(data), etree.XMLParser(recover=True, resolve_entities=False, huge_tree=True)
    )
    out = []
    if root is None:
        return out
    for u in root.iter("{*}url"):
        loc = (u.findtext("{*}loc") or "").strip()
        if "/job/" not in urlsplit(loc).path:
            continue
        out.append((loc, _parse_time(u.findtext("{*}lastmod"))))
    return out


_LD_RE = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)
_NEXT_PUSH_RE = re.compile(r"self\.__next_f\.push\(\[1,(\"(?:[^\"\\]|\\.)*\")\]\)", re.S)


def _next_payload(page_html: str) -> str:
    chunks = []
    for m in _NEXT_PUSH_RE.finditer(page_html):
        try:
            chunks.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return "".join(chunks)


def _payload_text_ref(payload: str, ref: str) -> Optional[str]:
    """Resolve a React flight text reference ("$1f" -> the ``1f:T<hexlen>,`` chunk)."""
    m = re.search(rf"(?<![0-9a-zA-Z$]){re.escape(ref)}:T([0-9a-f]+),", payload)
    if not m:
        return None
    n = int(m.group(1), 16)
    body = payload[m.end():].encode("utf-8")[:n]
    return body.decode("utf-8", errors="ignore")


def parse_job_page(page_html: str, page_url: str) -> Entry:
    """A job page in the feed entry shape (``via='page'``)."""
    ld: dict[str, Any] = {}
    for m in _LD_RE.finditer(page_html):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict) and d.get("@type") == "JobPosting":
            ld = d
            break
    job: dict[str, Any] = {}
    payload = _next_payload(page_html)
    i = payload.find('"initialJob":')
    if i >= 0:
        try:
            job, _ = json.JSONDecoder().raw_decode(payload, i + len('"initialJob":'))
        except ValueError:
            job = {}
    if not ld and not job:
        raise ValueError("no JobPosting JSON-LD or job payload")
    ident = ld.get("identifier") if isinstance(ld.get("identifier"), dict) else {}
    jid = str(job.get("$id") or ident.get("value") or "").strip()
    if not jid:
        raise ValueError("no job id")
    e = Entry(id=jid, page_url=page_url, via="page")
    e.title = job.get("title") or ld.get("title")
    org = ld.get("hiringOrganization") if isinstance(ld.get("hiringOrganization"), dict) else {}
    e.company = job.get("companyName") or org.get("name")
    e.apply_url = job.get("applyUrl") or ld.get("applyUrl")
    e.published = job.get("postedAt") or ld.get("datePosted")
    desc = job.get("description")
    if isinstance(desc, str) and desc.startswith("$"):
        desc = _payload_text_ref(payload, desc[1:])
    e.description_html = desc or ld.get("description")
    loc = ld.get("jobLocation") if isinstance(ld.get("jobLocation"), dict) else {}
    addr = loc.get("address") if isinstance(loc.get("address"), dict) else {}
    e.location = job.get("location") or addr.get("addressLocality") or job.get("country")
    e.workplace_type = job.get("workplaceType") or None
    # Rebuild the feed's labels from the payload so one filter path serves both.
    domain = job.get("domain") or ld.get("industry")
    if domain:
        e.labels.append(domain)
    if job.get("jobType"):
        e.labels.append(f"Job Type: {job['jobType']}")
    if e.location:
        e.labels.append(f"Location: {job.get('country') or e.location}")
    if job.get("salary"):
        e.labels.append(f"Salary: {job['salary']}")
    exp = str(job.get("experience") or "").strip()
    if exp and exp != "0":
        e.labels.append(f"Experience: {exp}+ years")  # the feed writes "2-4" as "2-4+ years"
    elif exp == "0":
        e.labels.append("Entry Level")
    elif ld.get("experienceRequirements"):
        e.labels.append(f"Experience: {ld['experienceRequirements']}")
    return e


# ---------------------------------------------------------------------------
# Entry -> JobCandidate
# ---------------------------------------------------------------------------


_BLOCK_END_RE = re.compile(r"</(?:p|div|ul|ol|h[1-6]|tr|table|section)\s*>|<br\s*/?>", re.I)
_LI_RE = re.compile(r"<li\b[^>]*>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: Optional[str]) -> str:
    """Plain text with paragraph and list breaks kept."""
    if not raw:
        return ""
    s = _LI_RE.sub("\n- ", raw)
    s = _BLOCK_END_RE.sub("\n", s)
    s = html.unescape(_TAG_RE.sub("", s))
    lines = [re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in s.split("\n")]
    out: list[str] = []
    for ln in lines:
        if ln or (out and out[-1]):
            out.append(ln)
    return "\n".join(out).strip()


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    """Naive UTC datetime (how jobs.db stores times), or None."""
    v = (value or "").strip()
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _label_value(labels: list[str], key: str) -> Optional[str]:
    for raw in labels:
        k, sep, v = html.unescape(raw).partition(":")
        if sep and k.strip().lower() == key and v.strip():
            return v.strip()
    return None


def entry_to_candidate(e: Entry) -> tuple[JobCandidate, job_filters.JobFacts, bool]:
    """``(candidate, facts, used_page_url_fallback)``."""
    facts = job_filters.facts_from_labels(e.title, e.labels)
    job_url = urls.normalize_apply_url(e.apply_url)
    fallback = job_url is None
    if fallback:
        job_url = urls.normalize_apply_url(e.page_url) or e.page_url
    enrichment: dict[str, Any] = {
        "employment_type": facts.job_type,
        "remote_hybrid_onsite": e.workplace_type,
        "salary": facts.salary,
        "experience": facts.experience,
        "categories": list(facts.categories),
        "description_full": html_to_text(e.description_html),
        FACTS_KEY: facts.as_dict(),
        "_jobfound": {
            "id": e.id,
            "page_url": e.page_url,
            "apply_url_raw": e.apply_url,
            "categories_raw": list(e.labels),
            "published": e.published,
            "updated": e.updated,
            "via": e.via,
        },
    }
    cand = JobCandidate(
        job_url=job_url,
        source=SOURCE,
        company_name=e.company,
        job_title=e.title,
        location=_label_value(e.labels, "location") or e.location,
        date_released=_parse_time(e.published),
        enrichment=enrichment,
        enrichment_model=SOURCE,
        source_key=e.id,
    )
    return cand, facts, fallback


# ---------------------------------------------------------------------------
# Cursor (source_state table)
# ---------------------------------------------------------------------------


def ensure_source_state(session: Session) -> None:
    session.execute(
        text(
            "CREATE TABLE IF NOT EXISTS source_state ("
            "source TEXT PRIMARY KEY, cursor TEXT, updated_at DATETIME)"
        )
    )


def read_cursor(session: Session, source: str = SOURCE) -> Optional[datetime]:
    ensure_source_state(session)
    row = session.execute(
        text("SELECT cursor FROM source_state WHERE source = :s"), {"s": source}
    ).first()
    return _parse_time(row[0]) if row and row[0] else None


def write_cursor(session: Session, value: datetime, source: str = SOURCE) -> None:
    ensure_source_state(session)
    session.execute(
        text(
            "INSERT INTO source_state (source, cursor, updated_at) VALUES (:s, :c, :u) "
            "ON CONFLICT(source) DO UPDATE SET cursor = excluded.cursor, updated_at = excluded.updated_at"
        ),
        {"s": source, "c": value.isoformat(), "u": datetime.utcnow().isoformat(sep=" ")},
    )
    session.commit()


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


@dataclass
class JobfoundResult:
    fetched: int = 0  # entries parsed (feed + gap pages)
    feed_entries: int = 0
    parse_skipped: dict[str, int] = field(default_factory=dict)
    feed_error: Optional[str] = None
    sitemap_error: Optional[str] = None
    gap_urls: int = 0
    gap_truncated: int = 0
    pages_fetched: int = 0
    page_errors: int = 0
    thin_fallbacks: int = 0
    thin_fallback_errors: int = 0
    page_url_fallbacks: int = 0  # no usable apply link; stored the jobfound page URL
    kept: int = 0
    dropped_by_reason: dict[str, int] = field(default_factory=dict)
    insert: InsertResult = field(default_factory=InsertResult)
    cursor_before: Optional[datetime] = None
    cursor_after: Optional[datetime] = None
    candidates: list[JobCandidate] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        """Both the feed and the sitemap failed: nothing could be collected."""
        return self.feed_error is not None and self.sitemap_error is not None


def _thin(text_: str) -> bool:
    return len(text_ or "") < job_filters.THIN_DESCRIPTION_CHARS


def collect(
    session: Session,
    *,
    dry_run: bool = False,
    since_hours: int = DEFAULT_SINCE_HOURS,
    gap_fill: bool = True,
    force_gap_fill: bool = False,
    max_gap_pages: int = MAX_GAP_PAGES,
    fetcher: Optional[Fetcher] = None,
    now: Optional[datetime] = None,
) -> JobfoundResult:
    """Fetch, filter and insert jobfound jobs.

    ``gap_fill=False``: feed only (the cursor is left alone, since the gap was
    not looked at). ``force_gap_fill``: treat every sitemap job URL in the
    window as a gap URL even when the feed has it (end-to-end checks of the
    page path). ``dry_run``: no DB writes at all, not even the cursor.
    """
    fetcher = fetcher or Fetcher()
    now = now or datetime.utcnow()
    res = JobfoundResult()
    res.cursor_before = read_cursor(session) if not dry_run else _read_cursor_ro(session)

    entries: dict[str, Entry] = {}
    try:
        feed = parse_feed(fetcher.get(FEED_URL).encode("utf-8"))
        res.feed_entries = len(feed.entries)
        res.parse_skipped = dict(feed.skipped)
        if not feed.entries:
            raise ValueError("feed parsed to zero entries")
        for e in feed.entries:
            entries.setdefault(e.id, e)
    except (FetchError, etree.XMLSyntaxError, ValueError) as ex:
        res.feed_error = str(ex)
        logger.error("jobfound feed failed: %s", ex)
    feed_pages = {e.page_url for e in entries.values() if e.page_url}

    newest = max((t for t in (_parse_time(e.published) for e in entries.values()) if t), default=None)
    cursor_cap: Optional[datetime] = None
    if gap_fill:
        since = (res.cursor_before - CURSOR_OVERLAP) if res.cursor_before else now - timedelta(hours=since_hours)
        try:
            smap = parse_sitemap(fetcher.get(SITEMAP_URL).encode("utf-8"))
        except (FetchError, etree.XMLSyntaxError, ValueError) as ex:
            res.sitemap_error = str(ex)
            logger.error("jobfound sitemap failed: %s", ex)
            smap = []
        gap = sorted(
            (lm, loc)
            for loc, lm in smap
            if lm is not None and lm > since and (force_gap_fill or loc not in feed_pages)
        )
        res.gap_urls = len(gap)
        if len(gap) > max_gap_pages:
            res.gap_truncated = len(gap) - max_gap_pages
            cursor_cap = gap[max_gap_pages - 1][0]
            logger.warning(
                "jobfound: %d gap URLs, fetching the oldest %d; the rest wait for the next run",
                len(gap), max_gap_pages,
            )
            gap = gap[:max_gap_pages]
        for lm, loc in gap:
            try:
                e = parse_job_page(fetcher.get(loc, page=True), loc)
                res.pages_fetched += 1
            except (FetchError, ValueError) as ex:
                res.page_errors += 1
                logger.warning("jobfound page failed: %s", ex)
                continue
            entries[e.id] = e  # a page wins over the feed copy (force_gap_fill)
            newest = max(filter(None, (newest, lm)))
    res.fetched = len(entries)

    dropped: Counter = Counter()
    cands: list[JobCandidate] = []
    for e in entries.values():
        cand, facts, fallback = entry_to_candidate(e)
        if not cand.job_url:
            res.parse_skipped["no_url"] = res.parse_skipped.get("no_url", 0) + 1
            continue
        reason = job_filters.drop_reason(facts)
        if reason is None and _thin(cand.enrichment["description_full"]) and e.via == "feed" and e.page_url:
            res.thin_fallbacks += 1
            try:
                page = parse_job_page(fetcher.get(e.page_url, page=True), e.page_url)
                res.pages_fetched += 1
                desc = html_to_text(page.description_html)
                if len(desc) > len(cand.enrichment["description_full"]):
                    e.description_html = page.description_html
                known = {lbl.partition(":")[0].strip().lower() for lbl in e.labels if ":" in lbl}
                e.labels += [
                    lbl for lbl in page.labels
                    if ":" in lbl and lbl.partition(":")[0].strip().lower() not in known
                ]
                if not job_filters.facts_from_labels(e.title, e.labels).categories:
                    e.labels += [lbl for lbl in page.labels if ":" not in lbl]
                e.workplace_type = e.workplace_type or page.workplace_type
                e.location = e.location or page.location
                cand, facts, fallback = entry_to_candidate(e)
                reason = job_filters.drop_reason(facts)
            except (FetchError, ValueError) as ex:
                res.page_errors += 1
                res.thin_fallback_errors += 1
                cand.enrichment_error = f"thin description; job page fetch failed: {ex}"
        if reason is not None:
            dropped[reason] += 1
            continue
        res.page_url_fallbacks += int(fallback)
        cands.append(cand)
    res.kept = len(cands)
    res.dropped_by_reason = dict(dropped)
    res.candidates = cands

    res.insert = store.insert_candidates(session, cands, dry_run=dry_run, now=now)
    if (
        not dry_run
        and gap_fill
        and res.feed_error is None
        and res.sitemap_error is None
        and not res.insert.errors
    ):
        after = cursor_cap or newest
        if after and (res.cursor_before is None or after > res.cursor_before):
            write_cursor(session, after)
            res.cursor_after = after
    logger.info(
        "jobfound: feed=%d skipped=%s gap_urls=%d pages=%d page_errors=%d thin=%d kept=%d dropped=%s inserted=%d dup=%d",
        res.feed_entries, res.parse_skipped, res.gap_urls, res.pages_fetched, res.page_errors,
        res.thin_fallbacks, res.kept, res.dropped_by_reason, res.insert.inserted, res.insert.duplicates,
    )
    return res


def _read_cursor_ro(session: Session) -> Optional[datetime]:
    """Cursor without creating the table (dry runs write nothing)."""
    has = session.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_state'")
    ).first()
    if not has:
        return None
    row = session.execute(
        text("SELECT cursor FROM source_state WHERE source = :s"), {"s": SOURCE}
    ).first()
    return _parse_time(row[0]) if row and row[0] else None
