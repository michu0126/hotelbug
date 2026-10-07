"""Offline cross-group dispatch backlog and Redis-loss checks."""

from datetime import date, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import CrawlJob, utcnow
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
