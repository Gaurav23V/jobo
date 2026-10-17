"""Shared work queue for a parallel apply run (GHO-5).

One `jobo apply --form-workers N` run = one coordinator + one LinkedIn worker
+ N form workers, each a separate process with its own Chromium. Every
worker pulls its next job from this queue, so the hazards of running them
side by side are settled here, in one place, with SQLite's own locking:

  * no job is applied to twice: a job moves pending -> claimed by exactly one
    worker under BEGIN IMMEDIATE, and a claim is per run (run_id), so a
    crashed run's leftovers never block or leak into the next one. The runner
    also re-reads the job row from jobs.db after the claim and skips anything
    already applied or closed.
  * routing: every queued job is a LinkedIn listing. The LinkedIn worker
    (the only signed-in browser) opens it, and either hands the employer's
    own form URL to the form workers (route 'form') or keeps it (route
    'linkedin': Easy Apply, a Google Form, or anything it could not resolve).
  * no two in-flight applications share an employer or an ATS family
    (Workday, Greenhouse, iCIMS, ... or the site's registrable domain). Two
    concurrent jobs on one ATS would mail their verification codes from the
    same sender, and neither could tell whose code arrived; they would also
    race the same account creation. Serialising them per family makes an OTP
    attributable by construction (see module4/agent_driver.py for the
    emailed-code side: a cross-worker lock and a ledger of consumed mails).
  * one STOP flag every worker checks before its next claim (model usage
    limit, a fatal coordinator decision).

The file lives in data/ (git-ignored) and is safe to delete between runs.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "apply_queue.sqlite"

ROUTE_FORM = "form"
ROUTE_LINKEDIN = "linkedin"

# A job handed back from a form worker to the LinkedIn worker (the employer's
# site wanted LinkedIn) is re-routed at most this many times per run.
MAX_REROUTES = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS queue (
    run_id TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    company_key TEXT NOT NULL DEFAULT '',
    route TEXT,
    apply_url TEXT,
    family TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    worker TEXT,
    reroutes INTEGER NOT NULL DEFAULT 0,
    agent_started INTEGER NOT NULL DEFAULT 0,
    note TEXT,
    updated_at REAL,
    PRIMARY KEY (run_id, job_id)
);
CREATE TABLE IF NOT EXISTS workers (
    run_id TEXT NOT NULL,
    worker TEXT NOT NULL,
    role TEXT NOT NULL,
    cdp_port INTEGER,
    pid INTEGER,
    state TEXT NOT NULL DEFAULT 'running',
    updated_at REAL,
    PRIMARY KEY (run_id, worker)
);
CREATE TABLE IF NOT EXISTS run_flags (
    run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (run_id, name)
);
CREATE TABLE IF NOT EXISTS otp_consumed (
    message_key TEXT PRIMARY KEY,
    run_id TEXT,
    job_id INTEGER,
    at REAL
);
"""

# Multi-tenant ATS vendors and the domains they serve forms AND mail from.
# Two employers on one vendor still share a sender family for their emailed
# codes (e.g. northwind@myworkday.com and treyco@myworkday.com), so the vendor,
# not the tenant, is the unit of exclusivity.
VENDOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "workday": ("myworkdayjobs.com", "myworkday.com", "workday.com", "myworkdaysite.com"),
    "greenhouse": ("greenhouse.io", "greenhouse-mail.io"),
    "lever": ("lever.co",),
    "ashby": ("ashbyhq.com",),
    "icims": ("icims.com",),
    "smartrecruiters": ("smartrecruiters.com",),
    "workable": ("workable.com",),
    "successfactors": ("successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"),
    "oracle": ("oraclecloud.com", "taleo.net", "oracle.com"),
    "avature": ("avature.net",),
    "phenom": ("phenompeople.com",),
    "rippling": ("rippling.com", "rippling-ats.com"),
    "zoho": ("zohorecruit.com", "zohorecruit.in", "zoho.com", "zoho.in"),
    "darwinbox": ("darwinbox.in", "darwinbox.com"),
    "keka": ("keka.com",),
    "freshteam": ("freshteam.com",),
    "turbohire": ("turbohire.co",),
    "shine": ("shine.com",),
    "naukri": ("naukri.com",),
    "instahyre": ("instahyre.com",),
    "jobvite": ("jobvite.com",),
    "bamboohr": ("bamboohr.com",),
    "recruitee": ("recruitee.com",),
    "teamtailor": ("teamtailor.com", "teamtailor-mail.com"),
    "breezy": ("breezy.hr",),
    "jazzhr": ("applytojob.com", "jazzhr.com"),
    "eightfold": ("eightfold.ai",),
    "linkedin": ("linkedin.com", "licdn.com"),
    "google": ("google.com", "forms.gle"),
}

