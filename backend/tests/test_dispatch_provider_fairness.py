"""Offline cross-group dispatch backlog and Redis-loss checks."""

from datetime import date, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, utcnow
from app.scheduler import main as scheduler
from app.schemas.domain import JobKind
from app.services.jobs import due_dispatch_jobs


def job(provider, index, *, priority=80, hours=1, kind=JobKind.FETCH_RATE):
    return CrawlJob(
        id=f"{provider}-{index:04}",
        provider=provider,
        kind=kind,
        priority=priority,
        check_in=date.today(),
        check_out=date.today() + timedelta(days=1),
        scheduled_at=utcnow() - timedelta(hours=hours),
        dedupe_key=uuid4().hex,
    )


def settings():
    return Settings(
        global_monitoring_enabled=False,
        provider_overrides={p: ProviderPolicy(enabled=True) for p in ("ihg", "gha")},
    )


async def test_large_old_due_queue_cannot_hide_another_groups_hot_stay(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index) for index in range(200)])
        db.add(job("gha", 0, hours=0))
    assert await scheduler.tick(sessions, queue, settings()) == 100
    async with sessions() as db:
        assert (await db.get(CrawlJob, "gha-0000")).status == "QUEUED"
        assert len((await db.scalars(select(CrawlJob).where(CrawlJob.status == "QUEUED"))).all()) == 100


async def test_fair_reservation_survives_aged_backlog_and_redis_loss(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index, priority=40, hours=49) for index in range(200)])
        db.add(job("gha", 0, priority=40, hours=0))
        db.add(job("gha", 1, priority=100, kind=JobKind.VERIFY_ANOMALY))
    for _ in range(2):
        assert await scheduler.tick(sessions, queue, settings()) == 100
        ready = [await queue.pop() for _ in range(100)]
        assert ready[0] == "gha-0001" and "gha-0000" in ready
        assert len(set(ready)) == 100 and await queue.pop() is None
        await queue.redis.flushdb()
    async with sessions() as db:
        assert (await db.get(CrawlJob, "ihg-0000")).priority == 40
        assert (await db.get(CrawlJob, "gha-0000")).priority == 40


async def test_full_confirmation_batch_keeps_priority_over_normal_reservations(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index, priority=100, kind=JobKind.VERIFY_ANOMALY) for index in range(110)])
        db.add(job("gha", 0, hours=0))
    assert await scheduler.tick(sessions, queue, settings()) == 100
    ready = [await queue.pop() for _ in range(100)]
    assert all(j.startswith("ihg-") for j in ready)
    async with sessions() as db:
        assert (await db.get(CrawlJob, "gha-0000")).status == "PENDING"


async def test_all_six_groups_get_a_due_slot_without_duplicate_or_extra_dispatch(
    sessions, queue, monkeypatch
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    providers = ("marriott", "ihg", "hilton", "hyatt", "accor", "gha")
    config = Settings(
        global_monitoring_enabled=False,
        provider_overrides={p: ProviderPolicy(enabled=True) for p in providers},
    )
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index, priority=85) for index in range(200)])
        db.add_all([job(p, 0, priority=40, hours=0) for p in providers if p != "ihg"])
    assert await scheduler.tick(sessions, queue, config) == 100
    ready = [await queue.pop() for _ in range(100)]
    assert len(set(ready)) == 100 and await queue.pop() is None
    assert {p for p in providers if any(j.startswith(p + "-") for j in ready)} == set(providers)


@pytest.mark.parametrize("aged", [False, True])
async def test_reserved_groups_reach_worker_before_a_continuous_high_priority_backlog(
    sessions, queue, monkeypatch, aged
):
    """Being in Redis is insufficient if every Worker pop still favors one group."""
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    providers = ("marriott", "ihg", "hilton", "hyatt", "accor", "gha")
    config = Settings(
        global_monitoring_enabled=False,
        provider_overrides={p: ProviderPolicy(enabled=True) for p in providers},
    )
    async with sessions() as db, db.begin():
        db.add_all([job("gha", index, hours=49 if aged else 1) for index in range(200)])
        db.add_all(
            [job(p, 0, priority=70, hours=0, kind=JobKind.DISCOVER_CATALOG) for p in providers if p != "gha"]
        )
        db.add(job("gha", 9999, priority=100, kind=JobKind.VERIFY_ANOMALY))
    for _ in range(2):
        assert await scheduler.tick(sessions, queue, config) == 100
        assert await queue.pop() == "gha-9999"
        first_normal_jobs = [await queue.pop() for _ in providers]
        assert {job_id.split("-", 1)[0] for job_id in first_normal_jobs} == set(providers)
        # Replaying after Redis loss must retain both the fair heads and stored priorities.
        await queue.redis.flushdb()
    async with sessions() as db:
        assert (await db.get(CrawlJob, "gha-0000")).priority == 80
        assert (await db.get(CrawlJob, "ihg-0000")).priority == 70


