"""Coverage of observed calendar minima, kept separate from room/stay coverage."""

from datetime import timedelta, timezone

from sqlalchemy import case, func, select

from app.models.tables import CalendarHintHistory, Hotel, utcnow
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.calendar_hints import SHANGHAI
from app.schemas.domain import ProviderName
from app.services.catalog_price_source import load_catalog_price_source

BASIS = "CALENDAR_MINIMUM_TAX_UNCONFIRMED"


async def calendar_hint_coverage_by_provider(session, settings, now=None) -> dict[str, dict]:
    """Aggregate the latest actual observation for each eligible hotel/date.

    Multiple currencies, repeated jobs, and retained history do not multiply
    coverage. A calendar hint says nothing about room inventory or tax/member
    eligibility, and neither StayScan nor queued jobs are consulted here.
    """
    now = now or utcnow()
    now = now.replace(tzinfo=now.tzinfo or timezone.utc).astimezone(timezone.utc)
    today = now.astimezone(SHANGHAI).date()
    end = today + timedelta(days=365)
    result = {
        provider.value: {
            "supported": False,
            "status": "UNSUPPORTED_PROVIDER"
            if provider != ProviderName.MARRIOTT
            else "CN_SOURCE_NOT_SELECTED",
            "scope": "ACTIVE_COLLECTED_HOTELS_CURRENT_SOURCE",
            "query_mode": "MONTHLY_CALENDAR_HINT",
            "basis": BASIS,
            "timezone": "Asia/Shanghai",
            "window_start": today.isoformat(),
            "window_end_exclusive": end.isoformat(),
            "adults": 2,
            "rooms": 1,
            "nights": 1,
            "eligible_hotels": None,
            "observed_hotels": None,
            "observed_dates": None,
            "fresh_dates": None,
            "stale_dates": None,
            "required_dates": None,
        }
        for provider in ProviderName
    }
    source = await load_catalog_price_source(session)
    if source.root != CN_ROOT:
        return result  # No accepted CN source is not a measured zero/global success.
    hotel_scope = (
        Hotel.provider == "marriott",
        Hotel.active.is_(True),
        source.hotel_predicate(),
    )
    hotels = await session.scalar(select(func.count()).select_from(Hotel).where(*hotel_scope))
    latest = (
        select(
            CalendarHintHistory.hotel_id.label("hotel_id"),
            CalendarHintHistory.check_in.label("check_in"),
            func.max(CalendarHintHistory.captured_at).label("observed_at"),
        )
        .join(Hotel, Hotel.id == CalendarHintHistory.hotel_id)
        .where(
            *hotel_scope,
            CalendarHintHistory.provider == "marriott",
            CalendarHintHistory.adults == 2,
            CalendarHintHistory.rooms == 1,
            CalendarHintHistory.check_in >= today,
            CalendarHintHistory.check_in < end,
            CalendarHintHistory.captured_at <= now,
            CalendarHintHistory.basis == BASIS,
            CalendarHintHistory.displayed_price > 0,
            CalendarHintHistory.source_url.like(
                "https://www.marriott.com.cn/search/availabilityCalendar.mi?%"
            ),
            func.replace(CalendarHintHistory.source_url, "?", "&")
            .concat("&")
            .contains("&propertyCode=" + Hotel.provider_hotel_id + "&"),
        )
        .group_by(CalendarHintHistory.hotel_id, CalendarHintHistory.check_in)
        .subquery()
    )
    cutoff = case(
        (latest.c.check_in <= today + timedelta(days=30), now - timedelta(hours=settings.hot_interval_hours)),
        (
            latest.c.check_in <= today + timedelta(days=90),
            now - timedelta(hours=settings.warm_interval_hours),
        ),
        else_=now - timedelta(hours=settings.cold_interval_hours),
    )
    observed, fresh, observed_hotels = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(case((latest.c.observed_at > cutoff, 1), else_=0)), 0),
                func.count(func.distinct(latest.c.hotel_id)),
            ).select_from(latest)
        )
    ).one()
    result["marriott"].update(
        supported=True,
        status="SUPPORTED",
        eligible_hotels=hotels,
        observed_hotels=observed_hotels,
        observed_dates=observed,
        fresh_dates=fresh,
        stale_dates=observed - fresh,
        required_dates=hotels * 365,
        freshness_hours={
            "hot": settings.hot_interval_hours,
            "warm": settings.warm_interval_hours,
            "cold": settings.cold_interval_hours,
        },
    )
    return result
