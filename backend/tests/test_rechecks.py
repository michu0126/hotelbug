"""Offline persistent calendar scheduling; never sends real hotel/TG requests."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_domain import fixture_rate
from test_pipeline import FixtureProvider

from app.core.config import Settings
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceAlert,
    PriceHistory,
    ProviderStatus,
    StayScan,
    utcnow,
)
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services import rechecks
from app.services.jobs import enqueue
from app.services.notifications import deliver_one
from app.services.rates import upsert_hotel
from app.services.rechecks import expand_rechecks
from app.services.watchlists import expand_global


async def scan(session, *, offset=20, age=4, status="AVAILABLE", suffix="one", nights=1, provider="marriott"):
    hotel = Hotel(
        id="hotel-" + suffix,
        provider=provider,
        provider_hotel_id=suffix,
        hotel_name="Offline " + suffix,
        official_url="https://www.marriott.com/",
    )
    session.add(hotel)
    await session.flush()
    stay = StayScan(
        id="scan-" + suffix,
        hotel_id=hotel.id,
        check_in=date.today() + timedelta(days=offset),
        check_out=date.today() + timedelta(days=offset + nights),
        adults=2,
        rooms=1,
        status=status,
        observed_at=utcnow() - timedelta(hours=age),
        job_id="offline",
    )
    session.add(stay)
    await session.flush()
    return hotel, stay


@pytest.mark.parametrize(
    "offset,hours,priority", [(0, 3, 80), (30, 3, 80), (31, 9, 60), (90, 9, 60), (91, 48, 40), (364, 48, 40)]
)
async def test_due_dates_use_hot_warm_cold_intervals_without_waiting_for_year_sweep(
    sessions, monkeypatch, offset, hours, priority
):
    clock = utcnow()
    monkeypatch.setattr(rechecks, "utcnow", lambda: clock)
    async with sessions() as session, session.begin():
        _, stay = await scan(session, offset=offset)
        stay.observed_at = clock - timedelta(hours=hours - 1)
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 0
        stay.observed_at = clock - timedelta(hours=hours + 1)
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1
        job = await session.scalar(select(CrawlJob))
        assert (job.check_in, job.check_out) == (stay.check_in, stay.check_out)
        assert job.priority == priority
        assert job.payload == {"adults": 2, "rooms": 1}
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 0


@pytest.mark.parametrize(
    "status,age", [("PENDING", 100), ("QUEUED", 100), ("RUNNING", 100), ("FAILED", 1), ("SUCCEEDED", 1)]
)
async def test_live_or_recent_attempts_prevent_duplicate_queries(sessions, status, age):
    async with sessions() as session, session.begin():
        hotel, stay = await scan(session)
        job = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=stay.check_in,
                check_out=stay.check_out,
            ),
        )
        job.created_at = utcnow() - timedelta(hours=age)
        job.status = status
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 0


async def test_other_guest_count_does_not_suppress_default_calendar_recheck(sessions):
    async with sessions() as session, session.begin():
        hotel, stay = await scan(session)
        await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=stay.check_in,
                check_out=stay.check_out,
                payload={"adults": 1, "rooms": 1},
            ),
        )
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1
        assert await session.scalar(select(func.count()).select_from(CrawlJob)) == 2


@pytest.mark.parametrize("status", ["AVAILABLE", "UNAVAILABLE", "NOT_OPEN"])
async def test_successful_queries_including_no_rooms_and_not_open_are_revisited(sessions, status):
    async with sessions() as session, session.begin():
        _, stay = await scan(session, status=status)
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1
        assert stay.status == status
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0


@pytest.mark.parametrize("condition", ["inactive", "disabled", "blocked", "past", "outside", "other_guests"])
async def test_ineligible_hotels_dates_and_occupancy_are_not_rechecked(sessions, condition):
    async with sessions() as session, session.begin():
        hotel, stay = await scan(session)
        enabled = ["marriott"]
        if condition == "inactive":
            hotel.active = False
        if condition == "disabled":
            enabled = []
        if condition == "blocked":
            session.add(ProviderStatus(provider="marriott", blocked_until=utcnow() + timedelta(hours=1)))
        if condition == "past":
            stay.check_in, stay.check_out = date.today() - timedelta(days=1), date.today()
        if condition == "outside":
            stay.check_out = date.today() + timedelta(days=366)
        if condition == "other_guests":
            stay.adults = 1
        assert await expand_rechecks(session, Settings(), enabled, 1) == 0


async def test_more_than_hundred_scans_survive_restart_and_are_eventually_revisited(sessions):
    async with sessions() as session, session.begin():
        for index in range(105):
            await scan(session, suffix=f"{index:03}")
    for _ in range(6):
        async with sessions() as session, session.begin():
            await expand_rechecks(session, Settings(), ["marriott"], 20)
    async with sessions() as session:
        assert await session.scalar(select(func.count(func.distinct(CrawlJob.hotel_id)))) == 105
        assert await session.get(AppSetting, "global_recheck_cursor")


async def test_legacy_multinight_rows_cannot_trap_bounded_cursor(sessions):
    async with sessions() as session, session.begin():
        for index in range(10):
            await scan(session, suffix=f"bad-{index:03}", age=20 - index, nights=2)
        await scan(session, suffix="nightly", age=5)
    for _ in range(3):
        async with sessions() as session, session.begin():
            await expand_rechecks(session, Settings(), ["marriott"], 1)
    async with sessions() as session:
        jobs = (await session.scalars(select(CrawlJob))).all()
        assert len(jobs) == 1 and jobs[0].hotel_id == "hotel-nightly"


async def test_global_budget_reserves_full_year_progress_and_one_slot_alternates(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(global_jobs_per_tick=1)
    async with sessions() as session, session.begin():
        hotel, _ = await scan(session)
        session.add(AppSetting(key=f"global_date_cursor:{hotel.id}", value={"offset": 364}))
        assert await expand_global(session, settings) == 1
        assert (await session.scalar(select(CrawlJob))).check_in == date.today() + timedelta(days=20)
    async with sessions() as session, session.begin():
        # The next call keeps its sole slot for the full-year frontier, even
        # while more known dates are due for recheck.
        await scan(session, suffix="other")
        assert await expand_global(session, settings) == 1
        assert await session.scalar(
            select(CrawlJob.id).where(CrawlJob.check_out == date.today() + timedelta(days=365))
        )


async def test_backlog_cap_includes_rechecks_and_global_off_stops_both_lanes(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(global_jobs_per_tick=20, max_pending_jobs=20)
    async with sessions() as session, session.begin():
        await scan(session)
        for index in range(19):
            await enqueue(
                session,
                JobInput(
                    provider="marriott",
                    kind=JobKind.DISCOVER_HOTELS,
                    payload={"provider_hotel_id": f"offline-{index}"},
                ),
            )
        assert await expand_global(session, settings) == 1
        assert await session.scalar(select(func.count()).select_from(CrawlJob)) == 20
        assert await expand_global(session, settings) == 0
        settings.global_monitoring_enabled = False
        assert await expand_global(session, settings) == 0


async def test_automatic_rechecks_accumulate_three_days_then_drop_is_independently_confirmed_and_sent(
    sessions, queue, monkeypatch
):
    from app.crawler import worker
    from app.services import alerts, jobs, rates

    class Clock:
        now = utcnow() - timedelta(days=3)
        price = Decimal("200")

    for module in (worker, rechecks, alerts, jobs, rates):
        monkeypatch.setattr(module, "utcnow", lambda: Clock.now)
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    requests = []

    class PublicFixture(FixtureProvider):
        async def search_rates(self, request):
            requests.append(request)
            return [
                fixture_rate().model_copy(
                    update={
                        "provider_hotel_id": request.provider_hotel_id,
                        "check_in": request.check_in,
                        "check_out": request.check_out,
                        "adults": request.adults,
                        "rooms": request.rooms,
                        "cash_price": None,
                        "tax": None,
                        "total_price": Clock.price,
                        "captured_at": Clock.now,
                    }
                )
            ]

    monkeypatch.setitem(FACTORIES, "marriott", PublicFixture)
    settings = Settings(
        global_jobs_per_tick=2, telegram_bot_token=SecretStr("offline-token"), telegram_chat_id="offline-chat"
    )
    check_in = date.today() + timedelta(days=20)
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, await PublicFixture().get_hotel_details("fixture-only"))
        first = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_in + timedelta(days=1),
            ),
        )
        first.created_at = Clock.now
        first_id = first.id
    await queue.put(first_id, 80)
    assert await worker.process_one(sessions, queue, settings)
    async with sessions() as session:
        first = await session.get(CrawlJob, first_id)
        assert first.status == "SUCCEEDED", (first.error_type, first.error_message)
        assert await session.scalar(select(StayScan.id))
    for index in range(1, 4):
        Clock.now += timedelta(days=1)
        if index == 3:
            Clock.price = Decimal("70")
        # Fresh DB sessions model a scheduler/worker restart between days.
        async with sessions() as session, session.begin():
            assert await expand_global(session, settings) >= 1
            repeat = await session.scalar(
                select(CrawlJob).where(
                    CrawlJob.kind == JobKind.FETCH_RATE,
                    CrawlJob.check_in == check_in,
                    CrawlJob.status == "PENDING",
                )
            )
            assert repeat is not None
            # ORM column defaults keep their originally-bound real clock;
            # align only this synthetic observation's creation timestamp.
            repeat.created_at = Clock.now
            repeat_id = repeat.id
        await queue.put(repeat_id, 80)
        assert await worker.process_one(sessions, queue, settings)
        async with sessions() as session:
            assert await session.scalar(select(func.count()).select_from(PriceHistory)) == index + 1
            assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
    async with sessions() as session:
        alert = await session.scalar(select(PriceAlert))
        assert not alert.confirmed and alert.payload["history_days"] == 3
        verification_id = alert.verification_job_id
        assert alert.payload["baseline"] == "200.0000"
    Clock.now += timedelta(seconds=90)
    await queue.put(verification_id, 100)
    assert await worker.process_one(sessions, queue, settings)
    sent = []

    def telegram(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        assert await deliver_one(sessions, settings, client)
        assert not await deliver_one(sessions, settings, client)
    assert len(requests) == 5  # initial + three daily rechecks + independent confirmation
    assert len(sent) == 1
    for value in ("Fixture hotel", "万豪 Marriott", check_in.isoformat(), "70", "USD", "65.0%"):
        assert value in sent[0]["text"]
    async with sessions() as session:
        assert (await session.scalar(select(NotificationLog))).status == "SENT"
        assert (await session.scalar(select(PriceAlert))).confirmed
