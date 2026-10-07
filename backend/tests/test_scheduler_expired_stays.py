"""Offline cross-midnight capacity checks; no hotel website or Telegram calls."""

from datetime import date, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.models.tables import CrawlJob, NotificationLog, PriceAlert, ProviderStatus, utcnow
from app.scheduler import main as scheduler
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services.alerts import reconcile_terminal_verifications
from app.services.jobs import OUTSIDE_WINDOW_MESSAGE, cancel_outside_window, enqueue, expire_pending_stays
from app.services.rates import upsert_hotel


async def test_future_deferred_expired_jobs_release_global_capacity_even_while_group_is_paused(
    sessions, queue, monkeypatch
):
    settings = Settings(
        max_pending_jobs=20,
        global_jobs_per_tick=1,
        provider_overrides={"ihg": ProviderPolicy(enabled=True), "gha": ProviderPolicy(enabled=True)},
    )
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        healthy = await upsert_hotel(
            db,
            HotelData(
                provider="gha",
                provider_hotel_id="12345",
                hotel_name="Offline healthy hotel",
                official_url="https://www.ghadiscovery.com/",
            ),
        )
        paused_until = utcnow() + timedelta(hours=6)
        paused_hotel = await upsert_hotel(
            db,
            HotelData(
                provider="ihg",
                provider_hotel_id="TEST0",
                hotel_name="Offline paused hotel",
                official_url="https://www.ihg.com/",
            ),
        )
        db.add(ProviderStatus(provider="ihg", status="BLOCKED", blocked_until=paused_until))
        old_ids = []
        for index in range(20):
            job = await enqueue(
                db,
                JobInput(
                    provider="ihg",
                    kind=JobKind.FETCH_RATE,
                    hotel_id=paused_hotel.id,
                    check_in=date.today() + timedelta(days=index),
                    check_out=date.today() + timedelta(days=index + 1),
                ),
            )
            job.check_in = date.today() - timedelta(days=index + 2)
            job.check_out = job.check_in + timedelta(days=1)
            job.scheduled_at = paused_until
            job.error_type, job.error_message, job.retry_count = "TIMEOUT", "Original public query timeout", 2
            old_ids.append(job.id)
        healthy_id = healthy.id
    assert await scheduler.tick(sessions, queue, settings) == 0
    async with sessions() as db:
        rows = (await db.scalars(select(CrawlJob).where(CrawlJob.id.in_(old_ids)))).all()
        assert all(job.status == "CANCELLED" for job in rows)
        assert all(job.error_type == "TIMEOUT" and job.retry_count == 2 for job in rows)
        assert all(job.scheduled_at.replace(tzinfo=paused_until.tzinfo) == paused_until for job in rows)
        assert all("Original public query timeout" in job.error_message for job in rows)
        new = (await db.scalars(select(CrawlJob).where(CrawlJob.hotel_id == healthy_id))).all()
        assert len(new) == 1 and new[0].status == "PENDING" and new[0].check_in == date.today()
        state = await db.get(ProviderStatus, "ihg")
        assert (
            state.status == "BLOCKED"
            and state.blocked_until.replace(tzinfo=paused_until.tzinfo) == paused_until
        )


def legacy_job(**changes):
    """Synthetic pre-existing row; invalid stays cannot enter through enqueue."""
    values = dict(
        provider="ihg",
        kind=JobKind.FETCH_RATE,
        check_in=date.today() - timedelta(days=1),
        check_out=date.today(),
        status="PENDING",
        scheduled_at=utcnow() + timedelta(hours=6),
        dedupe_key=uuid4().hex,
        error_type="TIMEOUT",
        error_message="Original query timeout",
        retry_count=2,
        payload={"adults": 2, "rooms": 1},
    )
    return CrawlJob(**(values | changes))


@pytest.mark.parametrize("kind", [JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY])
@pytest.mark.parametrize("status", ["PENDING", "QUEUED"])
async def test_all_waiting_price_kinds_expire_independent_of_future_deadline(sessions, kind, status):
    async with sessions() as db, db.begin():
        job = legacy_job(kind=kind, status=status, lease_owner="old-worker", lease_until=utcnow())
        db.add(job)
        await db.flush()
        original = job.scheduled_at, job.payload.copy(), job.created_at
        assert await expire_pending_stays(db) == 1
        assert job.status == "CANCELLED"
        assert (job.error_type, job.retry_count) == ("TIMEOUT", 2)
        assert job.error_message == "Original query timeout; " + OUTSIDE_WINDOW_MESSAGE
        assert (job.scheduled_at, job.payload, job.created_at) == original
        assert job.lease_owner is None and job.lease_until is None
        cancel_outside_window(job)
        assert job.error_message.count(OUTSIDE_WINDOW_MESSAGE) == 1


@pytest.mark.parametrize("offsets", [(None, 1), (0, None), (0, 0), (1, 0), (365, 366), (-1, 1)])
async def test_missing_reversed_and_outside_year_dates_are_retired(sessions, offsets):
    async with sessions() as db, db.begin():
        dates = [date.today() + timedelta(days=n) if n is not None else None for n in offsets]
        job = legacy_job(check_in=dates[0], check_out=dates[1], error_message=None)
        db.add(job)
        assert await expire_pending_stays(db) == 1
        assert job.status == "CANCELLED" and job.error_message == OUTSIDE_WINDOW_MESSAGE


