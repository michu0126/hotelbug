import hashlib
import json
import random
from datetime import date, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import CrawlJob, utcnow
from app.schemas.domain import JobInput, JobKind

LIVE = ("PENDING", "QUEUED", "RUNNING")
CATALOG_PRIORITY = 70  # Below watched/near-stay prices; age promotion still prevents starvation.
OUTSIDE_WINDOW_MESSAGE = "Stay outside the next 365 days"


def dispatch_priority(now):
    """Bounded age promotion: old calendar/catalog work must not starve forever.

    Keep independently verified anomaly requests above the aged lane. This is
    a dispatch score only; original task priority and retry timing are retained.
    """
    return case(
        (
            CrawlJob.kind.in_(
                (
                    JobKind.FETCH_RATE,
                    JobKind.FETCH_CALENDAR,
                    JobKind.DISCOVER_HOTELS,
                    JobKind.DISCOVER_CATALOG,
                )
            )
            & (CrawlJob.scheduled_at <= now - timedelta(hours=48))
            & (CrawlJob.priority < 90),
            90,
        ),
        else_=CrawlJob.priority,
    )


async def due_dispatch_jobs(session: AsyncSession, now, providers, limit: int = 100):
    """Replay a bounded due batch without hiding small groups behind a big queue.

    Confirmations keep their highest-priority lane. Then reserve one best due
    job per known group and fill all remaining capacity by the usual priority
    and age order. Effective Redis priorities/original job priorities stay intact.
    Each read locks only selected jobs and skips an active Worker's row locks.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("Dispatch batch must be between 1 and 1000")
    score = dispatch_priority(now)
    query = (
        select(CrawlJob, score)
        .where(CrawlJob.status.in_(("PENDING", "QUEUED")), CrawlJob.scheduled_at <= now)
        .order_by(score.desc(), CrawlJob.scheduled_at, CrawlJob.id)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(query.where(score >= 100).limit(limit))).all())
    selected = {job.id for job, _ in rows}
    for provider in sorted(set(providers)):
        if len(rows) >= limit:
            break
        reserved = (
            await session.execute(
                query.where(CrawlJob.provider == provider, CrawlJob.id.not_in(selected)).limit(1)
            )
        ).first()
        if reserved:
            rows.append(reserved)
            selected.add(reserved[0].id)
    if len(rows) < limit:
        rows.extend(
            (await session.execute(query.where(CrawlJob.id.not_in(selected)).limit(limit - len(rows)))).all()
        )
    return rows


def within_monitoring_window(check_in: date | None, check_out: date | None) -> bool:
    today = date.today()
    return bool(check_in and check_out and today <= check_in < check_out <= today + timedelta(days=365))


def cancel_outside_window(job: CrawlJob) -> None:
    """Retire pending work without erasing its original retry/failure evidence."""
    job.status = "CANCELLED"
    if not outside_window_cancellation(job.error_message):
        job.error_message = ((job.error_message + "; ") if job.error_message else "") + OUTSIDE_WINDOW_MESSAGE
    job.lease_owner, job.lease_until = None, None


def outside_window_cancellation(message: str | None) -> bool:
    return bool(
        message and (message == OUTSIDE_WINDOW_MESSAGE or message.endswith("; " + OUTSIDE_WINDOW_MESSAGE))
    )


async def expire_pending_stays(session: AsyncSession, limit: int = 100) -> int:
    """Free capacity at midnight/restart, including future-deferred paused jobs.

    Never cancels an active Worker or changes provider pauses/history/outbox.
    Stale Redis references remain harmless: Worker rechecks the DB status.
    """
    if not 1 <= limit <= 1000:
        raise ValueError("Expiry batch must be between 1 and 1000")
    today = date.today()
    rows = (
        await session.scalars(
            select(CrawlJob)
            .where(
                CrawlJob.status.in_(("PENDING", "QUEUED")),
                CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
                or_(
                    CrawlJob.check_in.is_(None),
                    CrawlJob.check_out.is_(None),
                    CrawlJob.check_in < today,
                    CrawlJob.check_out <= CrawlJob.check_in,
                    CrawlJob.check_out > today + timedelta(days=365),
                ),
            )
            .order_by(CrawlJob.check_in, CrawlJob.created_at, CrawlJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    for job in rows:
        cancel_outside_window(job)
    return len(rows)


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
