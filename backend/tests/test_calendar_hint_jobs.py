"""Monthly scouting uses shared admission without manufacturing rate coverage."""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, StayScan
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.scheduler import main as scheduler
from app.schemas.domain import JobKind, ProviderName
from app.services import calendar_hint_jobs as subject
from app.services.jobs import LIVE

NOW = datetime(2026, 10, 14, 2, tzinfo=timezone.utc)
MONTHS = subject.monitored_months(NOW.date())


@pytest.fixture(autouse=True)
def frozen_now(monkeypatch):
    monkeypatch.setattr(subject, "utcnow", lambda: NOW)


def settings(*enabled, **overrides):
    return Settings(
        max_pending_jobs=overrides.pop("max_pending_jobs", 1000),
        provider_overrides={name: ProviderPolicy(enabled=name in enabled) for name in ProviderName},
        **overrides,
    )


def hotel(index=0, **overrides):
    values = {
        "id": f"hotel-{index:04d}",
        "provider": "marriott",
        "provider_hotel_id": f"TEST{index}",
        "hotel_name": f"Offline Hotel {index}",
        "official_url": "https://www.marriott.com.cn/search/availabilityCalendar.mi?propertyCode=TEST",
    }
    values.update(overrides)
    return Hotel(**values)


def accepted(root=CN_ROOT, **other):
    return AppSetting(key="catalog:marriott", value={"root": root, **other})


def attempt(hotel_id, month, *, status="SUCCEEDED", age=0, scheduled_age=None, suffix="", **overrides):
    values = {
        "id": hotel_id + month + suffix,
        "provider": "marriott",
        "kind": JobKind.FETCH_CALENDAR_HINTS,
        "hotel_id": hotel_id,
        "status": status,
        "payload": {"month": month, "automatic_catalog_root": CN_ROOT},
        "created_at": NOW - timedelta(hours=age),
        "scheduled_at": NOW - timedelta(hours=age if scheduled_age is None else scheduled_age),
        "dedupe_key": hotel_id + month + suffix,
    }
    values.update(overrides)
    return CrawlJob(**values)


async def live(db):
    return list(await db.scalars(select(CrawlJob).where(CrawlJob.status.in_(LIVE))))


async def seed(db, *, count=1):
    db.add(accepted())
    db.add_all(hotel(index) for index in range(count))
    await db.flush()


@pytest.mark.parametrize(
    ("today", "first", "last", "count"),
    [
        (date(2026, 10, 14), "2026-10", "2027-10", 13),
        (date(2026, 10, 1), "2026-10", "2027-09", 12),
        (date(2028, 2, 29), "2028-02", "2029-02", 13),
        (date(2027, 12, 31), "2027-12", "2028-12", 13),
    ],
)
def test_month_list_covers_only_intersecting_365_checkin_dates(today, first, last, count):
    months = subject.monitored_months(today)
    assert len(months) == count
    assert months[0] == first
    assert months[-1] == last


