"""Offline repair of failed, unobserved dates; no official or Telegram requests."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_domain import fixture_rate
from test_pipeline import FixtureProvider
from test_rechecks import scan

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, StayScan, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobKind
from app.services import failed_stays
from app.services.failed_stays import expand_failed_stays
from app.services.rechecks import expand_rechecks
from app.services.watchlists import expand_global


async def failure(
    session, *, suffix="one", age=4, offset=20, provider="marriott", kind=JobKind.FETCH_RATE, error="TIMEOUT"
):
    hotel = Hotel(
        id="hotel-failed-" + suffix,
        provider=provider,
        provider_hotel_id=suffix,
        hotel_name="Offline failed stay " + suffix,
        official_url="https://www.marriott.com/",
    )
    session.add(hotel)
    await session.flush()
    failed = CrawlJob(
        id="failed-job-" + suffix,
        provider=provider,
        hotel_id=hotel.id,
        kind=kind,
        check_in=date.today() + timedelta(days=offset),
        check_out=date.today() + timedelta(days=offset + 1),
        status="FAILED",
        error_type=error,
        retry_count=4,
        created_at=utcnow() - timedelta(hours=age),
        scheduled_at=utcnow() - timedelta(seconds=1),
        dedupe_key="old-failed-" + suffix,
        payload={},
    )
    session.add(failed)
    await session.flush()
    return hotel, failed


@pytest.mark.parametrize(
    "offset,hours,priority", [(0, 3, 80), (30, 3, 80), (31, 9, 60), (90, 9, 60), (91, 48, 40), (364, 48, 40)]
)
async def test_missing_stays_use_normal_tier_interval_and_preserve_failed_job(
    sessions, monkeypatch, offset, hours, priority
):
    clock = utcnow()
    monkeypatch.setattr(failed_stays, "utcnow", lambda: clock)
    async with sessions() as session, session.begin():
        hotel, old = await failure(session, offset=offset)
        old.created_at = clock - timedelta(hours=hours - 1)
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0
        old.created_at = clock - timedelta(hours=hours + 1)
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 1
        new = await session.scalar(select(CrawlJob).where(CrawlJob.id != old.id))
        assert new.kind == "FETCH_RATE" and new.priority == priority
        assert (new.hotel_id, new.check_in, new.check_out) == (hotel.id, old.check_in, old.check_out)
        assert new.payload == {"adults": 2, "rooms": 1} and new.retry_count == 0
        assert old.status == "FAILED" and old.retry_count == 4
        assert await session.scalar(select(func.count()).select_from(StayScan)) == 0
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0


@pytest.mark.parametrize("kind", [JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY])
@pytest.mark.parametrize("error", ["TIMEOUT", "NETWORK_ERROR", "BROWSER_STARTUP_FAILED", "RATE_LIMITED"])
async def test_explicit_transient_failures_of_any_price_query_can_recover(sessions, kind, error):
    async with sessions() as session, session.begin():
        await failure(session, kind=kind, error=error)
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 1


@pytest.mark.parametrize(
    "error",
    [
        "BLOCKED_BY_ANTIBOT",
        "BROWSER_UNAVAILABLE",
        "PARSER_ERROR",
        "INVALID_RESPONSE",
        "PROVIDER_CHANGED",
        "TOKEN_EXPIRED",
        "NOT_IMPLEMENTED",
        "HTTP_ERROR",
        None,
    ],
)
async def test_permanent_or_unclassified_errors_are_not_blindly_replayed(sessions, error):
    async with sessions() as session, session.begin():
        await failure(session, error=error)
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0


@pytest.mark.parametrize(
    "condition",
    [
        "inactive",
        "disabled",
        "blocked",
        "future_retry",
        "past",
        "outside",
        "one_adult",
        "two_rooms",
        "cancelled",
        "pending",
        "discovery",
        "provider_mismatch",
    ],
)
async def test_scope_and_pause_rules_hold_for_repairs(sessions, condition):
    async with sessions() as session, session.begin():
        hotel, old = await failure(session)
        enabled = ["marriott"]
        if condition == "inactive":
            hotel.active = False
        elif condition == "disabled":
            enabled = []
        elif condition == "blocked":
            session.add(ProviderStatus(provider="marriott", blocked_until=utcnow() + timedelta(hours=1)))
        elif condition == "future_retry":
            old.scheduled_at = utcnow() + timedelta(hours=1)
        elif condition == "past":
            old.check_in, old.check_out = date.today() - timedelta(days=1), date.today()
        elif condition == "outside":
            old.check_in, old.check_out = (
                date.today() + timedelta(days=365),
                date.today() + timedelta(days=366),
            )
        elif condition == "one_adult":
            old.payload = {"adults": 1, "rooms": 1}
        elif condition == "two_rooms":
            old.payload = {"adults": 2, "rooms": 2}
        elif condition == "cancelled":
            old.status = "CANCELLED"
        elif condition == "pending":
            old.status = "PENDING"
        elif condition == "discovery":
            old.kind = "DISCOVER_HOTELS"
        elif condition == "provider_mismatch":
            old.provider = "gha"
        assert await expand_failed_stays(session, Settings(), enabled, 1) == 0


@pytest.mark.parametrize("status", ["AVAILABLE", "UNAVAILABLE", "NOT_OPEN"])
async def test_known_calendar_stays_remain_in_existing_revisit_lane(sessions, status):
    async with sessions() as session, session.begin():
        hotel, old = await failure(session)
        session.add(
            StayScan(
                hotel_id=hotel.id,
                check_in=old.check_in,
                check_out=old.check_out,
                adults=2,
                rooms=1,
                status=status,
                observed_at=utcnow() - timedelta(hours=5),
                job_id="older-success",
            )
        )
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1


@pytest.mark.parametrize("status", ["PENDING", "QUEUED", "RUNNING"])
async def test_live_attempt_suppresses_repair_even_if_its_timestamp_sorts_before_failure(sessions, status):
    async with sessions() as session, session.begin():
        _, old = await failure(session)
        session.add(
            CrawlJob(
                id="earlier-live",
                provider=old.provider,
                hotel_id=old.hotel_id,
                kind=JobKind.VERIFY_ANOMALY,
                check_in=old.check_in,
                check_out=old.check_out,
                status=status,
                payload={"adults": 2, "rooms": 1},
                dedupe_key="live-offline",
                created_at=old.created_at - timedelta(seconds=1),
            )
        )
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0


@pytest.mark.parametrize(
    "status,error",
    [("FAILED", "TIMEOUT"), ("FAILED", "PARSER_ERROR"), ("SUCCEEDED", None), ("CANCELLED", None)],
)
async def test_newer_same_stay_attempt_prevents_old_failure_resurrection(sessions, status, error):
    async with sessions() as session, session.begin():
        _, old = await failure(session)
        session.add(
            CrawlJob(
                provider=old.provider,
                hotel_id=old.hotel_id,
                kind=JobKind.FETCH_RATE,
                check_in=old.check_in,
                check_out=old.check_out,
                status=status,
                payload={},
                error_type=error,
                dedupe_key="newer-offline",
                created_at=utcnow() - timedelta(hours=1),
            )
        )
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 0


async def test_other_occupancy_live_attempt_does_not_hide_unobserved_default_stay(sessions):
    async with sessions() as session, session.begin():
        _, old = await failure(session)
        session.add(
            CrawlJob(
                provider=old.provider,
                hotel_id=old.hotel_id,
                kind=JobKind.FETCH_RATE,
                check_in=old.check_in,
                check_out=old.check_out,
                status="PENDING",
                payload={"adults": 1},
                dedupe_key="solo-offline",
            )
        )
        assert await expand_failed_stays(session, Settings(), ["marriott"], 1) == 1


async def test_more_than_hundred_failed_dates_resume_across_fresh_sessions(sessions):
    async with sessions() as session, session.begin():
        for index in range(105):
            await failure(session, suffix=f"{index:03}")
    for _ in range(6):
        async with sessions() as session, session.begin():
            await expand_failed_stays(session, Settings(), ["marriott"], 20)
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status == "PENDING")
            )
            == 105
        )
        assert await session.get(AppSetting, "global_failed_stay_cursor")
        assert await session.scalar(select(func.count()).select_from(StayScan)) == 0


async def test_multinight_legacy_failures_cannot_trap_bounded_cursor(sessions):
    async with sessions() as session, session.begin():
        for index in range(10):
            _, old = await failure(session, suffix=f"bad-{index}", age=20 - index)
            old.check_out += timedelta(days=1)
        await failure(session, suffix="nightly")
    for _ in range(3):
        async with sessions() as session, session.begin():
            await expand_failed_stays(session, Settings(), ["marriott"], 1)
    async with sessions() as session:
        pending = (await session.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(pending) == 1 and pending[0].hotel_id == "hotel-failed-nightly"


async def test_single_revisit_slot_alternates_and_spare_capacity_serves_other_lane(sessions):
    async with sessions() as session, session.begin():
        _, old = await failure(session)
        known, stay = await scan(session, suffix="known")
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1
        first = await session.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
        assert first.hotel_id == old.hotel_id
    async with sessions() as session, session.begin():
        await failure(session, suffix="second")
        assert await expand_rechecks(session, Settings(), ["marriott"], 1) == 1
        assert await session.scalar(
            select(CrawlJob.id).where(CrawlJob.hotel_id == known.id, CrawlJob.check_in == stay.check_in)
        )
        # The successful lane has no more due stays; repair consumes spare slots.
        assert await expand_rechecks(session, Settings(), ["marriott"], 2) == 1


async def test_global_repairs_share_budget_but_preserve_frontier_and_backlog_cap(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(global_jobs_per_tick=2, max_pending_jobs=20)
    async with sessions() as session, session.begin():
        for index in range(3):
            await failure(session, suffix=str(index))
        assert await expand_global(session, settings) == 2
        pending = (await session.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert {job.check_in for job in pending} == {date.today(), date.today() + timedelta(days=20)}
        settings.max_pending_jobs = 2
        assert await expand_global(session, settings) == 0
        settings.global_monitoring_enabled = False
        assert await expand_global(session, settings) == 0


async def test_actual_worker_failure_then_automatic_repair_creates_first_real_scan(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    calls = []

    class Recovering(FixtureProvider):
        async def search_rates(self, request):
            calls.append(request)
            if len(calls) == 1:
                raise ProviderError(ErrorCode.NETWORK_ERROR, "Offline simulated transport failure")
            return [
                fixture_rate().model_copy(
                    update={
                        "provider_hotel_id": request.provider_hotel_id,
                        "check_in": request.check_in,
                        "check_out": request.check_out,
                        "captured_at": utcnow(),
                    }
                )
            ]

    monkeypatch.setitem(FACTORIES, "marriott", Recovering)
    settings = Settings(max_retries=0)
    async with sessions() as session, session.begin():
        _, old = await failure(session)
        old.status = "PENDING"
        old.retry_count = 0
        job_id = old.id
    await queue.put(job_id, 80)
    assert await process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        old = await session.get(CrawlJob, job_id)
        assert old.status == "FAILED" and old.error_type == "NETWORK_ERROR"
        assert await session.scalar(select(func.count()).select_from(StayScan)) == 0
        old.scheduled_at = utcnow() - timedelta(seconds=1)
        assert await expand_rechecks(session, settings, ["marriott"], 1) == 1
        new = await session.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
        new_id = new.id
    await queue.redis.delete("hotelbug:limit:marriott")
    await queue.put(new_id, 80)
    assert await process_one(sessions, queue, settings)
    async with sessions() as session:
        assert len(calls) == 2
        assert (await session.get(CrawlJob, job_id)).status == "FAILED"
        assert (await session.get(CrawlJob, new_id)).status == "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert (await session.scalar(select(StayScan))).status == "AVAILABLE"