@pytest.mark.parametrize("kind", [JobKind.DISCOVER_CATALOG, JobKind.DISCOVER_HOTELS])
async def test_catalog_within_a_group_is_not_starved_by_its_own_price_backlog(
    sessions, queue, monkeypatch, kind
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index, priority=80) for index in range(200)])
        db.add(job("ihg", 9999, priority=70, hours=0, kind=kind))
    await scheduler.tick(sessions, queue, settings())
    first = await queue.pop()
    assert first == "ihg-0000"
    async with sessions() as db, db.begin():
        (await db.get(CrawlJob, first)).status = "RUNNING"
    await scheduler.tick(sessions, queue, settings())
    # A group slot alone cannot make the global catalog progress if the group
    # always spends its slot querying the same existing hotels' near dates.
    assert await queue.pop() == "ihg-9999"


async def test_fair_head_is_pinned_until_claimed_not_rotated_on_every_tick(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("ihg", index, priority=80) for index in range(200)])
        db.add(job("ihg", 9999, priority=70, hours=0, kind=JobKind.DISCOVER_CATALOG))
    for expected in ("ihg-0000", "ihg-9999", "ihg-0001"):
        for _ in range(3):
            # New Scheduler/Queue instances and Redis loss cannot erase the
            # SQL head. No fast tick may demote it before a slow Worker pops.
            await queue.redis.flushdb()
            await scheduler.tick(sessions, queue, settings())
            ready = await queue.redis.zrange("hotelbug:ready", 0, 0)
            assert ready == [expected]
            async with sessions() as db:
                assert (await db.get(AppSetting, "dispatch_head:ihg")).value["job_id"] == expected
        assert await queue.pop() == expected
        async with sessions() as db, db.begin():
            (await db.get(CrawlJob, expected)).status = "RUNNING"


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "FUTURE", "DELETED"])
async def test_finished_or_deferred_pinned_head_cannot_hold_group_forever(
    sessions, queue, monkeypatch, status
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add(job("ihg", 0, priority=80))
        db.add(job("ihg", 9999, priority=70, hours=0, kind=JobKind.DISCOVER_HOTELS))
    await scheduler.tick(sessions, queue, settings())
    assert await queue.pop() == "ihg-0000"
    async with sessions() as db, db.begin():
        current = await db.get(CrawlJob, "ihg-0000")
        if status == "DELETED":
            await db.delete(current)
        elif status == "FUTURE":
            current.status = "PENDING"
            current.scheduled_at = utcnow() + timedelta(hours=1)
        else:
            current.status = status
    await scheduler.tick(sessions, queue, settings())
    assert await queue.pop() == "ihg-9999"


@pytest.mark.parametrize("status", ["RUNNING", "FAILED", "SUCCEEDED", "CANCELLED", "FUTURE"])
async def test_reservation_does_not_replay_terminal_running_or_future_jobs(sessions, status):
    async with sessions() as db, db.begin():
        excluded = job("gha", 0, priority=100, kind=JobKind.VERIFY_ANOMALY)
        if status == "FUTURE":
            excluded.scheduled_at = utcnow() + timedelta(hours=1)
        else:
            excluded.status = status
        db.add(excluded)
        db.add(job("ihg", 0))
        rows = await due_dispatch_jobs(db, utcnow(), ("gha", "ihg"))
        assert [j.id for j, _ in rows] == ["ihg-0000"]


@pytest.mark.parametrize("limit", [0, -1, 1001])
async def test_dispatch_batch_remains_bounded(sessions, limit):
    async with sessions() as db:
        with pytest.raises(ValueError, match="Dispatch batch"):
            await due_dispatch_jobs(db, utcnow(), ("gha", "ihg"), limit=limit)
