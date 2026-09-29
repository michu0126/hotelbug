"""Historical price-drop detection and independently fetched confirmation."""

import hashlib
from collections import defaultdict
from datetime import timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.tables import Hotel, NotificationLog, PriceAlert, PriceHistory, PriceStatistics, utcnow
from app.schemas.domain import JobInput, JobKind, RateData
from app.services.jobs import enqueue


async def detect_drops(session: AsyncSession, hotel: Hotel, rates: list[RateData], settings: Settings) -> int:
    created = 0
    now = utcnow()
    for rate in rates:
        if not rate.availability or rate.total_price is None or rate.total_price <= 0:
            continue
        key = rate.offer_key()
        history = (
            await session.scalars(
                select(PriceHistory).where(
                    PriceHistory.offer_key == key,
                    PriceHistory.captured_at >= now - timedelta(days=90),
                    PriceHistory.captured_at < rate.captured_at,
                    PriceHistory.availability.is_(True),
                    PriceHistory.total_price > 0,
                )
            )
        ).all()
        # One median per observation day avoids overweighting frequently polled days.
        days = defaultdict(list)
        for old in history:
            observed = old.captured_at
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            day = observed.astimezone(timezone.utc).date()
            if day < rate.captured_at.astimezone(timezone.utc).date():
                days[day].append(old.total_price)
        daily = {day: median(values) for day, values in days.items()}
        if len(daily) < settings.alert_min_history_days:
            continue
        baseline = median(daily.values())
        statistics = await session.get(PriceStatistics, key)
        if statistics is None:
            statistics = PriceStatistics(offer_key=key, hotel_id=hotel.id, date=rate.check_in)
            session.add(statistics)
        statistics.sample_count = len(daily)
        statistics.median_90d = baseline
        for window in (7, 30):
            values = [value for day, value in daily.items() if day >= now.date() - timedelta(days=window)]
            setattr(statistics, f"median_{window}d", median(values) if values else None)
        statistics.historical_low = min(daily.values())
        statistics.historical_high = max(daily.values())
        statistics.updated_at = now
        drop = Decimal(1) - rate.total_price / baseline
        if drop < settings.alert_drop_fraction:
            continue
        dedupe = hashlib.sha256(f"{key}:{now.date()}".encode()).hexdigest()
        if await session.scalar(select(PriceAlert.id).where(PriceAlert.dedupe_key == dedupe)):
            continue
        alert = PriceAlert(
            hotel_id=hotel.id,
            offer_key=key,
            event_type="PRICE_DROP",
            anomaly_score=min(100, int(drop * 100)),
            confirmed=False,
            dedupe_key=dedupe,
            payload={
                "hotel_name": hotel.hotel_name,
                "provider": hotel.provider,
                "check_in": rate.check_in.isoformat(),
                "check_out": rate.check_out.isoformat(),
                "price": str(rate.total_price),
                "currency": rate.currency,
                "baseline": str(baseline),
                "threshold": str(baseline * (1 - settings.alert_drop_fraction)),
                "history_days": len(daily),
                "room_type": rate.room_type,
                "rate_name": rate.rate_name,
                "price_basis": rate.price_basis,
                "rooms": rate.rooms,
                "adults": rate.adults,
                "source_url": str(rate.source_url),
                "verification": "PENDING",
            },
        )
        session.add(alert)
        await session.flush()
        job = await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.VERIFY_ANOMALY,
                hotel_id=hotel.id,
                check_in=rate.check_in,
                check_out=rate.check_out,
                priority=100,
                payload={"alert_id": alert.id, "adults": rate.adults, "rooms": rate.rooms},
            ),
            delay_seconds=settings.alert_confirmation_seconds,
        )
        alert.verification_job_id = job.id
        created += 1
    return created


async def confirm_drop(session: AsyncSession, alert_id: str, job_id: str, rates: list[RateData]) -> bool:
    alert = await session.scalar(select(PriceAlert).where(PriceAlert.id == alert_id).with_for_update())
    if not alert or alert.verification_job_id != job_id:
        return False
    if alert.confirmed:
        return True
    match = next((r for r in rates if r.offer_key() == alert.offer_key), None)
    payload = dict(alert.payload)
    if (
        match is None
        or not match.availability
        or match.total_price is None
        or not Decimal(0) < match.total_price <= Decimal(payload["threshold"])
    ):
        payload["verification"] = "REJECTED"
        alert.payload = payload
        return False
    payload.update(
        price=str(match.total_price),
        verification="CONFIRMED",
        verified_at=utcnow().isoformat(),
        drop_percent=str(round((1 - match.total_price / Decimal(payload["baseline"])) * 100, 1)),
    )
    alert.payload = payload
    alert.confirmed = True
    session.add(NotificationLog(alert_id=alert.id, channel="telegram", dedupe_key=f"telegram:{alert.id}"))
    await session.flush()
    return True
