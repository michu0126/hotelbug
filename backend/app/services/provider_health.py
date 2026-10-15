"""Use recent successful production price scans before scheduling extra probes."""

from datetime import date, datetime, timedelta
from urllib.parse import urlparse

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan
from app.schemas.domain import JobKind
from app.services.jobs import LIVE


async def marriott_probe_origin(session: AsyncSession) -> str:
    """Use the accepted directory source, not a fallback after a rejection."""
    from app.services.ihg_catalogs import selected_directory_root

    state = await session.get(AppSetting, "catalog:marriott")
    root = selected_directory_root(state.value if state else {}, "marriott")
    return "https://" + urlparse(root).hostname


async def marriott_probe_hotel(session: AsyncSession, hotel_id: str | None = None) -> Hotel | None:
    query = select(Hotel).where(
        Hotel.provider == "marriott",
        Hotel.active.is_(True),
        Hotel.official_url.like((await marriott_probe_origin(session)) + "/%"),
    )
    if hotel_id is not None:
        query = query.where(Hotel.id == hotel_id)
    return await session.scalar(query.order_by(Hotel.id).limit(1))


async def ordinary_price_work_due(session: AsyncSession, provider: str, now: datetime) -> bool:
    """Let normal, eligible stay work prove health before adding a fixed probe."""
    today = date.today()
    query = (
        select(CrawlJob.id)
        .join(Hotel, Hotel.id == CrawlJob.hotel_id)
        .where(
            CrawlJob.provider == provider,
            Hotel.provider == provider,
            Hotel.active.is_(True),
            CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
            CrawlJob.status.in_(LIVE),
            CrawlJob.scheduled_at <= now,
            CrawlJob.check_in >= today,
            CrawlJob.check_out > CrawlJob.check_in,
            CrawlJob.check_out <= today + timedelta(days=365),
        )
    )
    if provider == "marriott":
        query = query.where(Hotel.official_url.like((await marriott_probe_origin(session)) + "/%"))
    return await session.scalar(query.limit(1)) is not None


async def scheduled_probe_cancellation(session: AsyncSession, job: CrawlJob, now: datetime) -> str | None:
    """Recheck obsolete automatic probes at claim time; manual checks are untouched."""
    if await recent_healthy_scan(session, job.provider, now):
        return "Recent successful rate scan; redundant scheduled health probe"
    if await ordinary_price_work_due(session, job.provider, now):
        return "Eligible ordinary price work; redundant scheduled health probe"
    if job.provider == "marriott" and (
        not job.hotel_id or await marriott_probe_hotel(session, job.hotel_id) is None
    ):
        return "Scheduled Marriott probe has no active hotel on the selected directory source"
    return None


async def recent_healthy_scan(session: AsyncSession, provider: str, now: datetime) -> bool:
    state = await session.get(ProviderStatus, provider)
    if not state or state.status != "ONLINE":
        return False
    if state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
        return False
    # A heartbeat, directory page or ONLINE flag by itself is not price evidence.
    # The scan must belong to an actually successful price task for this stay.
    query = (
        select(StayScan.id)
        .join(CrawlJob, CrawlJob.id == StayScan.job_id)
        .join(Hotel, Hotel.id == StayScan.hotel_id)
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
    )
    if provider == "marriott":
        query = query.where(
            Hotel.active.is_(True),
            Hotel.provider == provider,
            Hotel.official_url.like((await marriott_probe_origin(session)) + "/%"),
        )
    return await session.scalar(query.limit(1)) is not None


async def healthcheck_due(session: AsyncSession, provider: str, now: datetime) -> bool:
    state = await session.get(ProviderStatus, provider)
    if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
        return False
    if provider == "marriott" and await marriott_probe_hotel(session) is None:
        return False
    if await ordinary_price_work_due(session, provider, now):
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
