from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import Hotel, PriceHistory, Rate
from app.schemas.domain import HotelData, RateData


async def upsert_hotel(session: AsyncSession, data: HotelData) -> Hotel:
    row = await session.scalar(
        select(Hotel).where(
            Hotel.provider == data.provider, Hotel.provider_hotel_id == data.provider_hotel_id
        )
    )
    values = data.model_dump()
    values["official_url"] = str(data.official_url)
    if row:
        for key, value in values.items():
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
