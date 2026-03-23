import logging
import os
import random
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

logger = logging.getLogger(__name__)

# Node 22+ emits DEP0169 for legacy `url.parse()`; Playwright's Node driver still hits that
# path. Child processes inherit NODE_OPTIONS (see node --disable-warning=).
_NODE_DEP0169_FLAG = "--disable-warning=DEP0169"


def _suppress_node_dep0169_for_playwright() -> None:
    cur = os.environ.get("NODE_OPTIONS", "").strip()
    parts = cur.split() if cur else []
    if _NODE_DEP0169_FLAG in parts:
        return
    os.environ["NODE_OPTIONS"] = " ".join([*parts, _NODE_DEP0169_FLAG])


@dataclass
class ExtractedPageText:
    """Plain-text slices from a LinkedIn job posting (for the LLM)."""

    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    error: Optional[str] = None

    def to_llm_blob(self) -> str:
        if self.error:
            return f"(extraction_error: {self.error})"
        parts = []
        if self.title:
            parts.append(f"Title: {self.title}")
        if self.company:
            parts.append(f"Company: {self.company}")
        if self.location:
            parts.append(f"Location: {self.location}")
        if self.description:
            parts.append(f"Description:\n{self.description}")
        return "\n".join(parts) if parts else "(empty extraction)"


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    return default


