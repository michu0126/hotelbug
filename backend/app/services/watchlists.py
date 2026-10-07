"""Bounded, rotating watchlist expansion into independently throttled nightly jobs."""

from collections import deque
from datetime import date, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, Watchlist, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.schemas.watchlists import SCOPES
from app.services.jobs import LIVE, enqueue, tier
from app.services.rechecks import expand_rechecks
from app.services.watch_scopes import scoped_hotels


def advance_date_cursor(value: dict, today: date, horizon: int) -> tuple[date, dict]:
    """Preserve pending nights, without letting slow rotations starve the future.

    Normally an absolute date survives midnight. After two consecutive visits
    where the calendar catches up with that date, retain an independent rolling
    offset instead. This is scheduling progress, not evidence of price coverage.
    """
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
    frontier_index = value.get("frontier_index")
    if type(frontier_index) is not int or not 0 <= frontier_index <= horizon:
        frontier_index = (check_in - today).days
    try:
        window_day = date.fromisoformat(value.get("window_day", ""))
    except (ValueError, TypeError):
        window_day = today
    rolling = value.get("rolling_frontier") is True
    lag_count = value.get("lag_count", 0)
    if type(lag_count) is not int or not 0 <= lag_count <= 2:
        lag_count = 0
    if not rolling:
        if window_day < today and check_in <= today:
            lag_count = min(2, lag_count + 1)
        elif check_in > today:
            lag_count = 0
        rolling = lag_count == 2
    if rolling:
        check_in = today + timedelta(days=frontier_index)
    next_index = (frontier_index + 1) % (horizon + 1)
    next_day = today + timedelta(days=next_index) if rolling else check_in + timedelta(days=1)
    if next_day > last_day:
        next_day = today
    return check_in, {
        "next_check_in": next_day.isoformat(),
        "offset": (next_day - today).days,
        "frontier_index": next_index,
        "window_day": today.isoformat(),
        "rolling_frontier": rolling,
        "lag_count": lag_count,
    }


async def expand_watchlists(session: AsyncSession, settings: Settings, budget: int = 4) -> int:
    if budget < 1:
        return 0
    now = utcnow()
    pending = await session.scalar(
        select(func.count())
        .select_from(CrawlJob)
        .where(CrawlJob.status.in_(("PENDING", "QUEUED", "RUNNING")))
    )
    budget = min(budget, max(0, settings.max_pending_jobs - pending))
    if not budget:
        return 0
    rotation = await session.get(AppSetting, "watchlist_rotation")
    last_id = rotation.value.get("watch_id", "") if rotation else ""
    if not isinstance(last_id, str):
        last_id = ""
    query = select(Watchlist).where(Watchlist.enabled.is_(True), Watchlist.scope.in_(SCOPES))
    watches = (
        await session.scalars(query.where(Watchlist.id > last_id).order_by(Watchlist.id).limit(budget * 4))
    ).all()
    if not watches:
        watches = (await session.scalars(query.order_by(Watchlist.id).limit(budget * 4))).all()
    if not watches:
        return 0
    enabled = [name for name in FACTORIES if settings.provider_policy(name).enabled]
    created = 0
    for watch in watches:
        if created >= budget:
            break
        last_id = watch.id
        horizon = watch.filters.get("days_ahead", 30)
        if type(horizon) is not int or not 1 <= horizon <= 365:
            continue
        # N monitored check-in dates, including today; the last checkout stays
        # inside the existing today + 365-day boundary.
        horizon -= 1
        hotel_cursor = None
        if watch.scope == "hotel":
            hotel_id = watch.filters.get("hotel_id")
            if not isinstance(hotel_id, str):
                continue
            hotel = await session.get(Hotel, hotel_id)
            hotels = [hotel] if hotel else []
        else:
            hotels, hotel_cursor = await scoped_hotels(session, watch, enabled, now)
        for hotel in hotels:
            if hotel_cursor:
                hotel_cursor.value = {"hotel_id": hotel.id}
            if not hotel.active or hotel.provider not in enabled:
                continue
            state = await session.get(ProviderStatus, hotel.provider)
            if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                continue
            # Preserve legacy per-hotel watch progress. Wide scopes need a
            # separate date cursor for every matched hotel, not one shared day.
            cursor_key = (
                f"watch_cursor:{watch.id}"
                if watch.scope == "hotel"
                else f"watch_date_cursor:{watch.id}:{hotel.id}"
            )
            cursor = await session.get(AppSetting, cursor_key)
            today = date.today()
            check_in, next_value = advance_date_cursor(cursor.value if cursor else {}, today, horizon)
            if cursor:
                cursor.value = next_value
            else:
                session.add(AppSetting(key=cursor_key, value=next_value))
            hours = {
                "HOT": settings.hot_interval_hours,
                "WARM": settings.warm_interval_hours,
                "COLD": settings.cold_interval_hours,
            }[tier((check_in - today).days)]
            recent = await session.scalar(
                select(CrawlJob.id)
                .where(
                    CrawlJob.hotel_id == hotel.id,
                    CrawlJob.check_in == check_in,
                    CrawlJob.check_out == check_in + timedelta(days=1),
                    CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
                    func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == 2,
                    func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == 1,
                    or_(CrawlJob.status.in_(LIVE), CrawlJob.created_at >= now - timedelta(hours=hours)),
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
                    priority=85,
                ),
            )
            created += 1
            break  # At most one job per watch per tick; a whole country cannot monopolize it.
    if rotation:
        rotation.value = {"watch_id": last_id}
    else:
        session.add(AppSetting(key="watchlist_rotation", value={"watch_id": last_id}))
    return created


