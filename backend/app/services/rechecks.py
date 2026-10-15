"""Revisit known dates and repair missing observations without a full year sweep."""

from datetime import date, timedelta

from sqlalchemy import case, func, or_, select

from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.capacity import load_capacity
from app.services.catalog_price_source import load_catalog_price_source
from app.services.failed_stays import expand_failed_stays
from app.services.jobs import LIVE, enqueue, tier
from app.services.revisit_rotation import fair_revisit_rows


async def expand_rechecks(session, settings, enabled, budget, *, capacity=None):
    """Share the existing revisit budget fairly; unused slots serve the other lane."""
    if budget < 1 or not enabled:
        return 0
    capacity = capacity or await load_capacity(session, settings)
    budget = min(budget, capacity.available_for(*enabled))
    if budget < 1:
        return 0
    repair_budget = budget // 2
    if budget == 1:
        lane = await session.get(AppSetting, "global_revisit_single_slot_lane")
        repair = not bool(lane.value.get("repair_last") if lane else False)
        if lane:
            lane.value = {"repair_last": repair}
        else:
            session.add(AppSetting(key="global_revisit_single_slot_lane", value={"repair_last": repair}))
        repair_budget = int(repair)
    repaired = await expand_failed_stays(session, settings, enabled, repair_budget, capacity=capacity)
    revisited = await expand_successful_rechecks(
        session, settings, enabled, budget - repaired, capacity=capacity
    )
    spare = budget - repaired - revisited
    if spare:
        repaired += await expand_failed_stays(session, settings, enabled, spare, capacity=capacity)
    return repaired + revisited


async def expand_successful_rechecks(session, settings, enabled, budget, *, capacity=None):
    if budget < 1:
        return 0
    capacity = capacity or await load_capacity(session, settings)
    enabled = [provider for provider in enabled if capacity.available_for(provider)]
    budget = min(budget, capacity.available_for(*enabled))
    if not enabled or budget < 1:
        return 0
    today, now = date.today(), utcnow()
    hot_end, warm_end = today + timedelta(days=30), today + timedelta(days=90)
    hot_cutoff = now - timedelta(hours=settings.hot_interval_hours)
    warm_cutoff = now - timedelta(hours=settings.warm_interval_hours)
    cold_cutoff = now - timedelta(hours=settings.cold_interval_hours)
    cutoff = case(
        (StayScan.check_in <= hot_end, hot_cutoff),
        (StayScan.check_in <= warm_end, warm_cutoff),
        else_=cold_cutoff,
    )
    attempted = (
        select(CrawlJob.id)
        .where(
            CrawlJob.hotel_id == StayScan.hotel_id,
            CrawlJob.check_in == StayScan.check_in,
            CrawlJob.check_out == StayScan.check_out,
            CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
            func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == StayScan.adults,
            func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == StayScan.rooms,
            or_(CrawlJob.status.in_(LIVE), CrawlJob.created_at > cutoff),
        )
        .correlate(StayScan)
        .exists()
    )
    query = (
        select(StayScan, Hotel)
        .join(Hotel, Hotel.id == StayScan.hotel_id)
        .outerjoin(ProviderStatus, ProviderStatus.provider == Hotel.provider)
        .where(
            Hotel.active.is_(True),
            Hotel.provider.in_(enabled),
            or_(ProviderStatus.blocked_until.is_(None), ProviderStatus.blocked_until <= now),
            StayScan.check_in >= today,
            StayScan.check_in <= today + timedelta(days=364),
            StayScan.check_out <= today + timedelta(days=365),
            StayScan.adults == 2,
            StayScan.rooms == 1,
            StayScan.status.in_(("AVAILABLE", "UNAVAILABLE", "NOT_OPEN")),
            # A constant upper bound also permits an observation-time index
            # range scan; the per-date CASE below enforces the exact tier.
            StayScan.observed_at <= hot_cutoff,
            StayScan.observed_at <= cutoff,
            ~attempted,
        )
    )
    source = await load_catalog_price_source(session)
    query = query.where(source.hotel_predicate())
    rows, remember = await fair_revisit_rows(
        session,
        query,
        StayScan,
        providers=enabled,
        key="global_recheck_cursor",
        time_field="observed_at",
        id_field="scan_id",
        limit=budget * 4,
    )
    created = 0
    for scan, hotel in rows:
        if created >= budget:
            break
        if not capacity.available_for(hotel.provider):
            continue
        remember(hotel.provider, {"observed_at": scan.observed_at.isoformat(), "scan_id": scan.id})
        if (scan.check_out - scan.check_in).days != 1:
            continue
        temperature = tier((scan.check_in - today).days)
        await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=scan.check_in,
                check_out=scan.check_out,
                priority={"HOT": 80, "WARM": 60, "COLD": 40}[temperature],
                payload={"adults": scan.adults, "rooms": scan.rooms, **source.payload(hotel.provider)},
            ),
        )
        capacity.consume(hotel.provider)
        created += 1
    return created
