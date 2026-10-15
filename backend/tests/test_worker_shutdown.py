import asyncio
import signal
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.config import Settings
from app.crawler import worker
from app.models.tables import CrawlJob, ProviderStatus
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue


@pytest.mark.parametrize("stage", ["provider_slot", "rate_limit", "execute"])
async def test_cancel_returns_owned_job_without_source_failure(sessions, queue, monkeypatch, stage):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as session, session.begin():
        job = await enqueue(session, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = job.id
    await queue.put(job_id, 99)
    started = asyncio.Event()
    cleaned = asyncio.Event()

    async def interrupted(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    target = worker if stage == "execute" else queue
    monkeypatch.setattr(target, stage, interrupted)
    task = asyncio.create_task(worker.process_one(sessions, queue, Settings()))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    async with sessions() as session:
        current = await session.get(CrawlJob, job_id)
        assert current.status == "PENDING"
        assert current.lease_owner is None and current.lease_until is None
        assert current.retry_count == 0 and current.error_type is None
        assert await session.get(ProviderStatus, "marriott") is None
    assert not await queue.redis.keys("hotelbug:slot:*")


async def test_cancellation_never_releases_another_workers_lease(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as session, session.begin():
        job = await enqueue(session, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = job.id
    await queue.put(job_id, 99)
    started = asyncio.Event()

    async def interrupted(*_):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "execute", interrupted)
    task = asyncio.create_task(worker.process_one(sessions, queue, Settings()))
    await asyncio.wait_for(started.wait(), 3)
    async with sessions() as session, session.begin():
        job = await session.get(CrawlJob, job_id)
        job.lease_owner = "replacement-worker"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as session:
        job = await session.get(CrawlJob, job_id)
        assert job.status == "RUNNING" and job.lease_owner == "replacement-worker"
    assert not await queue.redis.keys("hotelbug:slot:*")


async def test_shutdown_stops_consumers_before_shared_resources(queue, monkeypatch):
    order = []
    started = asyncio.Event()

    async def process(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            # Real context.close() spans multiple event-loop turns; a second
            # cancel from run_worker's finally must not interrupt it.
            await asyncio.sleep(0.01)
            order.append("consumer")

    async def browsers():
        order.append("browsers")

    async def redis_close():
        order.append("redis")

    monkeypatch.setattr(worker, "process_one", process)
    monkeypatch.setattr("app.providers.browser_session.close_browser_sessions", browsers)
    monkeypatch.setattr(queue.redis, "aclose", redis_close)
    task = asyncio.create_task(worker.run_worker(Settings(worker_concurrency=2), queue.redis))
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert order == ["consumer", "consumer", "browsers", "redis"]


async def test_failed_heartbeat_cancels_sibling_consumers_and_closes_resources(monkeypatch):
    redis = Mock(set=AsyncMock(side_effect=RuntimeError("offline")), aclose=AsyncMock())
    cleanup = AsyncMock()
    consumer_cleanup = asyncio.Event()

    async def process(*_):
        try:
            await asyncio.Event().wait()
        finally:
            consumer_cleanup.set()

    monkeypatch.setattr(worker, "process_one", process)
    monkeypatch.setattr("app.providers.browser_session.close_browser_sessions", cleanup)
    with pytest.raises(RuntimeError, match="offline"):
        await worker.run_worker(Settings(worker_concurrency=1), redis)
    assert consumer_cleanup.is_set()
    cleanup.assert_awaited_once()
    redis.aclose.assert_awaited_once()


async def test_browser_cleanup_failure_still_closes_redis(queue, monkeypatch):
    cleanup = AsyncMock(side_effect=RuntimeError("disconnected"))
    closed = AsyncMock()
    started = asyncio.Event()

    async def process(*_):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(worker, "process_one", process)
    monkeypatch.setattr("app.providers.browser_session.close_browser_sessions", cleanup)
    monkeypatch.setattr(queue.redis, "aclose", closed)
    task = asyncio.create_task(worker.run_worker(Settings(worker_concurrency=1), queue.redis))
    await started.wait()
    task.cancel()
    with pytest.raises(RuntimeError, match="disconnected"):
        await task
    closed.assert_awaited_once()


async def test_sigterm_runs_cleanup_once_and_restores_handlers(monkeypatch):
    handlers = {}
    started = asyncio.Event()
    cleaning = asyncio.Event()
    finish_cleanup = asyncio.Event()
    closed = asyncio.Event()

    def install(sig, handler):
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    async def serve(*_):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await finish_cleanup.wait()
            closed.set()

    monkeypatch.setattr(worker.signal, "signal", install)
    monkeypatch.setattr(worker, "run_worker", serve)
    monkeypatch.setattr(worker.Redis, "from_url", Mock())
    task = asyncio.create_task(worker.main())
    await started.wait()
    handlers[signal.SIGTERM]()
    await cleaning.wait()
    handlers[signal.SIGTERM]()
    await asyncio.sleep(0)
    assert not task.done()
    finish_cleanup.set()
    await task
    assert closed.is_set()
    assert all(handler == signal.SIG_DFL for handler in handlers.values())
