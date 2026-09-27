"""Bounded, rotating watchlist expansion into independently throttled nightly jobs."""

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, Watchlist, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue, tier


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
        offset = cursor.value.get("offset", 0) if cursor else 0
        if type(offset) is not int or not 0 <= offset <= horizon:
            offset = 0
        if cursor:
            cursor.value = {"offset": (offset + 1) % (horizon + 1)}
        else:
            session.add(AppSetting(key=cursor_key, value={"offset": (offset + 1) % (horizon + 1)}))
        check_in = date.today() + timedelta(days=offset)
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