async def test_default_job_is_monthly_source_marked_and_has_no_fake_stay_coverage(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        job = (await live(db))[0]
        assert job.kind == JobKind.FETCH_CALENDAR_HINTS
        assert job.hotel_id == hotel().id
        assert job.check_in is None and job.check_out is None
        assert job.payload == {"month": "2026-10", "automatic_catalog_root": CN_ROOT}
        assert job.priority == 75
        assert await db.scalar(select(func.count()).select_from(StayScan)) == 0
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0


@pytest.mark.parametrize(
    "mode", ["global_off", "provider_off", "absent_root", "en_root", "requested_cn", "paused", "zero"]
)
async def test_disabled_unaccepted_or_paused_lane_leaves_cursors_and_jobs_untouched(sessions, mode):
    async with sessions() as db, db.begin():
        db.add(hotel())
        config = settings("marriott")
        if mode == "global_off":
            config.global_monitoring_enabled = False
        elif mode == "provider_off":
            config = settings("ihg")
        if mode not in ("absent_root", "requested_cn"):
            db.add(accepted(EN_ROOT if mode == "en_root" else CN_ROOT))
        elif mode == "requested_cn":
            db.add(AppSetting(key="catalog:marriott", value={"requested_root": CN_ROOT}))
        if mode == "paused":
            db.add(ProviderStatus(provider="marriott", blocked_until=NOW + timedelta(hours=1)))
        assert await subject.expand_calendar_hints(db, config, 0 if mode == "zero" else 1) == 0
        assert not await live(db)
        assert await db.get(AppSetting, subject.HOTEL_CURSOR) is None


async def test_source_and_active_filters_apply_before_bounded_hotel_keyset(sessions):
    async with sessions() as db, db.begin():
        db.add(accepted())
        db.add_all(
            hotel(i, official_url="https://www.marriott.com/search/availabilityCalendar.mi")
            for i in range(10)
        )
        db.add_all(hotel(i, active=False) for i in range(10, 20))
        db.add(hotel(20))
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        assert (await live(db))[0].hotel_id == hotel(20).id


async def test_hotel_month_round_robin_survives_fresh_sessions_and_covers_all_months(sessions):
    async with sessions() as db, db.begin():
        await seed(db, count=2)
    for index in range(26):
        async with sessions() as db, db.begin():
            assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
            marker = await db.get(AppSetting, subject.HOTEL_CURSOR)
            assert marker.value["hotel_id"] == hotel(index % 2).id
    async with sessions() as db, db.begin():
        rows = await live(db)
        assert len(rows) == 26
        assert {(row.hotel_id, row.payload["month"]) for row in rows} == {
            (hotel(i).id, month) for i in range(2) for month in MONTHS
        }
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0


async def test_one_hotel_does_not_consume_multiple_slots_in_one_tick(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        assert await subject.expand_calendar_hints(db, settings("marriott"), budget=10) == 1


async def test_bounded_blocked_months_advance_hotel_keyset_to_unvisited_hotel(sessions):
    async with sessions() as db, db.begin():
        await seed(db, count=5)
        db.add_all(attempt(hotel(i).id, month, status="RUNNING") for i in range(4) for month in MONTHS)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        assert (await db.get(AppSetting, subject.HOTEL_CURSOR)).value == {"hotel_id": hotel(3).id}
    async with sessions() as db, db.begin():
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        assert (await db.get(AppSetting, subject.HOTEL_CURSOR)).value == {"hotel_id": hotel(4).id}


@pytest.mark.parametrize(
    ("month", "hours", "priority"),
    [("2026-10", 3, 75), ("2026-11", 3, 75), ("2026-12", 9, 55), ("2027-02", 48, 35)],
)
async def test_month_refresh_uses_nearest_covered_day_tier(sessions, month, hours, priority):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, value, age=hours - 0.1 if value == month else 0) for value in MONTHS)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        old = await db.get(CrawlJob, hotel().id + month)
        old.created_at = old.scheduled_at = NOW - timedelta(hours=hours)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        added = (await live(db))[0]
        assert added.payload["month"] == month
        assert added.priority == priority


async def test_later_success_completion_defers_old_created_job(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month, age=10 if month == MONTHS[0] else 0) for month in MONTHS)
        key = subject.SUCCESS_PREFIX + hotel().id + ":" + MONTHS[0]
        marker = AppSetting(
            key=key, value={"completed_at": (NOW - timedelta(hours=1)).isoformat(), "source_root": CN_ROOT}
        )
        db.add(marker)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        marker.value = {"completed_at": (NOW - timedelta(hours=3)).isoformat(), "source_root": CN_ROOT}
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1


@pytest.mark.parametrize("completion", ["not-a-date", None, "2026-10-14T01:00:00"])
async def test_malformed_success_timestamp_does_not_crash_or_permanently_block_month(sessions, completion):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month, age=4 if month == MONTHS[0] else 0) for month in MONTHS)
        db.add(
            AppSetting(
                key=subject.SUCCESS_PREFIX + hotel().id + ":" + MONTHS[0],
                value={"completed_at": completion, "source_root": CN_ROOT},
            )
        )
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1


