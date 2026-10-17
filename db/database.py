import os
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base

# JOBO_DB_PATH points every stage at another database file (a copy for dry
# runs and end-to-end checks). Unset = the live data/jobs.db.
DB_PATH = Path(os.environ.get("JOBO_DB_PATH") or Path(__file__).parent.parent / "data" / "jobs.db")
DB_URL = f"sqlite:///{DB_PATH}"


engine = create_engine(
    DB_URL,
    # timeout: a parallel apply run (module4/parallel.py) has several worker
    # processes writing job rows; wait for the lock instead of failing.
    connect_args={"check_same_thread": False, "timeout": 30},
    poolclass=StaticPool,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def _ensure_jobs_module2_attempted_column() -> None:
    """Add module2_attempted to existing SQLite DBs created before that column existed."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "module2_attempted" in columns:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "ALTER TABLE jobs ADD COLUMN module2_attempted BOOLEAN NOT NULL DEFAULT 0"
            )
        )


def _ensure_jobs_module2_enrichment_columns() -> None:
    """Add Module 2 provenance columns (enriched_at, model, last_error)."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    alters: list[str] = []
    if "module2_enriched_at" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module2_enriched_at DATETIME")
    if "module2_model" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module2_model VARCHAR")
    if "module2_last_error" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module2_last_error TEXT")
    if not alters:
        return
    with engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))


def _ensure_jobs_module3_columns() -> None:
    """Add Module 3 triage columns (fit, paths)."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    alters: list[str] = []
    if "should_apply" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN should_apply BOOLEAN")
    if "module3_fit_score" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module3_fit_score INTEGER")
    if "module3_fit_reasoning" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module3_fit_reasoning TEXT")
    if "module3_highlighted_projects" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module3_highlighted_projects TEXT")
    if "module3_resume_pdf_path" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module3_resume_pdf_path TEXT")
    if "module3_cover_pdf_path" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module3_cover_pdf_path TEXT")
    if not alters:
        return
    with engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))


def _ensure_jobs_module4_columns() -> None:
    """Add Module 4 submission columns."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    alters: list[str] = []
    if "module4_last_status" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module4_last_status VARCHAR")
    if "module4_submitted_at" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module4_submitted_at DATETIME")
    if "module4_attempts" not in columns:
        alters.append(
            "ALTER TABLE jobs ADD COLUMN module4_attempts INTEGER NOT NULL DEFAULT 0"
        )
    if "module4_closed_at" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module4_closed_at DATETIME")
    if "module4_outcome" not in columns:
        alters.append("ALTER TABLE jobs ADD COLUMN module4_outcome VARCHAR")
    if not alters:
        return
    with engine.begin() as conn:
        for stmt in alters:
            conn.execute(text(stmt))


def _ensure_jobs_prefilter_column() -> None:
    """Add the post-enrich rule-filter column (policy/job_filters.py)."""
    inspector = inspect(engine)
    if not inspector.has_table("jobs"):
        return
    columns = {c["name"] for c in inspector.get_columns("jobs")}
    if "prefilter_reason" in columns:
        return
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs ADD COLUMN prefilter_reason VARCHAR"))


def _ensure_source_state_table() -> None:
    """Per-source collection cursor (sources/jobfound.py)."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS source_state ("
                "source TEXT PRIMARY KEY, cursor TEXT, updated_at DATETIME)"
            )
        )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_jobs_module2_attempted_column()
    _ensure_jobs_module2_enrichment_columns()
    _ensure_jobs_module3_columns()
    _ensure_jobs_module4_columns()
    _ensure_jobs_prefilter_column()
    _ensure_source_state_table()


def get_session() -> Session:
    return SessionLocal()
