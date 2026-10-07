"""Historical price-drop detection and independently fetched confirmation."""

import hashlib
from collections import defaultdict
from datetime import timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.anomaly_config import AnomalyPolicy
from app.core.config import Settings
from app.models.tables import (
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceAlert,
    PriceHistory,
    PriceStatistics,
    utcnow,
)
from app.schemas.domain import JobInput, JobKind, RateData
from app.services.anomaly import anomaly_score, calendar_features, comparison_percentages, text_amount
from app.services.jobs import LIVE, enqueue, outside_window_cancellation
from app.services.offer_policy import classify_offer
from app.services.pricing import displayed_price, price_field


async def detect_drops(session: AsyncSession, hotel: Hotel, rates: list[RateData], settings: Settings) -> int:
    created = 0
    batches = defaultdict(list)
    seen_offers = set()
    now = utcnow()
    for rate in rates:
        key = rate.offer_key()
        if key in seen_offers:
            continue
        # Match persistence even if the first card is ineligible/unavailable:
        # a later duplicate must not silently replace that observation.
        seen_offers.add(key)
        amount = displayed_price(rate)
        field = price_field(rate)
        if not rate.availability or amount is None or amount <= 0:
            continue
        classification = classify_offer(rate)
        if not classification.alert_eligible:
            continue  # An aggregate/unknown offer is not a comparable public room/plan series.
        conditions = [
            PriceHistory.hotel_id == hotel.id,
            PriceHistory.offer_key == key,
            PriceHistory.currency == rate.currency,
            PriceHistory.captured_at < rate.captured_at,
            PriceHistory.availability.is_(True),
            PriceHistory.points_price.is_(None),
            getattr(PriceHistory, field) > 0,
        ]
        if field == "cash_price":
            conditions.append(PriceHistory.total_price.is_(None))
        history = (
            await session.scalars(
                select(PriceHistory).where(
                    *conditions,
                    PriceHistory.captured_at >= now - timedelta(days=90),
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
                days[day].append(getattr(old, field))
        daily = {day: median(values) for day, values in days.items()}
        windows = {
            window: [value for day, value in daily.items() if day >= now.date() - timedelta(days=window)]
            for window in (7, 30, 90)
        }
        baseline_window = next(
            (window for window in (30, 7, 90) if len(windows[window]) >= settings.alert_min_history_days),
            None,
        )
        baseline = median(windows[baseline_window]) if baseline_window is not None else None
        statistics = await session.get(PriceStatistics, key)
        if statistics is None:
            statistics = PriceStatistics(offer_key=key, hotel_id=hotel.id, date=rate.check_in)
            session.add(statistics)
        statistics.sample_count = len(daily)
        for window in (7, 30, 90):
            values = windows[window]
            setattr(statistics, f"median_{window}d", median(values) if values else None)
        low, high = (
            await session.execute(
                select(
                    func.min(getattr(PriceHistory, field)),
                    func.max(getattr(PriceHistory, field)),
                ).where(*conditions)
            )
        ).one()
        statistics.historical_low = min(low, amount) if low is not None else amount
        statistics.historical_high = max(high, amount) if high is not None else amount
        statistics.updated_at = now
        is_new_low = low is not None and amount < low
        baseline_days = len(windows[baseline_window]) if baseline_window is not None else 0
        if baseline is None:
            if not settings.alert_historical_lows_enabled or not is_new_low:
                statistics.features = {"status": "INSUFFICIENT_HISTORY"}
                continue
            day_column = func.date(PriceHistory.captured_at)
            if session.get_bind().dialect.name == "postgresql":
                day_column = func.date(func.timezone("UTC", PriceHistory.captured_at))
            observed_day = rate.captured_at.astimezone(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            baseline_days = await session.scalar(
                select(func.count(func.distinct(day_column))).where(
                    *conditions, PriceHistory.captured_at < observed_day
                )
            )
            if baseline_days < settings.alert_min_history_days:
                statistics.features = {"status": "INSUFFICIENT_HISTORY"}
                continue
            # An independent all-time record needs no fabricated recent median.
            # Its reference is explicitly the prior historical minimum instead.
            baseline = low
        drop = Decimal(1) - amount / baseline
        significant_drop = baseline_window is not None and drop >= settings.alert_drop_fraction
        previous_sample = await session.scalar(
            select(PriceHistory)
            .where(*conditions)
            .order_by(PriceHistory.captured_at.desc(), PriceHistory.id.desc())
            .limit(1)
        )
        analysis = {
            "baseline": str(baseline),
            "baseline_kind": "DAILY_MEDIAN" if baseline_window is not None else "PREVIOUS_HISTORICAL_LOW",
            "drop_fraction": str(settings.alert_drop_fraction),
            "historical_low": text_amount(low),
            "previous_price": text_amount(getattr(previous_sample, field)) if previous_sample else None,
            "medians": {
                str(window): text_amount(getattr(statistics, f"median_{window}d")) for window in (7, 30, 90)
            },
            "history_counts": {str(window): len(windows[window]) for window in (7, 30, 90)},
        }
        analysis.update(await calendar_features(session, hotel, rate, settings.anomaly_policy))
        analysis = comparison_percentages(amount, analysis)
        statistics.neighbor_date_median = (
            Decimal(analysis["neighbor_date_median"])
            if analysis["neighbor_date_median"] is not None
            else None
        )
        statistics.local_date_median = (
            Decimal(analysis["same_weekday_median"]) if analysis["same_weekday_median"] is not None else None
        )
        statistics.features = analysis
        if not significant_drop and not (settings.alert_historical_lows_enabled and is_new_low):
            continue
        # Do not stack more candidates while this offer is awaiting its fresh query.
        pending = await session.scalar(
            select(PriceAlert.id)
            .join(
                CrawlJob,
                CrawlJob.id == PriceAlert.verification_job_id,
            )
            .where(
                PriceAlert.hotel_id == hotel.id,
                PriceAlert.offer_key == key,
                PriceAlert.confirmed.is_(False),
                CrawlJob.status.in_(LIVE),
                PriceAlert.payload["verification"].as_string() == "PENDING",
            )
            .limit(1)
        )
        if pending:
            continue
        previous = await session.scalar(
            select(PriceAlert)
            .where(
                PriceAlert.hotel_id == hotel.id,
                PriceAlert.offer_key == key,
                PriceAlert.confirmed.is_(True),
            )
            .order_by(PriceAlert.created_at.desc(), PriceAlert.id.desc())
            .limit(1)
        )
        repeat_threshold = None
        if previous:
            previous_amount = Decimal(previous.payload["price"])
            repeat_threshold = previous_amount * (1 - settings.alert_renotify_drop_fraction)
            if amount > repeat_threshold:
                continue
        latest_attempt = await session.scalar(
            select(PriceAlert)
            .where(PriceAlert.hotel_id == hotel.id, PriceAlert.offer_key == key)
            .order_by(PriceAlert.created_at.desc(), PriceAlert.id.desc())
            .limit(1)
        )
        # A rejected/failed independent check must not silence this stay for an
        # entire day. Later scheduled observations can start a new attempt;
        # pending checks and confirmed-price thresholds still prevent spam.
        anchor = latest_attempt.id if latest_attempt else str(now.date())
        dedupe = hashlib.sha256(f"{key}:{anchor}".encode()).hexdigest()
        if await session.scalar(select(PriceAlert.id).where(PriceAlert.dedupe_key == dedupe)):
            continue
        score, level, breakdown = anomaly_score(amount, analysis, settings.anomaly_policy)
        event_type = "PRICE_DROP" if significant_drop else "NEW_HISTORICAL_LOW"
        alert = PriceAlert(
            hotel_id=hotel.id,
            offer_key=key,
            event_type=event_type,
            anomaly_score=score,
            confirmed=False,
            dedupe_key=dedupe,
            payload={
                "hotel_name": hotel.hotel_name,
                "provider": hotel.provider,
                "check_in": rate.check_in.isoformat(),
                "check_out": rate.check_out.isoformat(),
                "price": str(amount),
                "event_type": event_type,
                "new_historical_low": is_new_low,
                "historical_low": text_amount(low),
                "anomaly_score": score,
                "anomaly_level": level,
                "score_breakdown": breakdown,
                "scoring_policy": settings.anomaly_policy.model_dump(mode="json"),
                "analysis": analysis,
                "price_field": field,
                "currency": rate.currency,
                "baseline": str(baseline),
                "baseline_window_days": baseline_window,
                "baseline_kind": analysis["baseline_kind"],
                "threshold": str(baseline * (1 - settings.alert_drop_fraction) if significant_drop else low),
                "repeat_threshold": str(repeat_threshold) if repeat_threshold is not None else None,
                "history_days": baseline_days,
                "room_type": rate.room_type,
                "rate_name": rate.rate_name,
                "offer_classification": classification.as_dict(),
                "price_basis": rate.price_basis,
                "rooms": rate.rooms,
                "adults": rate.adults,
                "source_url": str(rate.source_url),
                "verification": "PENDING",
            },
        )
        session.add(alert)
        await session.flush()
        batches[(rate.check_in, rate.check_out, rate.adults, rate.rooms)].append(alert)
        created += 1
    # One fresh official-page query can verify every affected room/plan for the
    # same stay. Re-fetching it once per offer wastes the global crawl budget.
    for (check_in, check_out, adults, rooms), alerts in batches.items():
        job = await enqueue(
            session,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.VERIFY_ANOMALY,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_out,
                priority=100,
                payload={"alert_ids": sorted(alert.id for alert in alerts), "adults": adults, "rooms": rooms},
            ),
            delay_seconds=settings.alert_confirmation_seconds,
        )
        for alert in alerts:
            alert.verification_job_id = job.id
    return created


async def confirm_job_drops(session: AsyncSession, job: CrawlJob, rates: list[RateData]) -> int:
    """Confirm each offer separately, including legacy single-alert jobs."""
    ids = job.payload.get("alert_ids", [])
    if not isinstance(ids, list):
        ids = []
    legacy_id = job.payload.get("alert_id")
    if isinstance(legacy_id, str):
        ids = [*ids, legacy_id]
    confirmed = 0
    for alert_id in dict.fromkeys(item for item in ids if isinstance(item, str) and item):
        confirmed += await confirm_drop(session, alert_id, job.id, rates)
    return confirmed


async def reconcile_terminal_verifications(session: AsyncSession, limit: int = 100) -> int:
    """Close abandoned candidates, including old failures and exhausted worker leases.

    Live retries remain PENDING. Closing a candidate never creates an outbox
    entry or prevents a later ordinary observation starting a fresh query.
    """
    rows = (
        await session.execute(
            select(PriceAlert, CrawlJob)
            .join(CrawlJob, CrawlJob.id == PriceAlert.verification_job_id)
            .where(
                PriceAlert.confirmed.is_(False),
                PriceAlert.payload["verification"].as_string() == "PENDING",
                CrawlJob.status.in_(("FAILED", "CANCELLED")),
            )
            .order_by(PriceAlert.created_at, PriceAlert.id)
            .limit(limit)
            .with_for_update(of=PriceAlert, skip_locked=True)
        )
    ).all()
    for alert, job in rows:
        alert.payload = {
            **alert.payload,
            "verification": job.status,
            "verification_error": job.error_type,
            "verification_reason": "OUTSIDE_MONITORING_WINDOW"
            if outside_window_cancellation(job.error_message)
            else {
                "Provider disabled by configuration": "PROVIDER_DISABLED",
                "Stay outside the next 365 days": "OUTSIDE_MONITORING_WINDOW",
            }.get(job.error_message),
            "verification_finished_at": utcnow().isoformat(),
        }
    return len(rows)


async def confirm_drop(session: AsyncSession, alert_id: str, job_id: str, rates: list[RateData]) -> bool:
    alert = await session.scalar(select(PriceAlert).where(PriceAlert.id == alert_id).with_for_update())
    if not alert or alert.verification_job_id != job_id:
        return False
    if alert.confirmed:
        return True
    match = next((r for r in rates if r.offer_key() == alert.offer_key), None)
    payload = dict(alert.payload)
    amount = displayed_price(match) if match is not None else None
    classification = classify_offer(match) if match is not None else None
    if (
        match is None
        or not match.availability
        or not classification.alert_eligible
        or price_field(match) != payload.get("price_field", "total_price")
        or amount is None
        or not Decimal(0) < amount <= Decimal(payload["threshold"])
        or (alert.event_type == "NEW_HISTORICAL_LOW" and amount >= Decimal(payload["threshold"]))
        or (payload.get("repeat_threshold") is not None and amount > Decimal(payload["repeat_threshold"]))
    ):
        payload["verification"] = "REJECTED"
        if classification is not None and not classification.alert_eligible:
            payload["verification_reason"] = "EXCLUDED_OFFER"
            payload["offer_classification"] = classification.as_dict()
        alert.payload = payload
        return False
    payload.update(
        price=str(amount),
        verification="CONFIRMED",
        verified_at=utcnow().isoformat(),
        drop_percent=str(round((1 - amount / Decimal(payload["baseline"])) * 100, 1)),
    )
    if payload.get("analysis") and payload.get("scoring_policy"):
        payload["analysis"] = comparison_percentages(amount, payload["analysis"])
        score, level, breakdown = anomaly_score(
            amount,
            payload["analysis"],
            AnomalyPolicy.model_validate(payload["scoring_policy"]),
            confirmed=True,
        )
        alert.anomaly_score = score
        payload.update(anomaly_score=score, anomaly_level=level, score_breakdown=breakdown)
        payload["new_historical_low"] = (
            amount < Decimal(payload["historical_low"]) if payload.get("historical_low") else False
        )
    alert.payload = payload
    alert.confirmed = True
    session.add(NotificationLog(alert_id=alert.id, channel="telegram", dedupe_key=f"telegram:{alert.id}"))
    await session.flush()
    return True