@pytest.mark.parametrize("offsets", [(0, 1), (364, 365)])
async def test_first_and_last_valid_nights_are_preserved(sessions, offsets):
    async with sessions() as db, db.begin():
        job = legacy_job(
            check_in=date.today() + timedelta(days=offsets[0]),
            check_out=date.today() + timedelta(days=offsets[1]),
        )
        db.add(job)
        assert await expire_pending_stays(db) == 0
        assert job.status == "PENDING" and job.error_message == "Original query timeout"


@pytest.mark.parametrize("status", ["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"])
async def test_active_and_terminal_rows_are_untouched(sessions, status):
    async with sessions() as db, db.begin():
        job = legacy_job(status=status, lease_owner="active-owner", lease_until=utcnow() + timedelta(hours=1))
        db.add(job)
        assert await expire_pending_stays(db) == 0
        assert job.status == status and job.lease_owner == "active-owner"
        assert job.error_message == "Original query timeout" and job.retry_count == 2


@pytest.mark.parametrize(
    "kind", [JobKind.DISCOVER_HOTELS, JobKind.DISCOVER_CATALOG, JobKind.PROVIDER_HEALTHCHECK]
)
async def test_catalog_and_health_jobs_without_stays_are_not_expired(sessions, kind):
    async with sessions() as db, db.begin():
        job = legacy_job(kind=kind, check_in=None, check_out=None)
        db.add(job)
        assert await expire_pending_stays(db) == 0
        assert job.status == "PENDING"


async def test_bounded_cleanup_resumes_after_commit(sessions):
    async with sessions() as db, db.begin():
        db.add_all([legacy_job() for _ in range(3)])
        assert await expire_pending_stays(db, limit=2) == 2
    async with sessions() as db, db.begin():
        assert await expire_pending_stays(db, limit=2) == 1
    async with sessions() as db, db.begin():
        assert await expire_pending_stays(db, limit=2) == 0


async def test_due_expired_backlog_beyond_cleanup_batch_is_never_dispatched(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([legacy_job(scheduled_at=utcnow() - timedelta(seconds=1)) for _ in range(103)])
    assert await scheduler.tick(sessions, queue, Settings(global_monitoring_enabled=False)) == 0
    assert await queue.pop() is None
    async with sessions() as db:
        rows = (await db.scalars(select(CrawlJob))).all()
        assert len(rows) == 103 and all(row.status == "CANCELLED" for row in rows)
        assert all(row.error_message == "Original query timeout; " + OUTSIDE_WINDOW_MESSAGE for row in rows)


@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_cleanup_rejects_unbounded_batches(sessions, limit):
    async with sessions() as db, db.begin():
        with pytest.raises(ValueError, match="Expiry batch"):
            await expire_pending_stays(db, limit=limit)


@pytest.mark.parametrize("cleanup_first", [True, False])
async def test_stale_queue_reference_and_worker_guard_make_zero_provider_requests(
    sessions, queue, monkeypatch, cleanup_first
):
    execute = AsyncMock(side_effect=AssertionError("No provider request allowed"))
    slot = AsyncMock(side_effect=AssertionError("No provider slot allowed"))
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "provider_slot", slot)
    async with sessions() as db, db.begin():
        job = legacy_job(status="QUEUED", scheduled_at=utcnow() - timedelta(seconds=1))
        db.add(job)
        await db.flush()
        job_id = job.id
        if cleanup_first:
            assert await expire_pending_stays(db) == 1
    await queue.put(job_id, 100)
    assert await worker.process_one(sessions, queue, Settings())
    execute.assert_not_awaited()
    slot.assert_not_awaited()
    async with sessions() as db:
        job = await db.get(CrawlJob, job_id)
        assert job.status == "CANCELLED" and job.error_type == "TIMEOUT" and job.retry_count == 2
        assert job.error_message == "Original query timeout; " + OUTSIDE_WINDOW_MESSAGE
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0


@pytest.mark.parametrize("original", [None, "Original query timeout"])
async def test_expired_verification_keeps_failure_and_closes_candidate_without_notification(
    sessions, original
):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="ihg",
                provider_hotel_id="TEST0",
                hotel_name="Offline hotel",
                official_url="https://www.ihg.com/",
            ),
        )
        job = legacy_job(kind=JobKind.VERIFY_ANOMALY, hotel_id=hotel.id, error_message=original)
        db.add(job)
        await db.flush()
        alert = PriceAlert(
            hotel_id=hotel.id,
            offer_key="offline",
            event_type="PRICE_DROP",
            anomaly_score=100,
            verification_job_id=job.id,
            dedupe_key=uuid4().hex,
            payload={"verification": "PENDING"},
        )
        db.add(alert)
        assert await expire_pending_stays(db) == 1
        assert await reconcile_terminal_verifications(db) == 1
        assert not alert.confirmed and alert.payload["verification"] == "CANCELLED"
        assert alert.payload["verification_error"] == "TIMEOUT"
        assert alert.payload["verification_reason"] == "OUTSIDE_MONITORING_WINDOW"
        assert await reconcile_terminal_verifications(db) == 0
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0
