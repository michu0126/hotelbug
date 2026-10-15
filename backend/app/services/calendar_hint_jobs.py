"""Bounded monthly calendar scouting, separate from verified nightly coverage."""

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.calendar_hints import month_window
from app.schemas.domain import JobInput, JobKind
from app.services.capacity import load_capacity
from app.services.catalog_price_source import load_catalog_price_source
from app.services.jobs import LIVE, enqueue, tier

HOTEL_CURSOR = "calendar_hint_hotel_cursor:marriott"
MONTH_CURSOR_PREFIX = "calendar_hint_month_cursor:"
SUCCESS_PREFIX = "calendar_hint_success:"


def monitored_months(today: date) -> list[str]:
    """The 12 or 13 calendar months intersecting 365 check-in dates."""
    start, end = today.replace(day=1), today + timedelta(days=364)
    months = []
    while start <= end:
        months.append(start.strftime("%Y-%m"))
        start = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def _value(row: AppSetting | None) -> dict:
    return row.value if row and isinstance(row.value, dict) else {}


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _completion(row: AppSetting) -> datetime | None:
    value = _value(row)
    if value.get("source_root") != CN_ROOT:
        return None
    raw = value.get("completed_at")
    try:
        stamp = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return _utc(stamp) if stamp and stamp.tzinfo else None


async def _attempts(session: AsyncSession, hotel_id: str, months: list[str]) -> dict:
    """Return at most 13 latest attempts, retaining any older still-LIVE task."""
    month = CrawlJob.payload["month"].as_string()
    ranked = (
        select(
            month.label("month"),
            CrawlJob.status,
            CrawlJob.created_at,
            CrawlJob.scheduled_at,
            func.max(case((CrawlJob.status.in_(LIVE), 1), else_=0)).over(partition_by=month).label("live"),
            func.row_number()
            .over(partition_by=month, order_by=(CrawlJob.created_at.desc(), CrawlJob.id.desc()))
            .label("position"),
        )
        .where(
            CrawlJob.hotel_id == hotel_id,
            CrawlJob.provider == "marriott",
            CrawlJob.kind == JobKind.FETCH_CALENDAR_HINTS,
            CrawlJob.status != "CANCELLED",
            month.in_(months),
        )
        .subquery()
    )
    rows = (await session.execute(select(ranked).where(ranked.c.position == 1).limit(13))).mappings()
    return {row["month"]: row for row in rows}


async def expand_calendar_hints(session: AsyncSession, settings: Settings, budget: int = 1) -> int:
    """Persist hotel/month rotation without treating hints as completed stays.

    Only the explicitly accepted Chinese directory source is supported. The
    shared admission lock and capacity reserve other enabled groups' first slots.
    Existing failures, provider cooldowns, and queued retries are never reset.
    """
    if (
        budget < 1
        or not settings.global_monitoring_enabled
        or not settings.provider_policy("marriott").enabled
    ):
        return 0
    source = await load_catalog_price_source(session)
    if source.root != CN_ROOT:
        return 0
    now = utcnow()
    state = await session.get(ProviderStatus, "marriott")
    if state and state.blocked_until and _utc(state.blocked_until) > now:
        return 0
    capacity = await load_capacity(session, settings)
    budget = min(budget, capacity.available_for("marriott"))
    if budget < 1:
        return 0

    rotation = await session.get(AppSetting, HOTEL_CURSOR)
    last_id = _value(rotation).get("hotel_id", "")
    if not isinstance(last_id, str):
        last_id = ""
    query = select(Hotel).where(
        Hotel.active.is_(True), Hotel.provider == "marriott", source.hotel_predicate()
    )
    inspection_limit = min(budget * 4, 200)
    hotels = (
        await session.scalars(query.where(Hotel.id > last_id).order_by(Hotel.id).limit(inspection_limit))
    ).all()
    if not hotels:
        hotels = (await session.scalars(query.order_by(Hotel.id).limit(inspection_limit))).all()
    if not hotels:
        return 0

    today = now.astimezone(timezone(timedelta(hours=8))).date()
    months = monitored_months(today)
    created = 0
    for hotel in hotels:
        if created >= budget:
            break
        last_id = hotel.id
        cursor_key = MONTH_CURSOR_PREFIX + hotel.id
        cursor = await session.get(AppSetting, cursor_key)
        last_month = _value(cursor).get("last_month")
        start = (months.index(last_month) + 1) % len(months) if last_month in months else 0
        ordered = months[start:] + months[:start]
        attempts = await _attempts(session, hotel.id, months)
        success_keys = {SUCCESS_PREFIX + hotel.id + ":" + month: month for month in months}
        completions = {
            success_keys[row.key]: stamp
            for row in await session.scalars(select(AppSetting).where(AppSetting.key.in_(success_keys)))
            if (stamp := _completion(row)) is not None
        }
        for month in ordered:
            window = month_window(month, today)
            if window is None:
                continue
            temperature = tier((window[0] - today).days)
            interval = {
                "HOT": settings.hot_interval_hours,
                "WARM": settings.warm_interval_hours,
                "COLD": settings.cold_interval_hours,
            }[temperature]
            attempt = attempts.get(month)
            previous = completions.get(month)
            if attempt:
                if attempt["live"]:
                    continue
                if attempt["status"] == "FAILED":
                    interval = max(interval, settings.cold_interval_hours)
                previous = max(
                    _utc(attempt["created_at"]),
                    _utc(attempt["scheduled_at"]),
                    *([previous] if previous else []),
                )
            if previous is not None and previous + timedelta(hours=interval) > now:
                continue
            await enqueue(
                session,
                JobInput(
                    provider="marriott",
                    kind=JobKind.FETCH_CALENDAR_HINTS,
                    hotel_id=hotel.id,
                    priority={"HOT": 75, "WARM": 55, "COLD": 35}[temperature],
                    payload={"month": month, **source.payload("marriott")},
                ),
            )
            capacity.consume("marriott")
            created += 1
            if cursor:
                cursor.value = {"last_month": month}
            else:
                session.add(AppSetting(key=cursor_key, value={"last_month": month}))
            break  # One hotel cannot consume the whole monthly admission lane.
    if rotation:
        rotation.value = {"hotel_id": last_id}
    else:
        session.add(AppSetting(key=HOTEL_CURSOR, value={"hotel_id": last_id}))
    return created
