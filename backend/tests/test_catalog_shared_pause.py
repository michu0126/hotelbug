"""Offline source rejection must also pause queued browser tasks, not just XML."""

from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.catalogs import ACCOR_SITEMAP, discover_accor
from app.services.jobs import enqueue
from tests.test_catalog_resilience import settings, sources
from tests.test_catalogs import gha_map, sitemap


@pytest.mark.parametrize(
    "provider,stage",
    [("accor", "root"), ("gha", "root"), ("gha", "map"), ("hilton", "root"), ("hilton", "map")],
)
@pytest.mark.parametrize(
    "status,delay,code",
    [(401, 21600, "BLOCKED_BY_ANTIBOT"), (403, 21600, "BLOCKED_BY_ANTIBOT"), (429, 20000, "RATE_LIMITED")],
)
async def test_source_rejection_pauses_cached_dispatch_and_existing_worker_across_restart(
    sessions, queue, monkeypatch, provider, stage, status, delay, code
):
    now = utcnow()
    if provider == "accor":
        discover, source = discover_accor, ACCOR_SITEMAP
        data = {"codes": ["0505", "0606"], "cursor": 0, "refresh_at": 0}
        payload = {"provider_hotel_id": "0707"}
    else:
        discover, index, maps, hotel = sources(provider)
        source = index if stage == "root" else maps[0]
        data = {
            "maps": maps,
            "map_cursor": 0,
            "urls": [hotel],
            "cursor": 0,
            "refresh_at": 0 if stage == "root" else (now + timedelta(days=14)).timestamp(),
        }
        payload = {"official_url": hotel}
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:" + provider, value=data))
        job = await enqueue(db, JobInput(provider=provider, kind=JobKind.DISCOVER_HOTELS, payload=payload))
        job_id = job.id
    execute = AsyncMock(side_effect=AssertionError("Paused browser must not execute"))
    monkeypatch.setattr(worker, "execute", execute)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Retry-After": str(delay)} if status == 429 else {})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await discover(sessions, queue, settings(provider), client) == 0
    async with sessions() as db:
        state = await db.get(ProviderStatus, provider)
        assert state is not None and state.status == "BLOCKED" and state.last_error == code
        deadline = state.blocked_until
        assert deadline.replace(tzinfo=now.tzinfo) >= now + timedelta(seconds=delay)
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 0
    await queue.redis.flushdb()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as restarted:
        assert await discover(sessions, queue, settings(provider), restarted) == 0
    await queue.put(job_id, 70)
    await worker.process_one(sessions, queue, settings(provider))
    execute.assert_not_awaited()
    assert calls == [source]
    async with sessions() as db:
        state = await db.get(ProviderStatus, provider)
        assert state.blocked_until == deadline
        assert state.requests_today == state.success_count == state.failure_count == 0
        job = await db.get(CrawlJob, job_id)
        assert job.status == "PENDING" and job.scheduled_at == deadline and job.retry_count == 0


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
@pytest.mark.parametrize("status", [404, 500])
async def test_ordinary_source_failure_keeps_cached_hotels_available(sessions, queue, provider, status):
    if provider == "accor":
        discover = discover_accor
        data = {"codes": ["0505"], "cursor": 0, "refresh_at": 0}
    else:
        discover, _, maps, hotel = sources(provider)
        data = {"maps": maps, "urls": [hotel], "cursor": 0, "refresh_at": 0}
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:" + provider, value=data))
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
        assert await discover(sessions, queue, settings(provider), client) == 1
    async with sessions() as db:
        assert await db.get(ProviderStatus, provider) is None


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
async def test_pause_expiry_resumes_source_reads_without_inventing_scan(
    sessions, queue, monkeypatch, provider
):
    from app.services import catalogs

    now = utcnow()
    monkeypatch.setattr(catalogs, "utcnow", lambda: now)
    if provider == "accor":
        discover = discover_accor
        data = {"codes": ["0505", "0606"], "cursor": 1, "refresh_at": 0}
        content = sitemap(["0505", "0606"])
    else:
        discover, _, maps, hotel = sources(provider)
        data = {
            "maps": maps,
            "map_cursor": 0,
            "urls": [hotel],
            "cursor": 1,
            "refresh_at": (now + timedelta(days=14)).timestamp(),
        }
        content = gha_map([hotel])
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:" + provider, value=data))
    recovered = False
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return (
            httpx.Response(200, content=content)
            if recovered
            else httpx.Response(429, headers={"Retry-After": "20000"})
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await discover(sessions, queue, settings(provider), client) == 0
        async with sessions() as db:
            saved = (await db.get(AppSetting, "catalog:" + provider)).value
            assert saved["cursor"] == 1
            assert saved["source_pause_until"] == (now + timedelta(seconds=20000)).timestamp()
        now += timedelta(seconds=20001)
        recovered = True
        assert await discover(sessions, queue, settings(provider), client) == (
            1 if provider == "accor" else 0
        )
        assert len(calls) == 2
        async with sessions() as db:
            saved = (await db.get(AppSetting, "catalog:" + provider)).value
            assert saved.get("source_pause_until", 0) <= now.timestamp()
            assert saved["cursor"] == 1
            assert saved["last_error"] is None
            # Source recovery alone must not invent a successful price scan.
            state = await db.get(ProviderStatus, provider)
            assert state.last_success is None and state.success_count == 0


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
@pytest.mark.parametrize("result", ["success", "timeout", "shorter_rate_limit"])
async def test_already_running_browser_cannot_clear_or_shorten_source_pause(
    sessions, queue, monkeypatch, provider, result
):
    if provider == "accor":
        discover = discover_accor
        payload = {"provider_hotel_id": "0505"}
    else:
        discover, _, _, hotel = sources(provider)
        payload = {"official_url": hotel}
    async with sessions() as db, db.begin():
        job = await enqueue(db, JobInput(provider=provider, kind=JobKind.DISCOVER_HOTELS, payload=payload))
        job_id = job.id
    deadline = None

    async def execute(*_):
        nonlocal deadline
        # Scheduler finishes a separate source request while the browser is
        # already RUNNING. No actual external request is used in this test.
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            assert await discover(sessions, queue, settings(provider), client) == 0
        async with sessions() as db:
            deadline = (await db.get(ProviderStatus, provider)).blocked_until
        if result == "timeout":
            raise ProviderError(ErrorCode.TIMEOUT, "Offline concurrent browser timeout")
        if result == "shorter_rate_limit":
            raise ProviderError(ErrorCode.RATE_LIMITED, "Offline rate limit", retry_after=3600)
        return []

    monkeypatch.setattr(worker, "execute", execute)
    await queue.put(job_id, 70)
    assert await worker.process_one(sessions, queue, settings(provider))
    async with sessions() as db:
        state = await db.get(ProviderStatus, provider)
        assert state.status == "BLOCKED" and state.blocked_until == deadline
        assert state.last_error == (
            "RATE_LIMITED" if result == "shorter_rate_limit" else "BLOCKED_BY_ANTIBOT"
        )
        assert state.requests_today == 1
        current = await db.get(CrawlJob, job_id)
        assert current.status == ("SUCCEEDED" if result == "success" else "PENDING")
        if result != "success":
            assert current.scheduled_at == deadline
