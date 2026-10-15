"""A slow Worker must get a different group on its next scheduling turn."""

from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from test_dispatch_provider_fairness import job, settings

from app.core.config import ProviderPolicy
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.scheduler import main as scheduler
from app.schemas.domain import JobKind


@pytest.mark.parametrize(
    "providers", [("gha", "ihg"), ("accor", "gha", "hilton", "hyatt", "ihg", "marriott")]
)
async def test_fast_scheduler_cannot_let_replenished_low_sorting_group_starve_other_groups(
    sessions, queue, monkeypatch, providers
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    config = settings()
    config.provider_overrides = {provider: ProviderPolicy(enabled=True) for provider in providers}
    async with sessions() as db, db.begin():
        # These IDs make Redis' equal-score lexical tie deterministic. In
        # production the same effect depends on the randomly assigned UUIDs.
        db.add_all([job(provider, index) for provider in providers for index in range(10)])
    claimed = []
    for _ in range(len(providers) * 2):
        await scheduler.tick(sessions, queue, config)
        current = await queue.pop()
        claimed.append(current.split("-", 1)[0])
        async with sessions() as db, db.begin():
            (await db.get(CrawlJob, current)).status = "SUCCEEDED"
    assert claimed == list(providers) * 2


async def test_provider_turn_is_pinned_across_ticks_transactions_and_redis_loss(sessions, queue, monkeypatch):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("gha", 0), job("ihg", 0)])
    await scheduler.tick(sessions, queue, settings())
    first = await queue.pop()
    async with sessions() as db, db.begin():
        (await db.get(CrawlJob, first)).status = "RUNNING"
        db.add(job("gha", 1))
    for _ in range(3):
        await queue.redis.flushdb()
        await scheduler.tick(sessions, queue, settings())
        assert await queue.redis.zrange("hotelbug:ready", 0, 0) == ["ihg-0000"]
        async with sessions() as db:
            assert (await db.get(AppSetting, "dispatch_provider_turn")).value == {
                "provider": "ihg",
                "job_id": "ihg-0000",
            }


@pytest.mark.parametrize("unavailable", ["paused", "disabled", "future", "finished"])
async def test_unavailable_global_head_yields_without_changing_provider_hold(
    sessions, queue, monkeypatch, unavailable
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    config = settings()
    async with sessions() as db, db.begin():
        db.add_all([job("gha", 0), job("ihg", 0)])
    await scheduler.tick(sessions, queue, config)
    # The old Redis reference can survive a pause. Replay after Redis loss
    # proves the next actually eligible group becomes the persistent head.
    await queue.redis.flushdb()
    until = utcnow() + timedelta(hours=2)
    async with sessions() as db, db.begin():
        if unavailable == "paused":
            db.add(ProviderStatus(provider="gha", blocked_until=until))
        elif unavailable == "disabled":
            config.provider_overrides["gha"].enabled = False
        elif unavailable == "future":
            (await db.get(CrawlJob, "gha-0000")).scheduled_at = until
        else:
            (await db.get(CrawlJob, "gha-0000")).status = "SUCCEEDED"
    await scheduler.tick(sessions, queue, config)
    assert await queue.pop() == "ihg-0000"
    async with sessions() as db:
        if unavailable == "paused":
            assert (await db.get(ProviderStatus, "gha")).blocked_until.replace(tzinfo=until.tzinfo) == until


async def test_verifications_keep_priority_and_do_not_consume_normal_provider_turn(
    sessions, queue, monkeypatch
):
    monkeypatch.setattr(scheduler, "healthcheck_due", AsyncMock(return_value=False))
    async with sessions() as db, db.begin():
        db.add_all([job("gha", 0), job("ihg", 0)])
        db.add(job("ihg", 999, priority=100, kind=JobKind.VERIFY_ANOMALY))
    await scheduler.tick(sessions, queue, settings())
    assert await queue.pop() == "ihg-0999"
    async with sessions() as db, db.begin():
        (await db.get(CrawlJob, "ihg-0999")).status = "SUCCEEDED"
    await scheduler.tick(sessions, queue, settings())
    assert await queue.pop() == "gha-0000"
    async with sessions() as db:
        assert (await db.get(CrawlJob, "gha-0000")).priority == 80
        assert (await db.get(CrawlJob, "ihg-0999")).priority == 100