def maybe_perform_linkedin_login(context: BrowserContext) -> None:
    """Vanilla launch only: sign in if email/password env vars are set."""
    email = (os.environ.get("JOBO_LINKEDIN_EMAIL") or "").strip()
    password = (os.environ.get("JOBO_LINKEDIN_PASSWORD") or "").strip()
    if not email or not password:
        return

    page = context.new_page()
    try:
        page.goto(
            "https://www.linkedin.com/login",
            wait_until="domcontentloaded",
            timeout=45_000,
        )
        if "/feed" in page.url:
            logger.info("LinkedIn session already active (feed URL).")
            return
        page.fill('input[name="session_key"]', email)
        page.fill('input[name="session_password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("domcontentloaded", timeout=45_000)
        logger.info("LinkedIn login form submitted (check for 2FA/CAPTCHA manually if needed).")
    finally:
        page.close()


def _first_text(page: Page, selectors: list[str], timeout: float = 3_000) -> Optional[str]:
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            t = loc.inner_text(timeout=timeout)
            t = re.sub(r"\s+", " ", t).strip()
            if t:
                return t
        except Exception:
            continue
    return None


def _linkedin_description_text(page: Page) -> Optional[str]:
    selectors = [
        '[data-testid="expandable-text-box"]',
        ".jobs-description-content__text",
        ".jobs-description__content",
        "[class*='job-details-about-the-job']",
        "article.jobs-description",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            t = loc.inner_text(timeout=5_000)
            t = t.strip()
            if len(t) > 50:
                return t
        except Exception:
            continue
    return None


def _linkedin_title_from_early_main_paragraphs(page: Page) -> Optional[str]:
    """Unverified postings use a plain <p> for the title (no Verified badge); hashed classes rotate."""
    try:
        ps = page.locator("main p")
        n = min(ps.count(), 15)
        for i in range(n):
            try:
                raw = ps.nth(i).inner_text(timeout=2_000)
            except Exception:
                continue
            t = re.sub(r"\s+", " ", raw).strip()
            if not t or len(t) < 3 or len(t) > 200:
                continue
            if t.lower().startswith("http"):
                continue
            # Skip LinkedIn metadata line: "City · 3 days ago · N applicants"
            if "·" in t and re.search(r"ago|applicant", t, re.IGNORECASE):
                continue
            return t
    except Exception:
        pass
    return None


def _linkedin_title_text(page: Page) -> Optional[str]:
    """Verified row first; then legacy/h1; then plain top-card <p> (unverified listings)."""
    t = _first_text(
        page,
        [
            'p:has([aria-label="Verified job"])',
            "main h1",
            "h1",
            ".job-details-jobs-unified-top-card__job-title",
            "[data-test-job-card-title]",
        ],
    )
    if t:
        return t
    return _linkedin_title_from_early_main_paragraphs(page)


def _linkedin_company_text(page: Page) -> Optional[str]:
    """Company name link in top card; avoid hashed BEM-only classes."""
    selectors = [
        ".job-details-jobs-unified-top-card__company-name a",
        "a[data-tracking-control-name='public_jobs_topcard-org-name']",
        'a[href*="linkedin.com/company/"][href*="/life"]',
        'main a[href*="linkedin.com/company/"]',
        'a[href*="linkedin.com/company/"]',
        ".job-details-jobs-unified-top-card__company-name",
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.count() == 0:
                continue
            t = loc.inner_text(timeout=3_000)
            t = re.sub(r"\s+", " ", t).strip()
            if t and len(t) < 200:
                return t
        except Exception:
            continue
    return None


def _linkedin_location_text(page: Page) -> Optional[str]:
    """Location from top metadata line 'Place · time ago · applicants' or legacy bullets."""
    t = _first_text(
        page,
        [
            ".job-details-jobs-unified-top-card__bullet",
            ".job-details-jobs-unified-top-card__primary-description-container",
        ],
    )
    if t:
        return re.sub(r"\s+", " ", t).split("\n")[0].strip()

    try:
        cands = page.locator("p").filter(
            has_text=re.compile(r"ago|applicant", re.IGNORECASE)
        )
        n = min(cands.count(), 40)
        for i in range(n):
            try:
                raw = cands.nth(i).inner_text(timeout=2_000)
            except Exception:
                continue
            raw = re.sub(r"\s+", " ", raw).strip()
            if "·" not in raw:
                continue
            part = raw.split("·")[0].strip()
            if part and len(part) > 2 and not part.lower().startswith("http"):
                return part
    except Exception:
        pass
    return None


def extract_linkedin_job(context: BrowserContext, job_url: str) -> ExtractedPageText:
    page = context.new_page()
    try:
        page.goto(job_url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(2_500)

        u = page.url.lower()
        if "login" in u and "jobs/view" not in u:
            return ExtractedPageText(error="redirected_to_login")
        if "challenge" in u or "checkpoint" in u:
            return ExtractedPageText(error="linkedin_checkpoint_or_challenge")

        title = _linkedin_title_text(page)
        company = _linkedin_company_text(page)
        location = _linkedin_location_text(page)
        description = _linkedin_description_text(page)

        if not title and not description:
            return ExtractedPageText(
                error="no_title_or_description",
                title=title,
                company=company,
                location=location,
                description=description,
            )

        return ExtractedPageText(
            title=title,
            company=company,
            location=location,
            description=description,
        )
    except Exception as e:
        logger.exception(f"LinkedIn fetch failed for {job_url}")
        return ExtractedPageText(error=str(e))
    finally:
        page.close()


@contextmanager
def playwright_browser_context() -> Iterator[BrowserContext]:
    """
    CDP > persistent user dir > vanilla launch.
    Caller must run on main thread (sync Playwright).
    """
    headless = _env_bool("JOBO_PLAYWRIGHT_HEADLESS", False)
    cdp = (os.environ.get("JOBO_PLAYWRIGHT_CDP_URL") or "").strip()
    user_data = (os.environ.get("JOBO_PLAYWRIGHT_USER_DATA_DIR") or "").strip()

    _suppress_node_dep0169_for_playwright()

    with sync_playwright() as p:
        browser: Optional[Browser] = None
        context: Optional[BrowserContext] = None
        used_cdp = False
        try:
            if cdp:
                used_cdp = True
                logger.info(f"Playwright: connect_over_cdp({cdp})")
                browser = p.chromium.connect_over_cdp(cdp)
                if browser.contexts:
                    context = browser.contexts[0]
                else:
                    context = browser.new_context()
            elif user_data:
                logger.info(f"Playwright: launch_persistent_context({user_data})")
                context = p.chromium.launch_persistent_context(
                    user_data,
                    channel="chromium",
                    headless=headless,
                )
            else:
                logger.info(f"Playwright: vanilla chromium.launch(headless={headless})")
                browser = p.chromium.launch(headless=headless)
                context = browser.new_context()
                maybe_perform_linkedin_login(context)

            assert context is not None
            yield context
        finally:
            if used_cdp and browser is not None:
                browser.close()
            else:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()


def enrich_delay_jitter() -> None:
    """Sleep a random 0..JOBO_ENRICH_DELAY_MS_MAX ms between job URLs; no-op if unset/invalid."""
    raw = os.environ.get("JOBO_ENRICH_DELAY_MS_MAX", "").strip()
    if not raw:
        return
    try:
        ms_max = int(raw)
    except ValueError:
        return
    if ms_max <= 0:
        return
    time.sleep(random.uniform(0, ms_max / 1000.0))
