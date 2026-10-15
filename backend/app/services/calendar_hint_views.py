"""Latest displayed daily minima for a hotel's calendar, apart from room offers."""

from datetime import timedelta, timezone

from sqlalchemy import func, select

from app.models.tables import CalendarHintHistory, utcnow
from app.providers.marriott_cn_catalog import ROOT
from app.schemas.calendar_hints import SHANGHAI
from app.services.catalog_price_source import load_catalog_price_source


async def hotel_hint_days(session, hotel, start, end):
    source = await load_catalog_price_source(session)
    if (
        source.root != ROOT
        or hotel.provider != "marriott"
        or not hotel.active
        or not hotel.official_url.startswith("https://www.marriott.com.cn/")
    ):
        return {}
    now = utcnow()
    today = now.astimezone(SHANGHAI).date()
    start, end = max(start, today), min(end, today + timedelta(days=365))
    if start >= end:
        return {}
    ranked = (
        select(
            CalendarHintHistory.id,
            func.row_number()
            .over(
                partition_by=CalendarHintHistory.check_in,
                order_by=(CalendarHintHistory.captured_at.desc(), CalendarHintHistory.id.desc()),
            )
            .label("position"),
        )
        .where(
            CalendarHintHistory.hotel_id == hotel.id,
            CalendarHintHistory.provider == "marriott",
            CalendarHintHistory.check_in >= start,
            CalendarHintHistory.check_in < end,
            CalendarHintHistory.adults == 2,
            CalendarHintHistory.rooms == 1,
            CalendarHintHistory.basis == "CALENDAR_MINIMUM_TAX_UNCONFIRMED",
            CalendarHintHistory.displayed_price > 0,
            CalendarHintHistory.captured_at <= now,
            CalendarHintHistory.source_url.like(
                "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode="
                + hotel.provider_hotel_id
                + "&isSearch=true&currency=%"
            ),
        )
        .subquery()
    )
    rows = (
        await session.scalars(
            select(CalendarHintHistory)
            .join(ranked, ranked.c.id == CalendarHintHistory.id)
            .where(ranked.c.position == 1)
            .limit(31)
        )
    ).all()
    return {
        row.check_in.isoformat(): {
            "displayed_price": str(row.displayed_price),
            "currency": row.currency,
            "basis": row.basis,
            "captured_at": row.captured_at.replace(tzinfo=row.captured_at.tzinfo or timezone.utc).isoformat(),
            "source_url": row.source_url,
        }
        for row in rows
    }


def attach_hint_days(calendar, hints):
    for day in calendar["days"]:
        if hint := hints.get(day["date"]):
            day["calendar_hint"] = hint
    return calendar
