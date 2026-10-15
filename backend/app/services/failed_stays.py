"""Repair unobserved stays after exhausted transient failures, without fake scans."""

from collections.abc import Sequence
from datetime import date, timedelta

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.core.errors import ErrorCode
from app.models.tables import CrawlJob, Hotel, ProviderStatus, StayScan, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.capacity import load_capacity
from app.services.catalog_price_source import load_catalog_price_source
from app.services.jobs import LIVE, enqueue, tier
from app.services.revisit_rotation import fair_revisit_rows

RATE_KINDS = (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)
TRANSIENT_FAILURES = (
    ErrorCode.TIMEOUT,
    ErrorCode.NETWORK_ERROR,
    ErrorCode.RATE_LIMITED,
    ErrorCode.BROWSER_STARTUP_FAILED,
)


async def expand_failed_stays(
    session: AsyncSession, settings: Settings, enabled: Sequence[str], budget: int, *, capacity=None
) -> int:
    """Only the latest exhausted attempt for a still-unobserved default stay.

    A normal new FETCH_RATE job starts a new bounded attempt cycle, not an
    endless reset of retry_count on the failed job. Old attempts remain auditable.
    Permanent parser/install/access failures do not get blindly replayed here.
    """
    if budget < 1 or not enabled:
        return 0
    capacity = capacity or await load_capacity(session, settings)
    enabled = [provider for provider in enabled if capacity.available_for(provider)]
    budget = min(budget, capacity.available_for(*enabled))
    if not enabled or budget < 1:
        return 0
    today, now = date.today(), utcnow()
    hot_end, warm_end = today + timedelta(days=30), today + timedelta(days=90)
    cutoff = case(
        (CrawlJob.check_in <= hot_end, now - timedelta(hours=settings.hot_interval_hours)),
        (CrawlJob.check_in <= warm_end, now - timedelta(hours=settings.warm_interval_hours)),
        else_=now - timedelta(hours=settings.cold_interval_hours),
    )
    newer = aliased(CrawlJob)
    matching_attempt = (
        newer.hotel_id == CrawlJob.hotel_id,
        newer.provider == CrawlJob.provider,
        newer.check_in == CrawlJob.check_in,
        newer.check_out == CrawlJob.check_out,
        newer.kind.in_(RATE_KINDS),
        func.coalesce(newer.payload["adults"].as_integer(), 2) == 2,
        func.coalesce(newer.payload["rooms"].as_integer(), 1) == 1,
    )
    active_attempt = (
        select(newer.id).where(*matching_attempt, newer.status.in_(LIVE)).correlate(CrawlJob).exists()
    )
    later_attempt = (
        select(newer.id)
        .where(
            *matching_attempt,
            or_(
                newer.created_at > CrawlJob.created_at,
                and_(newer.created_at == CrawlJob.created_at, newer.id > CrawlJob.id),
            ),
        )
        .correlate(CrawlJob)
        .exists()
    )
    observed = (
        select(StayScan.id)
        .where(
            StayScan.hotel_id == CrawlJob.hotel_id,
            StayScan.check_in == CrawlJob.check_in,
            StayScan.check_out == CrawlJob.check_out,
            StayScan.adults == 2,
            StayScan.rooms == 1,
        )
        .correlate(CrawlJob)
        .exists()
    )
    query = (
        select(CrawlJob, Hotel)
        .join(Hotel, Hotel.id == CrawlJob.hotel_id)
        .outerjoin(ProviderStatus, ProviderStatus.provider == Hotel.provider)
        .where(
            Hotel.active.is_(True),
            Hotel.provider.in_(enabled),
            CrawlJob.provider == Hotel.provider,
            or_(ProviderStatus.blocked_until.is_(None), ProviderStatus.blocked_until <= now),
            CrawlJob.kind.in_(RATE_KINDS),
            CrawlJob.status == "FAILED",
            CrawlJob.error_type.in_(TRANSIENT_FAILURES),
            CrawlJob.created_at <= cutoff,
            # Honour the final retry-after/backoff even after an early restart.
            CrawlJob.scheduled_at <= now,
            CrawlJob.check_in >= today,
            CrawlJob.check_in <= today + timedelta(days=364),
            CrawlJob.check_out <= today + timedelta(days=365),
            func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == 2,
            func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == 1,
            ~later_attempt,
            ~active_attempt,
            ~observed,
        )
    )
    source = await load_catalog_price_source(session)
    query = query.where(source.hotel_predicate())
    rows, remember = await fair_revisit_rows(
        session,
        query,
        CrawlJob,
        providers=enabled,
        key="global_failed_stay_cursor",
        time_field="created_at",
        id_field="job_id",
        limit=budget * 4,
    )
    created = 0
    for failed, hotel in rows:
        if created >= budget:
            break
        if not capacity.available_for(hotel.provider):
            continue
        remember(hotel.provider, {"created_at": failed.created_at.isoformat(), "job_id": failed.id})
        if (failed.check_out - failed.check_in).days != 1:
            continue
        temperature = tier((failed.check_in - today).days)
        await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=failed.check_in,
                check_out=failed.check_out,
                priority={"HOT": 80, "WARM": 60, "COLD": 40}[temperature],
                payload={"adults": 2, "rooms": 1, **source.payload(hotel.provider)},
            ),
        )
        capacity.consume(hotel.provider)
        created += 1
    return created
