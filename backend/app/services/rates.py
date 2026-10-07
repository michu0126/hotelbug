from datetime import date, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Hotel, PriceHistory, Rate, StayScan, utcnow
from app.schemas.domain import HotelData, RateData


async def upsert_hotel(session: AsyncSession, data: HotelData) -> Hotel:
    row = await session.scalar(
        select(Hotel).where(
            Hotel.provider == data.provider, Hotel.provider_hotel_id == data.provider_hotel_id
        )
    )
    values = data.model_dump()
    values["official_url"] = str(data.official_url)
    for key in ("brand", "country", "region", "city", "address"):
        if isinstance(values[key], str):
            values[key] = values[key].strip() or None
    if row:
        for key, value in values.items():
            # An incomplete rendered directory is a new observation, not a
            # command to erase previously known metadata. New hotels still
            # retain NULLs, and explicit nonempty updates remain authoritative.
            if value is None:
                continue
            # HotelData defaults active=True for newly discovered properties.
            # A source that did not observe activity cannot undo an earlier
            # explicit inactive result; only an explicit new value can do so.
            if key == "active" and key not in data.model_fields_set:
                continue
            setattr(row, key, value)
    else:
        row = Hotel(**values)
        session.add(row)
    await session.flush()
    return row


async def persist_rates(session: AsyncSession, hotel: Hotel, rates: list[RateData], job_id: str) -> int:
    # Serialize persistence for this hotel even when different jobs return the same offer.
    await session.execute(select(Hotel.id).where(Hotel.id == hotel.id).with_for_update())
    count = 0
    for rate in rates:
        if rate.provider != hotel.provider or rate.provider_hotel_id != hotel.provider_hotel_id:
            raise ValueError("provider returned rates for another hotel")
        key = rate.offer_key()
        exists = await session.scalar(
            select(PriceHistory.id).where(PriceHistory.job_id == job_id, PriceHistory.offer_key == key)
        )
        if exists:
            continue
        values = rate.model_dump(exclude={"provider_hotel_id"})
        values.update(hotel_id=hotel.id, offer_key=key, source_url=str(rate.source_url))
        session.add(PriceHistory(**values, job_id=job_id))
        current = await session.scalar(select(Rate).where(Rate.offer_key == key).with_for_update())
        if current:
            # Do not let a delayed older job replace the newest observation.
            if current.captured_at.replace(tzinfo=rate.captured_at.tzinfo) <= rate.captured_at:
                for name, value in values.items():
                    setattr(current, name, value)
        else:
            session.add(Rate(**values))
        await session.flush()
        count += 1
    return count


async def finalize_stay_scan(
    session: AsyncSession,
    hotel: Hotel,
    check_in: date,
    check_out: date,
    adults: int,
    rooms: int,
    rates: list[RateData],
    job_id: str,
    empty_status: str = "UNAVAILABLE",
) -> None:
    """Retire offers absent from a successful new scan, including an empty stay."""
    if empty_status not in ("UNAVAILABLE", "NOT_OPEN"):
        raise ValueError("Invalid empty stay status")
    scan_status = "AVAILABLE" if rates else empty_status
    observed_at = max((rate.captured_at for rate in rates), default=utcnow())
    current_scan = await session.scalar(
        select(StayScan)
        .where(
            StayScan.hotel_id == hotel.id,
            StayScan.check_in == check_in,
            StayScan.check_out == check_out,
            StayScan.adults == adults,
            StayScan.rooms == rooms,
        )
        .with_for_update()
    )
    if current_scan:
        previous = current_scan.observed_at
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)
        if previous > observed_at:
            return
        current_scan.status = scan_status
        current_scan.observed_at = observed_at
        current_scan.job_id = job_id
    else:
        session.add(
            StayScan(
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_out,
                adults=adults,
                rooms=rooms,
                status=scan_status,
                observed_at=observed_at,
                job_id=job_id,
            )
        )
    seen = {rate.offer_key() for rate in rates}
    current_rates = (
        await session.scalars(
            select(Rate)
            .where(
                Rate.hotel_id == hotel.id,
                Rate.check_in == check_in,
                Rate.check_out == check_out,
                Rate.adults == adults,
                Rate.rooms == rooms,
            )
            .with_for_update()
        )
    ).all()
    for current in current_rates:
        captured = current.captured_at
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        if current.offer_key in seen or captured > observed_at:
            continue
        current.availability = False
        current.cash_price = None
        current.tax = None
        current.total_price = None
        current.points_price = None
        current.captured_at = observed_at