async def fair_frontier_hotels(session: AsyncSession, query, budget: int) -> list[Hotel]:
    """Round-robin groups before hotels; bounded keysets survive process restarts.

    A large healthy catalog must not fill every frontier inspection slot while
    smaller groups wait for its UUID range to finish. Existing nightly cursors
    are untouched. Missing per-group hotel cursors inherit the legacy keyset.
    """
    if budget < 1:
        return []
    providers = (await session.scalars(query.with_only_columns(Hotel.provider).distinct())).all()
    providers = sorted(providers)
    if not providers:
        return []
    turn = await session.get(AppSetting, "global_provider_cursor")
    last_provider = turn.value.get("provider", "") if turn else ""
    if not isinstance(last_provider, str):
        last_provider = ""
    providers = [p for p in providers if p > last_provider] + [p for p in providers if p <= last_provider]
    legacy = await session.get(AppSetting, "global_hotel_cursor")
    legacy_id = legacy.value.get("hotel_id", "") if legacy else ""
    if not isinstance(legacy_id, str):
        legacy_id = ""
    cursors, candidates = {}, {}
    for provider in providers:
        cursor = await session.get(AppSetting, "global_hotel_cursor:" + provider)
        cursors[provider] = cursor
        last_id = cursor.value.get("hotel_id", "") if cursor else legacy_id
        if not isinstance(last_id, str):
            last_id = ""
        scoped = query.where(Hotel.provider == provider)
        rows = (
            await session.scalars(scoped.where(Hotel.id > last_id).order_by(Hotel.id).limit(budget))
        ).all()
        if not rows:
            rows = (await session.scalars(scoped.order_by(Hotel.id).limit(budget))).all()
        candidates[provider] = deque(rows)
    chosen, next_ids = [], {}
    active = deque(p for p in providers if candidates[p])
    while active and len(chosen) < budget:
        provider = active.popleft()
        hotel = candidates[provider].popleft()
        chosen.append(hotel)
        next_ids[provider] = hotel.id
        last_provider = provider
        if candidates[provider]:
            active.append(provider)
    for provider, hotel_id in next_ids.items():
        cursor = cursors[provider]
        if cursor:
            cursor.value = {"hotel_id": hotel_id}
        else:
            session.add(AppSetting(key="global_hotel_cursor:" + provider, value={"hotel_id": hotel_id}))
    if chosen:
        if turn:
            turn.value = {"provider": last_provider}
        else:
            session.add(AppSetting(key="global_provider_cursor", value={"provider": last_provider}))
    return chosen


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
    # Reserve at most half for revisiting known calendar dates; all spare slots
    # still advance the full-year/global discovery cursor. With one available
    # slot, alternate lanes so neither calendar history nor discovery starves.
    recheck_budget = budget // 2
    if budget == 1:
        lane = await session.get(AppSetting, "global_single_slot_lane")
        revisit = not bool(lane.value.get("revisit_last") if lane else False)
        if lane:
            lane.value = {"revisit_last": revisit}
        else:
            session.add(AppSetting(key="global_single_slot_lane", value={"revisit_last": revisit}))
        recheck_budget = int(revisit)
    created = await expand_rechecks(session, settings, enabled, recheck_budget)
    frontier_budget = budget - created
    if not frontier_budget:
        return created
    rotation = await session.get(AppSetting, "global_hotel_cursor")
    last_id = rotation.value.get("hotel_id", "") if rotation else ""
    now = utcnow()
    # Exclude paused groups before the bounded keyset page. Otherwise a large
    # paused catalog can consume every inspection slot while healthy groups
    # wait through thousands of unrelated hotels without any work generated.
    query = (
        select(Hotel)
        .outerjoin(ProviderStatus, ProviderStatus.provider == Hotel.provider)
        .where(
            Hotel.active.is_(True),
            Hotel.provider.in_(enabled),
            or_(ProviderStatus.blocked_until.is_(None), ProviderStatus.blocked_until <= now),
        )
    )
    hotels = await fair_frontier_hotels(session, query, frontier_budget)
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
                CrawlJob.check_out == check_in + timedelta(days=1),
                CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
                func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == 2,
                func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == 1,
                or_(CrawlJob.status.in_(LIVE), CrawlJob.created_at >= now - timedelta(hours=hours)),
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
