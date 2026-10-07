"""Revisit known dates and repair missing observations without a full year sweep."""

from datetime import date, datetime, timedelta

from sqlalchemy import and_, case, func, or_, select

from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.failed_stays import expand_failed_stays
from app.services.jobs import LIVE, enqueue, tier


async def expand_rechecks(session, settings, enabled, budget):
    """Share the existing revisit budget fairly; unused slots serve the other lane."""
    if budget < 1 or not enabled:
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
    repaired = await expand_failed_stays(session, settings, enabled, repair_budget)
    revisited = await expand_successful_rechecks(session, settings, enabled, budget - repaired)
    spare = budget - repaired - revisited
    if spare:
        repaired += await expand_failed_stays(session, settings, enabled, spare)
    return repaired + revisited


async def expand_successful_rechecks(session, settings, enabled, budget):
    if budget < 1:
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
    state = await session.get(AppSetting, "global_recheck_cursor")
    value = dict(state.value) if state else {}
    cursor_time = value.get("observed_at")
    try:
        cursor_time = datetime.fromisoformat(cursor_time) if isinstance(cursor_time, str) else None
    except ValueError:
        cursor_time = None
    cursor_id = value.get("scan_id", "")
    if not isinstance(cursor_id, str):
        cursor_id = ""
    after = (
        or_(
            StayScan.observed_at > cursor_time,
            and_(StayScan.observed_at == cursor_time, StayScan.id > cursor_id),
        )
        if cursor_time
        else True
    )
    ordered = query.order_by(StayScan.observed_at, StayScan.id)
    # Bound inspection too. The durable keyset advances past non-nightly rows
    # rather than repeatedly stopping at the first unsupported legacy stay.
    rows = (await session.execute(ordered.where(after).limit(budget * 4))).all()
    if not rows and cursor_time is not None:
        rows = (await session.execute(ordered.limit(budget * 4))).all()
    created = 0
    for scan, hotel in rows:
        if created >= budget:
            break
        value = {"observed_at": scan.observed_at.isoformat(), "scan_id": scan.id}
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
                payload={"adults": scan.adults, "rooms": scan.rooms},
            ),
        )
        created += 1
    if state:
        state.value = value
    elif rows:
        session.add(AppSetting(key="global_recheck_cursor", value=value))
    return created
