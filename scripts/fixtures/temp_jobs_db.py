"""A throwaway job database for tests that otherwise read data/jobs.db.

data/ is never committed, so a fresh clone has no job database. Tests that
check queue rules call use_temp_db_if_missing() first:
with a real data/jobs.db nothing changes; without one, db.database is pointed
at a temp SQLite file seeded with the rows those tests look for (blocked
employers that would otherwise be queued for apply and triage).
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine

import db.database as database
from db.models import Base, JobModel

# (id, company) rows that every queue would pick up if the blocklist failed.
SEED = [
    (101, "Globex"),
    (102, "Globex Solutions"),
    (595, "Initech"),
    (596, "Initech"),
    (700, "Example Corp"),
]


def _row(jid: int, company: str, *, triaged: bool) -> JobModel:
    now = datetime(2026, 10, 1, 10, 0, 0)
    return JobModel(
        id=jid,
        company_name=company,
        job_title=f"Backend Engineer {jid}",
        job_url=f"https://www.linkedin.com/jobs/view/{jid}/",
        applied=False,
        referred_json="[]",
        first_seen_at=now,
        last_seen_at=now,
        metadata_json="{}",
        module2_attempted=True,
        module2_enriched_at=now,
        should_apply=True if triaged else None,
        module4_attempts=0,
    )


def use_temp_db_if_missing(*, always: bool = False) -> Path | None:
    """Point db.database at a seeded temp DB when data/jobs.db is absent.

    Returns the temp path, or None when the real database exists (then
    nothing is changed)."""
    if database.DB_PATH.is_file() and not always:
        return None
    tmp = Path(tempfile.mkdtemp(prefix="jobo-testdb-")) / "jobs.db"
    engine = create_engine(f"sqlite:///{tmp}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    database.engine = engine
    database.SessionLocal.configure(bind=engine)
    session = database.SessionLocal()
    try:
        for jid, company in SEED:
            session.add(_row(jid, company, triaged=True))
        # one untriaged twin per blocked employer, for the module3 queue
        for jid, company in ((103, "Globex"), (597, "Initech")):
            session.add(_row(jid, company, triaged=False))
        session.commit()
    finally:
        session.close()
    print(f"(no data/jobs.db: using a seeded temp database at {tmp})")
    return tmp
