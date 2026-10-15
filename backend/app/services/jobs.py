import hashlib
import json
import random
from datetime import date, timedelta

from sqlalchemy import case, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import AppSetting, CrawlJob, utcnow
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
                    JobKind.FETCH_CALENDAR_HINTS,
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


async def fair_group_head(session: AsyncSession, query, provider, selected):
    """Pin one fair head until claimed; alternate discovery and ordinary work.

    Rotating on every tick can still starve a lane when a slow Worker always
    pops at the same phase. SQL keeps the head stable across Redis loss/restarts
    until it runs, completes, or is no longer due. Original priorities remain.
    """
    query = query.where(CrawlJob.provider == provider, CrawlJob.id.not_in(selected))
    key = "dispatch_head:" + provider
    cursor = await session.get(AppSetting, key)
    value = cursor.value if cursor and isinstance(cursor.value, dict) else {}
    pinned_id = value.get("job_id")
    if isinstance(pinned_id, str):
        pinned = (await session.execute(query.where(CrawlJob.id == pinned_id).limit(1))).first()
        if pinned:
            return pinned
    catalogs = (JobKind.DISCOVER_CATALOG, JobKind.DISCOVER_HOTELS)
    lane = value.get("lane")
    preferred = (
        query.where(CrawlJob.kind.not_in(catalogs))
        if lane == "catalog"
        else query.where(CrawlJob.kind.in_(catalogs))
        if lane == "ordinary"
        else None
    )
    row = (await session.execute(preferred.limit(1))).first() if preferred is not None else None
    if row is None:
        row = (await session.execute(query.limit(1))).first()
    if row:
        next_value = {"job_id": row[0].id, "lane": "catalog" if row[0].kind in catalogs else "ordinary"}
        if cursor:
            cursor.value = next_value
        else:
            session.add(AppSetting(key=key, value=next_value))
    return row


async def due_dispatch_jobs(session: AsyncSession, now, providers, limit: int = 100):
    """Replay a bounded due batch without hiding small groups behind a big queue.

    Confirmations keep their highest-priority lane. Then reserve one pinned
    fair head per known group and fill remaining capacity by the usual priority
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
        reserved = await fair_group_head(session, query, provider, selected)
        if reserved:
            rows.append(reserved)
            selected.add(reserved[0].id)
    if len(rows) < limit:
        rows.extend(
            (await session.execute(query.where(CrawlJob.id.not_in(selected)).limit(limit - len(rows)))).all()
        )
    return rows


async def reserve_provider_turn(session: AsyncSession, rows) -> str | None:
    """Pin one eligible group head until claimed, then advance to the next group.

    Equal Redis scores are ordered by job ID, not insertion time. Replenishing
    the earliest-sorting group on each fast Scheduler tick can therefore starve
    the other pinned heads when a slow Worker claims only one job per tick.
    The caller supplies only currently eligible jobs; confirmations retain their
    separate priority and do not advance the ordinary-work turn.
    """
    heads = {}
    for job, priority in rows:
        if priority < 100:
            heads.setdefault(job.provider, job.id)
    if not heads:
        return None
    cursor = await session.get(AppSetting, "dispatch_provider_turn")
    value = cursor.value if cursor and isinstance(cursor.value, dict) else {}
    previous = value.get("provider", "")
    if not isinstance(previous, str):
        previous = ""
    pinned_id = value.get("job_id")
    if isinstance(pinned_id, str) and heads.get(previous) == pinned_id:
        return pinned_id
    providers = sorted(heads)
    provider = next((name for name in providers if name > previous), providers[0])
    value = {"provider": provider, "job_id": heads[provider]}
    if cursor:
        cursor.value = value
    else:
        session.add(AppSetting(key="dispatch_provider_turn", value=value))
    return heads[provider]


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
    if len(rows) == limit:
        return len(rows)
    # Month tasks deliberately have no single check-in date. Retire expired
    # or malformed months even when their provider pause is still in future.
    from app.schemas.calendar_hints import SHANGHAI

    local_day = utcnow().astimezone(SHANGHAI).date()
    month = local_day.replace(day=1)
    last_day = local_day + timedelta(days=364)
    months = []
    while month <= last_day:
        months.append(month.strftime("%Y-%m"))
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    task_month = CrawlJob.payload["month"].as_string()
    hint_rows = (
        await session.scalars(
            select(CrawlJob)
            .where(
                CrawlJob.status.in_(("PENDING", "QUEUED")),
                CrawlJob.kind == JobKind.FETCH_CALENDAR_HINTS,
                or_(task_month.is_(None), task_month.not_in(months)),
            )
            .order_by(CrawlJob.created_at, CrawlJob.id)
            .limit(limit - len(rows))
            .with_for_update(skip_locked=True)
        )
    ).all()
    for job in hint_rows:
        cancel_outside_window(job)
    return len(rows) + len(hint_rows)


async def enqueue(session: AsyncSession, job: JobInput, delay_seconds: float = 0) -> CrawlJob:
    if job.kind == JobKind.FETCH_CALENDAR_HINTS:
        from app.schemas.calendar_hints import month_window

        if month_window(job.payload.get("month")) is None:
            raise ValueError("Calendar month must overlap the next 365 days")
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
