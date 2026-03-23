from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from db.models import Base

DB_PATH = Path(__file__).parent.parent / "data" / "jobs.db"
DB_URL = f"sqlite:///{DB_PATH}"


engine = create_engine(
    DB_URL,
    connect_args={"check_same_thread": False},
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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _ensure_jobs_module2_attempted_column()
    _ensure_jobs_module2_enrichment_columns()


def get_session() -> Session:
    return SessionLocal()
