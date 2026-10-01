"""Bounded, rotating watchlist expansion into independently throttled nightly jobs."""

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, Watchlist, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue, tier


def advance_date_cursor(value: dict, today: date, horizon: int) -> tuple[date, dict]:
    """Keep an absolute next date so crossing midnight does not skip unvisited nights."""
    last_day = today + timedelta(days=horizon)
    stored_date = value.get("next_check_in")
    try:
        check_in = date.fromisoformat(stored_date) if isinstance(stored_date, str) else None
    except ValueError:
        check_in = None
    if check_in is None:
        # Migrate existing offset-only cursors without discarding their progress.
        offset = value.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= horizon:
            offset = 0
        check_in = today + timedelta(days=offset)
    if not today <= check_in <= last_day:
        check_in = today
    next_day = check_in + timedelta(days=1)
    if next_day > last_day:
        next_day = today
    return check_in, {"next_check_in": next_day.isoformat(), "offset": (next_day - today).days}


async def expand_watchlists(session: AsyncSession, settings: Settings, budget: int = 4) -> int:
    if budget < 1:
        return 0
    now = utcnow()
    watches = (
        await session.scalars(
            select(Watchlist)
            .where(Watchlist.enabled.is_(True), Watchlist.scope == "hotel")
            .order_by(Watchlist.created_at, Watchlist.id)
            .limit(100)
        )
    ).all()
    if not watches:
        return 0
    rotation = await session.get(AppSetting, "watchlist_rotation")
    start = rotation.value.get("index", 0) if rotation else 0
    if type(start) is not int or not 0 <= start < len(watches):
        start = 0
    indexed = list(enumerate(watches))
    ordered = indexed[start:] + indexed[:start]
    created = 0
    next_index = start
    for index, watch in ordered:
        next_index = (index + 1) % len(watches)
        if created >= budget:
            break
        hotel_id = watch.filters.get("hotel_id")
        horizon = watch.filters.get("days_ahead", 30)
        if not isinstance(hotel_id, str) or type(horizon) is not int or not 1 <= horizon <= 365:
            continue
        horizon = min(horizon, 364)
        hotel = await session.get(Hotel, hotel_id)
        if not hotel or not hotel.active or hotel.provider not in FACTORIES:
            continue
        if not settings.provider_policy(hotel.provider).enabled:
            continue
        state = await session.get(ProviderStatus, hotel.provider)
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            continue
        cursor_key = f"watch_cursor:{watch.id}"
        cursor = await session.get(AppSetting, cursor_key)
        today = date.today()
        check_in, next_value = advance_date_cursor(cursor.value if cursor else {}, today, horizon)
        if cursor:
            cursor.value = next_value
        else:
            session.add(AppSetting(key=cursor_key, value=next_value))
        offset = (check_in - today).days
        kind = tier(offset)
        hours = {
            "HOT": settings.hot_interval_hours,
            "WARM": settings.warm_interval_hours,
            "COLD": settings.cold_interval_hours,
        }[kind]
        # Failed and queued requests count as recent attempts; never flood a failing website.
        recent = await session.scalar(
            select(CrawlJob.id)
            .where(
                CrawlJob.hotel_id == hotel.id,
                CrawlJob.check_in == check_in,
                CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR)),
                CrawlJob.created_at >= now - timedelta(hours=hours),
            )
            .limit(1)
        )
        if recent:
            continue
        await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_in + timedelta(days=1),
                priority={"HOT": 80, "WARM": 60, "COLD": 40}[kind],
            ),
        )
        created += 1
    if rotation:
        rotation.value = {"index": next_index}
    else:
        session.add(AppSetting(key="watchlist_rotation", value={"index": next_index}))
    return created


async def expand_global(session: AsyncSession, settings: Settings) -> int:
    """Fair keyset rotation across all catalog hotels, with a persistent date cursor."""
    if not settings.global_monitoring_enabled:
        return 0
    enabled = [name for name in FACTORIES if settings.provider_policy(name).enabled]
    if not enabled:
        return 0
    pending = await session.scalar(
        select(func.count())
        .select_from(CrawlJob)
        .where(CrawlJob.status.in_(("PENDING", "QUEUED", "RUNNING")))
    )
    budget = min(settings.global_jobs_per_tick, max(0, settings.max_pending_jobs - pending))
    if not budget:
        return 0
    rotation = await session.get(AppSetting, "global_hotel_cursor")
    last_id = rotation.value.get("hotel_id", "") if rotation else ""
    query = select(Hotel).where(Hotel.active.is_(True), Hotel.provider.in_(enabled))
    hotels = (await session.scalars(query.where(Hotel.id > last_id).order_by(Hotel.id).limit(budget))).all()
    if not hotels:
        hotels = (await session.scalars(query.order_by(Hotel.id).limit(budget))).all()
    now = utcnow()
    created = 0
    for hotel in hotels:
        last_id = hotel.id
        state = await session.get(ProviderStatus, hotel.provider)
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            continue
        cursor_key = f"global_date_cursor:{hotel.id}"
        cursor = await session.get(AppSetting, cursor_key)
        today = date.today()
        check_in, next_value = advance_date_cursor(cursor.value if cursor else {}, today, 364)
        if cursor:
            cursor.value = next_value
        else:
            session.add(AppSetting(key=cursor_key, value=next_value))
        offset = (check_in - today).days
        temperature = tier(offset)
        hours = {
            "HOT": settings.hot_interval_hours,
            "WARM": settings.warm_interval_hours,
            "COLD": settings.cold_interval_hours,
        }[temperature]
        recent = await session.scalar(
            select(CrawlJob.id)
            .where(
                CrawlJob.hotel_id == hotel.id,
                CrawlJob.check_in == check_in,
                CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR)),
                CrawlJob.created_at >= now - timedelta(hours=hours),
            )
            .limit(1)
        )
        if recent:
            continue
        await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_in + timedelta(days=1),
                priority={"HOT": 80, "WARM": 60, "COLD": 40}[temperature],
            ),
        )
        created += 1
    if rotation:
        rotation.value = {"hotel_id": last_id}
    else:
        session.add(AppSetting(key="global_hotel_cursor", value={"hotel_id": last_id}))
    return created