# Second-level public suffixes seen on employer sites (co.in, com.au, ...).
_TWO_PART_SUFFIXES = {
    "co.in", "co.uk", "com.au", "co.jp", "com.sg", "co.nz", "com.br", "org.in",
    "net.in", "ac.in", "gov.in", "com.cn", "co.za", "com.mx",
}

_CORP_WORDS = {
    "pvt", "private", "ltd", "limited", "inc", "llc", "llp", "corp", "corporation",
    "co", "company", "technologies", "technology", "solutions", "services",
    "india", "the", "group", "global",
}


def company_key(name: str | None) -> str:
    """'Northwind in India' and 'Northwind' must collide; 'Treyco' and 'XY' must not."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    core = [w for w in words if w not in _CORP_WORDS and w != "in"]
    return " ".join(core or words)


def registrable_domain(host: str) -> str:
    host = (host or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _TWO_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def family_for_host(host: str) -> str:
    """The exclusivity unit for a host: a vendor family or its registrable domain."""
    reg = registrable_domain(host)
    if not reg:
        return ""
    for family, domains in VENDOR_FAMILIES.items():
        if any(reg == d or reg.endswith("." + d) for d in domains):
            return family
    return reg


def family_for_url(url: str | None) -> str:
    try:
        host = urlsplit(url or "").hostname or ""
    except ValueError:
        return ""
    return family_for_host(host)


def family_for_sender(sender: str | None) -> str:
    """Family of a mail sender: 'Northwind <x@myworkday.com>' -> 'workday'."""
    s = (sender or "").lower()
    m = re.search(r"@([a-z0-9.-]+\.[a-z]{2,})", s) or re.search(r"\b([a-z0-9-]+(?:\.[a-z0-9-]+)+)\b", s)
    return family_for_host(m.group(1)) if m else ""


def default_path() -> Path:
    raw = os.environ.get("JOBO_APPLY_QUEUE_DB", "").strip()
    return Path(raw) if raw else DEFAULT_PATH


def current_run_id() -> str:
    """Set by the coordinator for every worker it spawns; empty outside a parallel run."""
    return os.environ.get("JOBO_APPLY_RUN_ID", "").strip()


@dataclass
class Claim:
    job_id: int
    route: str | None
    apply_url: str | None
    family: str | None
    reroutes: int


class ApplyQueue:
    def __init__(self, run_id: str, path: Path | None = None):
        if not run_id:
            raise ValueError("run_id is required")
        self.run_id = run_id
        self.path = Path(path) if path else default_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- connection ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.executescript(_SCHEMA)
        return con

    def _tx(self):
        """BEGIN IMMEDIATE: one writer at a time, so a claim's read and its
        write cannot interleave with another worker's claim."""
        con = self._connect()
        con.execute("BEGIN IMMEDIATE")
        return con

    # -- run setup (coordinator) -------------------------------------------
    def create_run(self, jobs: list[tuple[int, str | None]]) -> int:
        """Queue (job_id, company_name) pairs in priority order."""
        con = self._tx()
        try:
            now = time.time()
            for seq, (job_id, company) in enumerate(jobs):
                con.execute(
                    "INSERT OR IGNORE INTO queue(run_id, job_id, seq, company_key, updated_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (self.run_id, int(job_id), seq, company_key(company), now),
                )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
        return len(jobs)

    # -- claiming ----------------------------------------------------------
    def claim_unrouted(self, worker: str) -> Claim | None:
        """LinkedIn worker: the next job that still needs routing. Routing only
        reads the listing, so employer/ATS exclusivity does not apply."""
        return self._claim(worker, route_sql="route IS NULL", params=(), state="resolving", exclusive=False)

    def claim_route(self, worker: str, route: str) -> Claim | None:
        """The next routed job this worker may apply to now."""
        return self._claim(worker, route_sql="route = ?", params=(route,), state="claimed", exclusive=True)

    def _claim(self, worker, *, route_sql, params, state, exclusive) -> Claim | None:
        con = self._tx()
        try:
            rows = con.execute(
                f"SELECT * FROM queue WHERE run_id = ? AND state = 'pending' AND {route_sql} "
                "ORDER BY seq",
                (self.run_id, *params),
            ).fetchall()
            busy_companies: set[str] = set()
            busy_families: set[str] = set()
            if exclusive and rows:
                # Only claims of LIVE workers block: a worker that died
                # mid-job must not lock its employer out for the whole run.
                for r in con.execute(
                    "SELECT q.company_key, q.family FROM queue q JOIN workers w "
                    "ON w.run_id = q.run_id AND w.worker = q.worker "
                    "WHERE q.run_id = ? AND q.state = 'claimed' AND q.worker != ? AND w.state = 'running'",
                    (self.run_id, worker),
                ):
                    if r["company_key"]:
                        busy_companies.add(r["company_key"])
                    if r["family"]:
                        busy_families.add(r["family"])
            chosen = None
            for r in rows:
                if exclusive and (
                    (r["company_key"] and r["company_key"] in busy_companies)
                    or (r["family"] and r["family"] in busy_families)
                ):
                    continue
                chosen = r
                break
            if chosen is None:
                con.execute("COMMIT")
                return None
            cur = con.execute(
                "UPDATE queue SET state = ?, worker = ?, updated_at = ? "
                "WHERE run_id = ? AND job_id = ? AND state = 'pending'",
                (state, worker, time.time(), self.run_id, chosen["job_id"]),
            )
            if cur.rowcount != 1:  # cannot happen under BEGIN IMMEDIATE; be loud if it does
                con.execute("ROLLBACK")
                logger.error("apply_queue: lost a claim race on job %s", chosen["job_id"])
                return None
            con.execute("COMMIT")
            return Claim(
                job_id=int(chosen["job_id"]),
                route=chosen["route"],
                apply_url=chosen["apply_url"],
                family=chosen["family"],
                reroutes=int(chosen["reroutes"] or 0),
            )
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    # -- transitions -------------------------------------------------------
    def _update(self, sql: str, params: tuple) -> int:
        con = self._tx()
        try:
            n = con.execute(sql, params).rowcount
            con.execute("COMMIT")
            return n
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def set_route(self, job_id: int, route: str, apply_url: str | None = None, note: str = "") -> None:
        """Hand a job to a route's workers (back to 'pending')."""
        family = family_for_url(apply_url) if apply_url else None
        self._update(
            "UPDATE queue SET route = ?, apply_url = ?, family = ?, state = 'pending', worker = NULL, "
            "note = ?, updated_at = ? WHERE run_id = ? AND job_id = ?",
            (route, apply_url, family, note[:300], time.time(), self.run_id, int(job_id)),
        )

    def reroute_to_linkedin(self, job_id: int, note: str) -> bool:
        """A form worker found the employer's site needs LinkedIn. False if the
        job already used its re-route, so it cannot ping-pong."""
        n = self._update(
            "UPDATE queue SET route = ?, family = NULL, state = 'pending', worker = NULL, "
            "reroutes = reroutes + 1, agent_started = 0, note = ?, updated_at = ? "
            "WHERE run_id = ? AND job_id = ? AND reroutes < ?",
            (ROUTE_LINKEDIN, note[:300], time.time(), self.run_id, int(job_id), MAX_REROUTES),
        )
        return n == 1

    def mark_agent_started(self, job_id: int) -> None:
        self._update(
            "UPDATE queue SET agent_started = 1, updated_at = ? WHERE run_id = ? AND job_id = ?",
            (time.time(), self.run_id, int(job_id)),
        )

    def finish(self, job_id: int, worker: str, note: str = "") -> None:
        self._update(
            "UPDATE queue SET state = 'done', note = COALESCE(NULLIF(?, ''), note), updated_at = ? "
            "WHERE run_id = ? AND job_id = ? AND worker = ? AND state IN ('claimed', 'resolving')",
            (note[:300], time.time(), self.run_id, int(job_id), worker),
        )

    def release(self, job_id: int, worker: str) -> None:
        """Give an untouched claim back (the worker stopped before using it).
        A job whose agent already started is finished instead, never re-queued:
        it may have been submitted."""
        self._update(
            "UPDATE queue SET state = CASE WHEN agent_started = 1 THEN 'done' ELSE 'pending' END, "
            "worker = CASE WHEN agent_started = 1 THEN worker ELSE NULL END, updated_at = ? "
            "WHERE run_id = ? AND job_id = ? AND worker = ? AND state IN ('claimed', 'resolving')",
            (time.time(), self.run_id, int(job_id), worker),
        )

    # -- observation -------------------------------------------------------
    def counts(self) -> dict[str, int]:
        con = self._connect()
        try:
            out: dict[str, int] = {}
            for r in con.execute(
                "SELECT COALESCE(route, 'unrouted') AS route, state, COUNT(*) AS n FROM queue "
                "WHERE run_id = ? GROUP BY 1, 2",
                (self.run_id,),
            ):
                out[f"{r['route']}:{r['state']}"] = int(r["n"])
            return out
        finally:
            con.close()

    def pending(self, route: str | None) -> int:
        con = self._connect()
        try:
            if route is None:
                row = con.execute(
                    "SELECT COUNT(*) FROM queue WHERE run_id = ? AND route IS NULL AND state IN ('pending', 'resolving')",
                    (self.run_id,),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT COUNT(*) FROM queue WHERE run_id = ? AND route = ? AND state = 'pending'",
                    (self.run_id, route),
                ).fetchone()
            return int(row[0])
        finally:
            con.close()

    def rows(self) -> list[dict]:
        con = self._connect()
        try:
            return [dict(r) for r in con.execute(
                "SELECT * FROM queue WHERE run_id = ? ORDER BY seq", (self.run_id,))]
        finally:
            con.close()

    # -- workers and flags -------------------------------------------------
    def register_worker(self, worker: str, role: str, cdp_port: int | None, pid: int | None) -> None:
        self._update(
            "INSERT OR REPLACE INTO workers(run_id, worker, role, cdp_port, pid, state, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (self.run_id, worker, role, cdp_port, pid, time.time()),
        )

    def worker_done(self, worker: str) -> None:
        self._update(
            "UPDATE workers SET state = 'done', updated_at = ? WHERE run_id = ? AND worker = ?",
            (time.time(), self.run_id, worker),
        )

    def running_workers(self, role: str | None = None) -> list[dict]:
        con = self._connect()
        try:
            q = "SELECT * FROM workers WHERE run_id = ? AND state = 'running'"
            args: tuple = (self.run_id,)
            if role:
                q += " AND role = ?"
                args += (role,)
            return [dict(r) for r in con.execute(q, args)]
        finally:
            con.close()

    def request_stop(self, reason: str) -> None:
        self._update(
            "INSERT OR IGNORE INTO run_flags(run_id, name, value) VALUES (?, 'stop', ?)",
            (self.run_id, reason[:300]),
        )

    def stop_reason(self) -> str | None:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT value FROM run_flags WHERE run_id = ? AND name = 'stop'", (self.run_id,)
            ).fetchone()
            return row[0] if row else None
        finally:
            con.close()

    # -- emailed codes -------------------------------------------------------
    def otp_consumed_keys(self, exclude_job: int | None = None) -> set[str]:
        """Mail ids already handed to a job (other than exclude_job), any run."""
        con = self._connect()
        try:
            return {
                r[0] for r in con.execute(
                    "SELECT message_key FROM otp_consumed WHERE job_id != ?",
                    (-1 if exclude_job is None else int(exclude_job),),
                )
            }
        finally:
            con.close()

    def consume_otp(self, message_key: str, job_id: int) -> bool:
        """Record that a mail's code went to job_id. False if another job already took it."""
        if not message_key:
            return True
        con = self._tx()
        try:
            row = con.execute(
                "SELECT job_id FROM otp_consumed WHERE message_key = ?", (message_key,)
            ).fetchone()
            if row is not None and int(row[0]) != int(job_id):
                con.execute("COMMIT")
                return False
            con.execute(
                "INSERT OR IGNORE INTO otp_consumed(message_key, run_id, job_id, at) VALUES (?, ?, ?, ?)",
                (message_key, self.run_id, int(job_id), time.time()),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()

    def other_inflight_families(self, worker: str) -> set[str]:
        """ATS families other workers are on right now: their claimed jobs'
        resolved form URLs plus whatever their browsers actually show (a
        LinkedIn-route job may have left LinkedIn for an employer site)."""
        con = self._connect()
        try:
            fams = {
                r[0] for r in con.execute(
                    "SELECT family FROM queue WHERE run_id = ? AND state = 'claimed' AND worker != ? "
                    "AND family IS NOT NULL",
                    (self.run_id, worker),
                ) if r[0]
            }
            ports = [
                int(r[0]) for r in con.execute(
                    "SELECT cdp_port FROM workers WHERE run_id = ? AND state = 'running' AND worker != ? "
                    "AND cdp_port IS NOT NULL",
                    (self.run_id, worker),
                )
            ]
        finally:
            con.close()
        for port in ports:
            for url in _page_urls(port):
                fam = family_for_url(url)
                if fam and fam not in ("linkedin", "google"):
                    fams.add(fam)
        return fams


def _page_urls(port: int) -> list[str]:
    """Read-only tab list of another worker's browser; [] on any failure."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=3) as r:
            tabs = json.loads(r.read().decode("utf-8"))
        return [t.get("url", "") for t in tabs if t.get("type") == "page"]
    except Exception:
        return []
