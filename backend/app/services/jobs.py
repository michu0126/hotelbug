import hashlib
import json
import random
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import CrawlJob, utcnow
from app.schemas.domain import JobInput, JobKind

LIVE = ("PENDING", "QUEUED", "RUNNING")


def within_monitoring_window(check_in: date | None, check_out: date | None) -> bool:
    today = date.today()
    return bool(check_in and check_out and today <= check_in < check_out <= today + timedelta(days=365))


async def enqueue(session: AsyncSession, job: JobInput, delay_seconds: float = 0) -> CrawlJob:
    if job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
        if not within_monitoring_window(job.check_in, job.check_out):
            raise ValueError("Stay must be within the next 365 days")
    data = job.model_dump(mode="json")
    identity = {k: v for k, v in data.items() if k != "priority"}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    existing = await session.scalar(
        select(CrawlJob).where(CrawlJob.dedupe_key == key, CrawlJob.status.in_(LIVE))
    )
    if existing:
        return existing
    row = CrawlJob(
        **job.model_dump(), dedupe_key=key, scheduled_at=utcnow() + timedelta(seconds=delay_seconds)
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        row = await session.scalar(
            select(CrawlJob).where(CrawlJob.dedupe_key == key, CrawlJob.status.in_(LIVE))
        )
        if row is None:
            raise
    return row


def backoff(retries: int, retry_after: int | None = None) -> float:
    return max(retry_after or 0, min(3600, 30 * 2 ** min(retries, 7)) + random.uniform(0, 10))


def tier(offset: int) -> str:
    if not 0 <= offset <= 365:
        raise ValueError("date offset outside monitoring window")
    return "HOT" if offset <= 30 else "WARM" if offset <= 90 else "COLD"