async def test_failed_month_preserves_failure_and_waits_cold_interval_since_last_retry(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month) for month in MONTHS[1:])
        failed = attempt(
            hotel().id,
            MONTHS[0],
            status="FAILED",
            age=100,
            scheduled_age=47,
            error_type="PARSE_ERROR",
            error_message="Original failure",
            retry_count=3,
        )
        db.add(failed)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        failed.scheduled_at = NOW - timedelta(hours=48)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        assert failed.status == "FAILED" and failed.retry_count == 3
        assert failed.error_message == "Original failure"


async def test_future_failed_retry_schedule_is_not_reset(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month, age=100, status="FAILED", scheduled_age=-1) for month in MONTHS)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        assert not await live(db)


async def test_cancelled_scope_change_is_not_month_freshness(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month) for month in MONTHS[1:])
        db.add(attempt(hotel().id, MONTHS[0], status="CANCELLED"))
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        assert (await live(db))[0].payload["month"] == MONTHS[0]


async def test_cancelled_attempt_cannot_hide_earlier_failure_cooldown(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month) for month in MONTHS[1:])
        db.add(attempt(hotel().id, MONTHS[0], status="FAILED", age=1, suffix="-failed"))
        db.add(attempt(hotel().id, MONTHS[0], status="CANCELLED"))
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        assert not await live(db)


async def test_older_live_month_suppresses_later_terminal_attempt_with_other_payload(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(attempt(hotel().id, month, age=100) for month in MONTHS)
        db.add_all(
            attempt(
                hotel().id,
                month,
                age=120,
                suffix="-live",
                status="QUEUED",
                payload={"month": month, "unrelated": True},
            )
            for month in MONTHS
        )
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 0
        assert len(await live(db)) == 13


async def test_capacity_reserves_other_enabled_groups_first_slot(sessions):
    async with sessions() as db, db.begin():
        await seed(db, count=2)
        db.add_all(
            CrawlJob(provider="marriott", kind="DISCOVER_CATALOG", status="RUNNING", dedupe_key=f"old-{i}")
            for i in range(18)
        )
        assert (
            await subject.expand_calendar_hints(
                db, settings("marriott", "gha", max_pending_jobs=20), budget=10
            )
            == 1
        )
        assert len(await live(db)) == 19
        saved = (await db.get(AppSetting, subject.HOTEL_CURSOR)).value
        assert await subject.expand_calendar_hints(db, settings("marriott", "gha", max_pending_jobs=20)) == 0
        assert (await db.get(AppSetting, subject.HOTEL_CURSOR)).value == saved


async def test_disabled_provider_backlog_still_counts_toward_hard_capacity(sessions):
    async with sessions() as db, db.begin():
        await seed(db)
        db.add_all(
            CrawlJob(provider="ihg", kind="DISCOVER_CATALOG", status="PENDING", dedupe_key=f"old-{i}")
            for i in range(20)
        )
        assert await subject.expand_calendar_hints(db, settings("marriott", max_pending_jobs=20)) == 0
        assert len(await live(db)) == 20
        assert await db.get(AppSetting, subject.HOTEL_CURSOR) is None


async def test_shanghai_calendar_boundary_not_utc_calendar_day(sessions, monkeypatch):
    monkeypatch.setattr(subject, "utcnow", lambda: datetime(2026, 10, 31, 16, tzinfo=timezone.utc))
    async with sessions() as db, db.begin():
        await seed(db)
        assert await subject.expand_calendar_hints(db, settings("marriott")) == 1
        assert (await live(db))[0].payload["month"] == "2026-11"


async def test_scheduler_monthly_lane_does_not_also_fill_marriott_single_stay_frontier(
    sessions, queue, monkeypatch
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        await seed(db)
        db.add(hotel(1, provider="ihg", official_url="https://www.ihg.com/hotel/TEST1"))
    await scheduler.tick(sessions, queue, settings("marriott", "ihg", global_jobs_per_tick=10))
    async with sessions() as db:
        jobs = list(await db.scalars(select(CrawlJob)))
        marriott = [job for job in jobs if job.provider == "marriott"]
        assert len(marriott) == 1 and marriott[0].kind == JobKind.FETCH_CALENDAR_HINTS
        assert any(job.provider == "ihg" and job.kind == JobKind.FETCH_RATE for job in jobs)
        assert await db.get(AppSetting, "global_date_cursor:" + hotel().id) is None
