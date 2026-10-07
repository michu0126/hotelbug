from unittest.mock import AsyncMock

import pytest
from audit_ihg_catalog import ensure_audit_database, run_batch
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.providers.ihg_catalog import ROOT
from app.schemas.domain import CatalogPageData, HotelData, JobKind
from app.services.ihg_catalogs import directory_policy

LEAVES = ("https://www.ihg.com/adelaide-australia", "https://www.ihg.com/agra-india")


def audit_settings():
    return Settings(
        provider_overrides={"ihg": ProviderPolicy(enabled=True)},
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
    )


def catalog(source):
    if source == ROOT:
        return CatalogPageData(provider="ihg", source_url=source, complete=True, directory_urls=LEAVES)
    index = LEAVES.index(source)
    return CatalogPageData(
        provider="ihg",
        source_url=source,
        complete=True,
        reported_total=1,
        hotels=[
            HotelData(
                provider="ihg",
                provider_hotel_id=f"TEST{index}",
                hotel_name=f"Explicit offline {index}",
                official_url=f"https://www.ihg.com/holidayinnexpress/hotels/us/en/offline/test{index}/hoteldetail",
            )
        ],
    )


async def test_audit_continues_new_sources_without_retesting_root_after_queue_restart(
    sessions, queue, monkeypatch
):
    calls = []

    async def execute(job, hotel, settings):
        assert job.kind == JobKind.DISCOVER_CATALOG
        calls.append(job.payload["official_url"])
        return catalog(calls[-1])

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    first = await run_batch(sessions, queue, audit_settings(), limit=2, emit=lambda _: None)
    assert calls == [ROOT, LEAVES[0]]
    assert first["real_hotels_persisted"] == first["year_jobs_generated"] == 1
    await queue.redis.flushdb()
    second = await run_batch(sessions, queue, audit_settings(), limit=1, emit=lambda _: None)
    assert calls == [ROOT, *LEAVES]
    assert second["real_hotels_persisted"] == 2
    assert first["directory_quotes"] == second["notifications"] == 0
    async with sessions() as db:
        year_jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))).all()
        assert year_jobs and all(job.check_in < job.check_out for job in year_jobs)


async def test_excluded_prior_source_is_not_falsely_recorded_successful(sessions, queue, monkeypatch):
    calls = []

    async def execute(job, hotel, settings):
        calls.append(job.payload["official_url"])
        return catalog(calls[-1])

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    report = await run_batch(
        sessions, queue, audit_settings(), limit=2, excluded=[LEAVES[0]], emit=lambda _: None
    )
    assert calls == [ROOT, LEAVES[1]] and report["real_hotels_persisted"] == 1
    async with sessions() as db:
        state = await db.get(AppSetting, "catalog:ihg")
        assert state.value["pages"][LEAVES[0]]["status"] == "QUEUED"
        assert "hotel_ids" not in state.value["pages"][LEAVES[0]]


async def test_exclusions_persist_without_repeating_cli_flags(sessions, queue, monkeypatch):
    calls = []

    async def execute(job, hotel, settings):
        calls.append(job.payload["official_url"])
        return catalog(calls[-1])

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    await run_batch(sessions, queue, audit_settings(), limit=1, excluded=[LEAVES[0]], emit=lambda _: None)
    report = await run_batch(sessions, queue, audit_settings(), limit=1, emit=lambda _: None)
    assert calls == [ROOT, LEAVES[1]]
    assert report["excluded_previously_tested_sources"] == [LEAVES[0]]


