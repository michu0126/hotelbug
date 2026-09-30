"""One-night cash calendar. Comparisons never cross offer identity or currency."""

import calendar as calendar_module
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Hotel, PriceHistory, Rate, StayScan


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)


def month_bounds(month: str) -> tuple[date, date]:
    try:
        if len(month) != 7 or month[4] != "-":
            raise ValueError
        start = date(int(month[:4]), int(month[5:]), 1)
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    if start.month == 12:
        return start, date(start.year + 1, 1, 1)
    return start, date(start.year, start.month + 1, 1)


def month_dates(month: str) -> list[date]:
    start, _ = month_bounds(month)
    return [start + timedelta(days=i) for i in range(calendar_module.monthrange(start.year, start.month)[1])]


def calendar_rows(
    hotel: Hotel,
    month: str,
    current: list[Rate],
    history: list[PriceHistory],
    historical_lows: dict[str, Decimal] | None = None,
    scans: list[StayScan] | None = None,
) -> dict:
    by_day: dict[date, list[Rate]] = defaultdict(list)
    for rate in current:
        if (
            rate.availability
            and rate.total_price is not None
            and rate.adults == 2
            and rate.rooms == 1
            and (hotel.default_currency is None or rate.currency == hotel.default_currency)
        ):
            by_day[rate.check_in].append(rate)
    prior: dict[str, list[PriceHistory]] = defaultdict(list)
    for item in history:
        if item.availability and item.total_price is not None:
            prior[item.offer_key].append(item)
    scans_by_day = {scan.check_in: scan for scan in scans or [] if (scan.check_out - scan.check_in).days == 1}
    days = []
    for day in month_dates(month):
        choices = by_day.get(day, [])
        best = min(choices, key=lambda r: (r.total_price, r.offer_key)) if choices else None
        scan = scans_by_day.get(day)
        if scan and scan.status == "UNAVAILABLE":
            observed = as_utc(scan.observed_at)
            captured = as_utc(best.captured_at) if best else None
            if captured is None or observed >= captured:
                days.append({"date": day.isoformat(), "status": "UNAVAILABLE"})
                continue
        if not best:
            days.append({"date": day.isoformat(), "status": "NO_DATA"})
            continue
        # Historical low and previous sample refer to exactly the selected offer.
        observations = [item for item in prior.get(best.offer_key, []) if item.currency == best.currency]
        historical_low = (historical_lows or {}).get(best.offer_key)
        if historical_low is None:
            historical_low = min((item.total_price for item in observations), default=None)
        older = [item for item in observations if as_utc(item.captured_at) < as_utc(best.captured_at)]
        previous = max(older, key=lambda item: as_utc(item.captured_at)) if older else None
        drop = None
        if previous and previous.total_price > 0:
            drop = ((previous.total_price - best.total_price) / previous.total_price * 100).quantize(
                Decimal("0.01")
            )
        days.append(
            {
                "date": day.isoformat(),
                "status": "AVAILABLE",
                "currency": best.currency,
                "current_low": str(best.total_price),
                "historical_low": str(historical_low) if historical_low is not None else None,
                "drop_from_previous_percent": str(drop) if drop is not None else None,
                "offer_key": best.offer_key,
                "room_type": best.room_type,
                "rate_name": best.rate_name,
                "member_rate": best.member_rate,
                "captured_at": best.captured_at.isoformat(),
            }
        )
    return {
        "hotel_id": hotel.id,
        "month": month,
        "price_basis": "one_night_total",
        "currency": hotel.default_currency,
        "days": days,
    }


async def get_calendar(session: AsyncSession, hotel: Hotel, month: str) -> dict:
    start, end = month_bounds(month)
    scans = (
        await session.scalars(
            select(StayScan).where(
                StayScan.hotel_id == hotel.id,
                StayScan.check_in >= start,
                StayScan.check_in < end,
                StayScan.adults == 2,
                StayScan.rooms == 1,
            )
        )
    ).all()
    current = (
        await session.scalars(
            select(Rate).where(
                Rate.hotel_id == hotel.id,
                Rate.check_in >= start,
                Rate.check_in < end,
                Rate.adults == 2,
                Rate.rooms == 1,
            )
        )
    ).all()
    current = [item for item in current if (item.check_out - item.check_in).days == 1]
    # The calendar needs prior samples only for each day's current cheapest offer.
    keys = {
        min(rates, key=lambda r: (r.total_price, r.offer_key)).offer_key
        for day in month_dates(month)
        if (
            rates := [
                r
                for r in current
                if r.check_in == day
                and r.availability
                and r.total_price is not None
                and (hotel.default_currency is None or r.currency == hotel.default_currency)
            ]
        )
    }
    if not keys:
        return calendar_rows(hotel, month, current, [], scans=scans)
    lows = dict(
        (
            await session.execute(
                select(PriceHistory.offer_key, func.min(PriceHistory.total_price))
                .where(
                    PriceHistory.hotel_id == hotel.id,
                    PriceHistory.offer_key.in_(keys),
                    PriceHistory.availability.is_(True),
                    PriceHistory.total_price.is_not(None),
                )
                .group_by(PriceHistory.offer_key)
            )
        ).all()
    )
    # Bounded trend lookback; all-time low is calculated in PostgreSQL above.
    history = (
        await session.scalars(
            select(PriceHistory).where(
                PriceHistory.hotel_id == hotel.id,
                PriceHistory.offer_key.in_(keys),
                PriceHistory.captured_at >= func.now() - timedelta(days=90),
            )
        )
    ).all()
    return calendar_rows(hotel, month, current, history, lows, scans)
