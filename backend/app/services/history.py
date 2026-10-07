"""Read-only, same-offer history with complete UTC daily aggregates."""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Hotel, PriceHistory, utcnow
from app.services.calendar import as_utc
from app.services.offer_policy import classify_offer
from app.services.pricing import displayed_price, price_field


def offer_metadata(row: PriceHistory) -> dict:
    return {
        "offer_key": row.offer_key,
        "check_in": row.check_in.isoformat(),
        "check_out": row.check_out.isoformat(),
        "room_type": row.room_type,
        "room_code": row.room_code,
        "rate_name": row.rate_name,
        "rate_code": row.rate_code,
        "currency": row.currency,
        "price_field": price_field(row),
        "price_basis": row.price_basis,
        "adults": row.adults,
        "rooms": row.rooms,
        "member_rate": row.member_rate,
        "refundable": row.refundable,
        "breakfast_included": row.breakfast_included,
        "offer_classification": classify_offer(row).as_dict(),
    }


async def get_stay_offers(
    session: AsyncSession,
    hotel: Hotel,
    check_in: date,
    *,
    check_out: date | None = None,
    after: str | None = None,
    limit: int = 100,
) -> dict:
    filters = [
        PriceHistory.hotel_id == hotel.id,
        PriceHistory.check_in == check_in,
        PriceHistory.captured_at <= utcnow(),
    ]
    if check_out is not None:
        filters.append(PriceHistory.check_out == check_out)
    if after:
        filters.append(PriceHistory.offer_key > after)
    ranked = (
        select(
            PriceHistory.id,
            func.row_number()
            .over(
                partition_by=PriceHistory.offer_key,
                order_by=(
                    (PriceHistory.total_price.is_not(None) | PriceHistory.cash_price.is_not(None)).desc(),
                    PriceHistory.captured_at.desc(),
                    PriceHistory.id.desc(),
                ),
            )
            .label("position"),
        )
        .where(*filters)
        .subquery()
    )
    rows = (
        await session.scalars(
            select(PriceHistory)
            .join(ranked, ranked.c.id == PriceHistory.id)
            .where(ranked.c.position == 1)
            .order_by(PriceHistory.offer_key)
            .limit(limit + 1)
        )
    ).all()
    return {
        "hotel_id": hotel.id,
        "check_in": check_in.isoformat(),
        "offers": [offer_metadata(row) for row in rows[:limit]],
        "next_cursor": rows[limit - 1].offer_key if len(rows) > limit else None,
    }


def amount(value) -> str | None:
    return str(Decimal(str(value)).quantize(Decimal("0.0001"))) if value is not None else None


async def get_offer_trend(session: AsyncSession, hotel: Hotel, key: str, days: int) -> dict | None:
    now = utcnow()
    snapshot = await session.scalar(
        select(PriceHistory)
        .where(
            PriceHistory.hotel_id == hotel.id,
            PriceHistory.offer_key == key,
            PriceHistory.captured_at <= now,
        )
        # Legacy points-only observations may share an all-fees offer key. They
        # must not change the amount basis of an existing cash history series.
        .order_by(
            (PriceHistory.total_price.is_not(None) | PriceHistory.cash_price.is_not(None)).desc(),
            PriceHistory.captured_at.desc(),
            PriceHistory.id.desc(),
        )
        .limit(1)
    )
    if snapshot is None:
        return None
    field = price_field(snapshot)
    price = getattr(PriceHistory, field)
    base_filters = [
        PriceHistory.hotel_id == hotel.id,
        PriceHistory.offer_key == key,
        PriceHistory.currency == snapshot.currency,
        PriceHistory.availability.is_(True),
        price > 0,
        PriceHistory.captured_at <= now,
    ]
    if field == "cash_price":
        base_filters.append(PriceHistory.total_price.is_(None))
    all_time_low = await session.scalar(select(func.min(price)).where(*base_filters))
    filters = [*base_filters, PriceHistory.captured_at >= now - timedelta(days=days)]
    # PostgreSQL timestamptz -> UTC date explicitly; SQLite stores the normalized UTC values.
    timestamp = PriceHistory.captured_at
    if session.get_bind().dialect.name == "postgresql":
        timestamp = func.timezone("UTC", timestamp)
    day = func.date(timestamp)
    daily = (
        await session.execute(
            select(day.label("day"), func.min(price), func.max(price), func.avg(price), func.count())
            .where(*filters)
            .group_by(day)
            .order_by(day)
        )
    ).all()
    stats = (
        await session.execute(
            select(func.count(), func.min(price), func.max(price), func.avg(price)).where(*filters)
        )
    ).one()
    latest = await session.scalar(
        select(PriceHistory)
        .where(*filters)
        .order_by(PriceHistory.captured_at.desc(), PriceHistory.id.desc())
        .limit(1)
    )
    return {
        "hotel_id": hotel.id,
        "hotel_name": hotel.hotel_name,
        "provider": hotel.provider,
        "offer": offer_metadata(snapshot),
        "days": days,
        "timezone": "UTC",
        "window_start": (now - timedelta(days=days)).isoformat(),
        "window_end": now.isoformat(),
        "observation_count": stats[0],
        "observation_days": len(daily),
        "window_low": amount(stats[1]),
        "window_high": amount(stats[2]),
        "window_mean": amount(stats[3]),
        "historical_low": amount(all_time_low),
        "last_observed_price": amount(displayed_price(latest)) if latest else None,
        "last_observed_at": as_utc(latest.captured_at).isoformat() if latest else None,
        "points": [
            {
                "date": str(row[0]),
                "low": amount(row[1]),
                "high": amount(row[2]),
                "mean": amount(row[3]),
                "samples": row[4],
            }
            for row in daily
        ],
    }
