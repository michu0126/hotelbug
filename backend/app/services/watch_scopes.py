"""Bounded hotel selection for wide watch scopes with durable per-hotel dates."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tables import AppSetting, Hotel, ProviderStatus, Watchlist
from app.schemas.watchlists import TARGET_FIELDS


async def scoped_hotels(
    session: AsyncSession, watch: Watchlist, enabled: Sequence[str], now: datetime, limit: int = 4
) -> tuple[list[Hotel], AppSetting | None]:
    field = TARGET_FIELDS.get(watch.scope)
    target = watch.filters.get(field) if field else None
    if not isinstance(target, str) or not target.strip():
        return [], None
    provider = watch.filters.get("provider")
    if provider is not None and (not isinstance(provider, str) or provider not in enabled):
        return [], None
    query = (
        select(Hotel)
        .outerjoin(ProviderStatus, ProviderStatus.provider == Hotel.provider)
        .where(
            Hotel.active.is_(True),
            Hotel.provider.in_(enabled),
            or_(ProviderStatus.blocked_until.is_(None), ProviderStatus.blocked_until <= now),
            func.lower(func.trim(getattr(Hotel, field))) == func.lower(target.strip()),
        )
    )
    if provider:
        query = query.where(Hotel.provider == provider)
    key = f"watch_hotel_cursor:{watch.id}"
    state = await session.get(AppSetting, key)
    last_id = state.value.get("hotel_id", "") if state else ""
    if not isinstance(last_id, str):
        last_id = ""
    hotels = (await session.scalars(query.where(Hotel.id > last_id).order_by(Hotel.id).limit(limit))).all()
    if not hotels:
        hotels = (await session.scalars(query.order_by(Hotel.id).limit(limit))).all()
    if state is None and hotels:
        state = AppSetting(key=key, value={})
        session.add(state)
    return hotels, state
