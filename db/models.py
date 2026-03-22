from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, func
from sqlalchemy.orm import declarative_base

Base = declarative_base()


@dataclass
class Job:
    job_url: str
    company_name: Optional[str] = None
    job_title: Optional[str] = None
    location: Optional[str] = None
    applied: bool = False
    referred_json: str = "[]"
    date_released: Optional[datetime] = None
    source_platform: Optional[str] = None
    first_seen_at: datetime = field(default_factory=datetime.utcnow)
    last_seen_at: datetime = field(default_factory=datetime.utcnow)
    metadata_json: str = "{}"
    id: Optional[int] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "company_name": self.company_name,
            "job_title": self.job_title,
            "location": self.location,
            "applied": self.applied,
            "referred_json": self.referred_json,
            "date_released": self.date_released.isoformat()
            if self.date_released
            else None,
            "job_url": self.job_url,
            "source_platform": self.source_platform,
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_seen_at": self.last_seen_at.isoformat(),
            "metadata_json": self.metadata_json,
        }


class JobModel(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    company_name = Column(String, nullable=True)
    job_title = Column(String, nullable=True)
    location = Column(String, nullable=True)
    applied = Column(Boolean, nullable=False, default=False)
    referred_json = Column(Text, nullable=False, default="[]")
    date_released = Column(DateTime, nullable=True)
    job_url = Column(String, nullable=False)
    source_platform = Column(String, nullable=True)
    first_seen_at = Column(DateTime, nullable=False, default=func.now())
    last_seen_at = Column(DateTime, nullable=False, default=func.now())
    metadata_json = Column(Text, nullable=False, default="{}")

    def to_job(self) -> Job:
        return Job(
            id=self.id,
            company_name=self.company_name,
            job_title=self.job_title,
            location=self.location,
            applied=self.applied,
            referred_json=self.referred_json,
            date_released=self.date_released,
            job_url=self.job_url,
            source_platform=self.source_platform,
            first_seen_at=self.first_seen_at,
            last_seen_at=self.last_seen_at,
            metadata_json=self.metadata_json,
        )