@pytest.mark.parametrize("code", [ErrorCode.RATE_LIMITED, ErrorCode.BLOCKED_BY_ANTIBOT])
async def test_audit_preserves_pause_and_makes_no_second_request_after_rejection(
    sessions, queue, monkeypatch, code
):
    execute = AsyncMock(side_effect=ProviderError(code, "Explicit offline rejection"))
    monkeypatch.setattr(worker, "execute", execute)
    first = await run_batch(sessions, queue, audit_settings(), limit=4, emit=lambda _: None)
    assert first["outcome"] == "DIRECTORY_READ_UNSUCCESSFUL"
    async with sessions() as db:
        blocked = (await db.get(ProviderStatus, "ihg")).blocked_until
        assert blocked.replace(tzinfo=utcnow().tzinfo) > utcnow()
    second = await run_batch(sessions, queue, audit_settings(), limit=4, emit=lambda _: None)
    assert second["outcome"] == "PROVIDER_PAUSED" and execute.await_count == 1
    assert second["real_hotels_persisted"] == second["directory_quotes"] == 0
    async with sessions() as db:
        assert (await db.get(ProviderStatus, "ihg")).blocked_until == blocked


async def test_audit_stops_after_timeout_without_resetting_retry_timestamp(sessions, queue, monkeypatch):
    execute = AsyncMock(side_effect=ProviderError(ErrorCode.TIMEOUT, "Explicit offline timeout"))
    monkeypatch.setattr(worker, "execute", execute)
    report = await run_batch(sessions, queue, audit_settings(), limit=4, emit=lambda _: None)
    assert report["outcome"] == "DIRECTORY_READ_UNSUCCESSFUL" and execute.await_count == 1
    async with sessions() as db:
        job = await db.scalar(select(CrawlJob))
        assert job.status == "PENDING" and job.retry_count == 1
        assert job.scheduled_at.replace(tzinfo=utcnow().tzinfo) > utcnow()


@pytest.mark.parametrize("provider", ["marriott", "hyatt"])
async def test_other_browser_directory_audits_select_actual_registered_root(
    sessions, queue, monkeypatch, provider
):
    root, _ = directory_policy(provider)
    execute = AsyncMock(return_value=CatalogPageData(provider=provider, source_url=root, complete=True))
    monkeypatch.setattr(worker, "execute", execute)
    config = Settings(
        _env_file=None,
        provider_overrides={provider: ProviderPolicy(enabled=True)},
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
    )
    report = await run_batch(sessions, queue, config, limit=1, emit=lambda _: None, provider=provider)
    assert report["provider"] == provider and report["outcome"] == "BATCH_COMPLETE"
    assert report["directory_quotes"] == report["notifications"] == 0
    assert execute.await_args.args[0].payload == {"official_url": root}
    async with sessions() as db:
        assert (await db.get(AppSetting, "audit:directory-only")).value == {"provider": provider}


@pytest.mark.parametrize("provider", ["marriott", "hyatt"])
@pytest.mark.parametrize("code", [ErrorCode.BLOCKED_BY_ANTIBOT, ErrorCode.RATE_LIMITED])
async def test_new_provider_audit_preserves_rejection_across_process_restart(
    sessions, queue, monkeypatch, provider, code
):
    execute = AsyncMock(side_effect=ProviderError(code, "Explicit offline rejection", retry_after=20000))
    monkeypatch.setattr(worker, "execute", execute)
    config = Settings(_env_file=None, provider_overrides={provider: ProviderPolicy(enabled=True)})
    first = await run_batch(sessions, queue, config, limit=1, emit=lambda _: None, provider=provider)
    assert first["outcome"] == "DIRECTORY_READ_UNSUCCESSFUL" and first["provider_status"] == "BLOCKED"
    await queue.redis.flushdb()
    second = await run_batch(sessions, queue, config, limit=1, emit=lambda _: None, provider=provider)
    assert second["outcome"] == "PROVIDER_PAUSED" and execute.await_count == 1
    assert second["blocked_until"] == first["blocked_until"]


async def test_browser_directory_marker_cannot_be_reused_for_another_provider(sessions):
    await ensure_audit_database(sessions, "ihg")
    with pytest.raises(AssertionError, match="Different directory audit provider"):
        await ensure_audit_database(sessions, "hyatt")
