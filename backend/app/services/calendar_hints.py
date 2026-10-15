"""Monthly minimum hints -> durable signal -> two independent room observations.

Hints never become RateData, StayScan or a Telegram outbox item. A signal may
only reach the existing confirmation path after a public, tax-inclusive room
offer is below the historical calendar-minimum threshold. That comparison is
explicitly labeled as calendar history, not the same room/plan's history.
"""

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from statistics import median

from sqlalchemy import func, or_, select

from app.models.tables import (
    AppSetting,
    CalendarHintHistory,
    CrawlJob,
    Hotel,
    PriceAlert,
    ProviderStatus,
    utcnow,
)
from app.providers.marriott_cn_catalog import ROOT
from app.schemas.calendar_hints import CalendarHintBatch, month_window
from app.schemas.domain import JobInput, JobKind
from app.services.capacity import load_capacity
from app.services.catalog_price_source import SOURCE_MARKER, load_catalog_price_source
from app.services.jobs import LIVE, enqueue, within_monitoring_window
from app.services.offer_policy import classify_offer

SIGNAL = "CALENDAR_PRICE_DROP"
WAIT_QUOTE = "NEEDS_ROOM_QUOTE"
WAIT_CONFIRM = "NEEDS_CONFIRMATION"
SHANGHAI = timezone(timedelta(hours=8))
CANDIDATE_CURSOR = "calendar_hint_candidate_cursor"


