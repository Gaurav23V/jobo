from typing import Optional

from db.models import Job, JobModel


def get_all_jobs(session) -> list[Job]:
    jobs = session.query(JobModel).all()
    return [j.to_job() for j in jobs]


def upsert_job(session, job: Job) -> tuple[Job, bool]:
    """
    Insert or update a job.
    Returns (job, created) where created is True if new, False if updated.
    """
    existing = (
        session.query(JobModel).filter(JobModel.job_url == job.job_url).first()
    )

    if existing:
        existing.last_seen_at = job.last_seen_at
        session.commit()
        return existing.to_job(), False

    model = JobModel(
        company_name=job.company_name,
        job_title=job.job_title,
        location=job.location,
        applied=job.applied,
        referred_json=job.referred_json,
        date_released=job.date_released,
        job_url=job.job_url,
        source_platform=job.source_platform,
        first_seen_at=job.first_seen_at,
        last_seen_at=job.last_seen_at,
        metadata_json=job.metadata_json,
    )
    session.add(model)
    session.commit()
    return model.to_job(), True
