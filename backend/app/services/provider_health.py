"""Use recent successful production price scans before scheduling extra probes."""

from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import CrawlJob, ProviderStatus, StayScan
from app.schemas.domain import JobKind
from app.services.jobs import LIVE


async def recent_healthy_scan(session: AsyncSession, provider: str, now: datetime) -> bool:
    state = await session.get(ProviderStatus, provider)
    if not state or state.status != "ONLINE":
        return False
    if state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
        return False
    # A heartbeat, directory page or ONLINE flag by itself is not price evidence.
    # The scan must belong to an actually successful price task for this stay.
    scan = await session.scalar(
        select(StayScan.id)
        .join(CrawlJob, CrawlJob.id == StayScan.job_id)
        .where(
            CrawlJob.provider == provider,
            CrawlJob.hotel_id == StayScan.hotel_id,
            CrawlJob.check_in == StayScan.check_in,
            CrawlJob.check_out == StayScan.check_out,
            CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
            CrawlJob.status == "SUCCEEDED",
            StayScan.status.in_(("AVAILABLE", "UNAVAILABLE", "NOT_OPEN")),
            StayScan.observed_at >= now - timedelta(hours=1),
            StayScan.observed_at <= now,
        )
        .limit(1)
    )
    return scan is not None


async def healthcheck_due(session: AsyncSession, provider: str, now: datetime) -> bool:
    state = await session.get(ProviderStatus, provider)
    if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
        return False
    existing = await session.scalar(
        select(CrawlJob.id)
        .where(
            CrawlJob.provider == provider,
            CrawlJob.kind == JobKind.PROVIDER_HEALTHCHECK,
            or_(CrawlJob.status.in_(LIVE), CrawlJob.created_at > now - timedelta(hours=1)),
        )
        .limit(1)
    )
    return existing is None and not await recent_healthy_scan(session, provider, now)