def hint_series_key(hotel, batch, day):
    identity = [
        hotel.id,
        hotel.provider,
        day.isoformat(),
        batch.adults,
        batch.rooms,
        batch.currency,
        batch.basis,
        str(batch.source_url),
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()


async def persist_calendar_hints(session, hotel, job, batch: CalendarHintBatch, settings):
    # The browser may finish after an administrator changed the accepted source
    # or the hotel record. Re-read current records, not the detached claim-time
    # hotel, before this observation can establish history or a new signal.
    source = await load_catalog_price_source(session)
    hotel = await session.scalar(
        select(Hotel).where(Hotel.id == hotel.id).with_for_update().execution_options(populate_existing=True)
    )
    if (
        source.root != ROOT
        or not hotel
        or not hotel.active
        or hotel.provider != "marriott"
        or not hotel.official_url.startswith("https://www.marriott.com.cn/")
        or job.payload.get(SOURCE_MARKER) not in (None, ROOT)
        or batch.provider != hotel.provider
        or batch.provider_hotel_id != hotel.provider_hotel_id
    ):
        return 0
    now = utcnow()
    today = now.astimezone(SHANGHAI).date()
    saved = 0
    for day in batch.days:
        if not today <= day.check_in < today + timedelta(days=365):
            continue  # A task crossing midnight cannot save a past-date observation.
        if await session.scalar(
            select(CalendarHintHistory.id).where(
                CalendarHintHistory.job_id == job.id, CalendarHintHistory.check_in == day.check_in
            )
        ):
            continue
        key = hint_series_key(hotel, batch, day.check_in)
        history = (
            await session.scalars(
                select(CalendarHintHistory).where(
                    CalendarHintHistory.series_key == key,
                    CalendarHintHistory.captured_at < batch.captured_at,
                    CalendarHintHistory.captured_at >= batch.captured_at - timedelta(days=90),
                )
            )
        ).all()
        daily = defaultdict(list)
        current_day = batch.captured_at.astimezone(timezone.utc).date()
        for old in history:
            observed = old.captured_at.replace(tzinfo=old.captured_at.tzinfo or timezone.utc)
            if observed.date() < current_day:
                daily[observed.date()].append(old.displayed_price)
        medians = {day: median(values) for day, values in daily.items()}
        window = next(
            (
                n
                for n in (30, 7, 90)
                if sum(d >= current_day - timedelta(days=n) for d in medians)
                >= settings.alert_min_history_days
            ),
            None,
        )
        baseline = (
            median(v for d, v in medians.items() if d >= current_day - timedelta(days=window))
            if window
            else None
        )
        record = CalendarHintHistory(
            hotel_id=hotel.id,
            provider=hotel.provider,
            job_id=job.id,
            series_key=key,
            check_in=day.check_in,
            adults=batch.adults,
            rooms=batch.rooms,
            displayed_price=day.displayed_price,
            currency=batch.currency,
            basis=batch.basis,
            source_url=str(batch.source_url),
            captured_at=batch.captured_at,
            raw_response_hash=batch.raw_response_hash,
        )
        session.add(record)
        saved += 1
        if baseline is None or day.displayed_price > baseline * (1 - settings.alert_drop_fraction):
            continue
        previous = (
            await session.scalars(
                select(PriceAlert)
                .where(
                    PriceAlert.hotel_id == hotel.id,
                    PriceAlert.event_type == SIGNAL,
                    PriceAlert.payload["hint_series_key"].as_string() == key,
                )
                .order_by(PriceAlert.created_at.desc(), PriceAlert.id.desc())
                .limit(100)
            )
        ).all()
        if any(a.payload.get("verification") in (WAIT_QUOTE, WAIT_CONFIRM, "PENDING") for a in previous):
            continue
        confirmed = next((a for a in previous if a.confirmed), None)
        repeat = (
            Decimal(confirmed.payload["price"]) * (1 - settings.alert_renotify_drop_fraction)
            if confirmed
            else None
        )
        if repeat is not None and day.displayed_price > repeat:
            continue
        dedupe = hashlib.sha256(f"calendar:{key}:{job.id}".encode()).hexdigest()
        if await session.scalar(select(PriceAlert.id).where(PriceAlert.dedupe_key == dedupe)):
            continue
        session.add(
            PriceAlert(
                hotel_id=hotel.id,
                offer_key=key,
                event_type=SIGNAL,
                anomaly_score=0,
                confirmed=False,
                dedupe_key=dedupe,
                payload={
                    "event_type": SIGNAL,
                    "hint_series_key": key,
                    "hotel_name": hotel.hotel_name,
                    "provider": hotel.provider,
                    "check_in": day.check_in.isoformat(),
                    "check_out": (day.check_in + timedelta(days=1)).isoformat(),
                    "adults": batch.adults,
                    "rooms": batch.rooms,
                    "currency": batch.currency,
                    "price": str(day.displayed_price),
                    "hint_price": str(day.displayed_price),
                    "hint_captured_at": batch.captured_at.isoformat(),
                    "source_root": ROOT,
                    "baseline": str(baseline),
                    "baseline_kind": "CALENDAR_MINIMUM_DAILY_MEDIAN",
                    "baseline_window_days": window,
                    "history_days": sum(d >= current_day - timedelta(days=window) for d in medians),
                    "threshold": str(baseline * (1 - settings.alert_drop_fraction)),
                    "repeat_threshold": str(repeat) if repeat is not None else None,
                    "price_field": "calendar_hint",
                    "price_basis": "calendar_minimum",
                    "source_url": str(batch.source_url),
                    "verification": WAIT_QUOTE,
                },
            )
        )
    month = job.payload["month"]
    key = f"calendar_hint_success:{hotel.id}:{month}"
    progress = await session.get(AppSetting, key)
    value = {"completed_at": now.isoformat(), "source_root": ROOT}
    if progress:
        progress.value = value
    else:
        session.add(AppSetting(key=key, value=value))
    await session.flush()
    return saved


async def _candidate_batch(session):
    """Rotate past LIVE-bound candidates rather than repeatedly reading their head."""
    cursor = await session.get(AppSetting, CANDIDATE_CURSOR)
    last_id = cursor.value.get("alert_id", "") if cursor and isinstance(cursor.value, dict) else ""
    if not isinstance(last_id, str):
        last_id = ""
    query = select(PriceAlert).where(
        PriceAlert.event_type == SIGNAL,
        PriceAlert.confirmed.is_(False),
        PriceAlert.payload["verification"].as_string().in_((WAIT_QUOTE, WAIT_CONFIRM)),
    )
    candidates = (
        await session.scalars(
            query.where(PriceAlert.id > last_id)
            .order_by(PriceAlert.id)
            .limit(100)
            .with_for_update(skip_locked=True)
        )
    ).all()
    if not candidates and last_id:
        candidates = (
            await session.scalars(query.order_by(PriceAlert.id).limit(100).with_for_update(skip_locked=True))
        ).all()
    if candidates:
        value = {"alert_id": candidates[-1].id}
        if cursor:
            cursor.value = value
        else:
            session.add(AppSetting(key=CANDIDATE_CURSOR, value=value))
    return candidates


async def admit_calendar_candidates(session, settings, budget=2):
    """Queue-full signals persist; cleanup/rotation also run during provider pauses."""
    if budget < 1 or not settings.global_monitoring_enabled:
        return 0
    source = await load_catalog_price_source(session)
    now = utcnow()
    state = await session.get(ProviderStatus, "marriott")
    paused = state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now
    can_admit = source.root == ROOT and settings.provider_policy("marriott").enabled and not paused
    capacity = await load_capacity(session, settings) if can_admit else None
    candidates = await _candidate_batch(session)
    created = 0
    for alert in candidates:
        p = dict(alert.payload)
        if source.root is not None and source.root != ROOT:
            # Missing legacy state alone is not evidence of an admin source choice.
            alert.payload = {
                **p,
                "verification": "CANCELLED",
                "verification_reason": "CATALOG_SOURCE_CHANGED",
            }
            continue
        hotel = await session.get(Hotel, alert.hotel_id)
        check_in, check_out = date.fromisoformat(p["check_in"]), date.fromisoformat(p["check_out"])
        if (
            not hotel
            or not hotel.active
            or not hotel.official_url.startswith("https://www.marriott.com.cn/")
            or not within_monitoring_window(check_in, check_out)
        ):
            alert.payload = {
                **p,
                "verification": "CANCELLED",
                "verification_reason": "HOTEL_OR_WINDOW_CHANGED",
            }
            continue
        if not can_admit:
            continue
        quote_id = p.get("quote_job_id") if p["verification"] == WAIT_QUOTE else None
        existing = await session.get(CrawlJob, quote_id) if quote_id else None
        if existing:
            if existing.payload.get(SOURCE_MARKER) not in (None, ROOT):
                alert.payload = {
                    **p,
                    "verification": "CANCELLED",
                    "verification_reason": "QUOTE_SOURCE_CHANGED",
                }
                continue
            if existing.status in LIVE:
                continue
            alert.payload = {**p, "verification": "QUOTE_FAILED"}
            continue
        if p["verification"] == WAIT_QUOTE:
            existing = await session.scalar(
                select(CrawlJob)
                .where(
                    CrawlJob.hotel_id == hotel.id,
                    CrawlJob.check_in == check_in,
                    CrawlJob.check_out == check_out,
                    CrawlJob.status.in_(LIVE),
                    CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
                    or_(
                        CrawlJob.payload[SOURCE_MARKER].as_string().is_(None),
                        CrawlJob.payload[SOURCE_MARKER].as_string() == ROOT,
                    ),
                    func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == p["adults"],
                    func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == p["rooms"],
                )
                .order_by(CrawlJob.created_at, CrawlJob.id)
                .limit(1)
            )
            if existing:
                alert.payload = {**p, "quote_job_id": existing.id}
                continue
        if created >= budget or not capacity.available_for("marriott"):
            continue
        payload = {SOURCE_MARKER: ROOT, "calendar_signal": True, "adults": p["adults"], "rooms": p["rooms"]}
        confirming = p["verification"] == WAIT_CONFIRM
        if confirming:
            payload["alert_ids"] = [alert.id]
        job = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.VERIFY_ANOMALY if confirming else JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_out,
                priority=100 if confirming else 95,
                payload=payload,
            ),
            delay_seconds=settings.alert_confirmation_seconds if confirming else 0,
        )
        capacity.consume("marriott")
        created += 1
        if confirming:
            alert.verification_job_id = job.id
            alert.payload = {**p, "verification": "PENDING"}
        else:
            alert.payload = {**p, "quote_job_id": job.id}
    return created


async def match_calendar_candidates(session, hotel, job, rates):
    """A real tax-inclusive public offer must independently support the signal."""
    candidates = (
        await session.scalars(
            select(PriceAlert)
            .where(
                PriceAlert.hotel_id == hotel.id,
                PriceAlert.event_type == SIGNAL,
                PriceAlert.payload["verification"].as_string() == WAIT_QUOTE,
                PriceAlert.payload["quote_job_id"].as_string() == job.id,
            )
            .with_for_update()
        )
    ).all()
    source = await load_catalog_price_source(session)
    current_hotel = await session.get(Hotel, hotel.id, populate_existing=True)
    eligible_source = (
        source.root == ROOT
        and current_hotel is not None
        and current_hotel.active
        and current_hotel.provider == "marriott"
        and current_hotel.official_url.startswith("https://www.marriott.com.cn/")
        and job.payload.get(SOURCE_MARKER) in (None, ROOT)
    )
    matched = 0
    for alert in candidates:
        p = dict(alert.payload)
        if not eligible_source:
            alert.payload = {
                **p,
                "verification": "CANCELLED",
                "verification_reason": "SOURCE_OR_HOTEL_CHANGED",
            }
            continue
        captured = datetime.fromisoformat(p["hint_captured_at"])
        offers = [
            r
            for r in rates
            if r.availability
            and r.provider == current_hotel.provider
            and r.provider_hotel_id == current_hotel.provider_hotel_id
            and r.member_rate is False
            and r.total_price is not None
            and Decimal(0) < r.total_price <= Decimal(p["threshold"])
            and r.currency == p["currency"]
            and r.check_in.isoformat() == p["check_in"]
            and r.check_out.isoformat() == p["check_out"]
            and r.adults == p["adults"]
            and r.rooms == p["rooms"]
            and r.captured_at >= captured
            and str(r.source_url).startswith("https://www.marriott.com.cn/")
            and (p.get("repeat_threshold") is None or r.total_price <= Decimal(p["repeat_threshold"]))
            and classify_offer(r).alert_eligible
        ]
        if not offers:
            alert.payload = {
                **p,
                "verification": "REJECTED",
                "verification_reason": "NO_MATCHING_PUBLIC_TOTAL",
            }
            continue
        rate = min(offers, key=lambda r: (r.total_price, r.offer_key()))
        alert.offer_key = rate.offer_key()
        alert.payload = {
            **p,
            "verification": WAIT_CONFIRM,
            "price": str(rate.total_price),
            "price_field": "total_price",
            "price_basis": rate.price_basis,
            "room_type": rate.room_type,
            "rate_name": rate.rate_name,
            "offer_classification": classify_offer(rate).as_dict(),
            "source_url": str(rate.source_url),
            "first_room_captured_at": rate.captured_at.isoformat(),
        }
        matched += 1
    return matched


def hint_month_current(job):
    try:
        return month_window(job.payload.get("month")) is not None
    except (ValueError, TypeError):
        return False
