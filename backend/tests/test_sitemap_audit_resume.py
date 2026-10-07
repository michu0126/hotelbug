from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from audit_ihg_price import run_existing_price
from audit_sitemap_catalog import ensure_audit_database, run_batch
from sqlalchemy import select
from test_domain import fixture_rate

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.schemas.domain import HotelData, JobInput, JobKind, ProviderName
from app.services.jobs import enqueue
from app.services.rates import persist_rates, upsert_hotel
from app.services.watchlists import expand_global
from tests.test_catalogs import sitemap


def settings(provider="accor"):
    return Settings(
        _env_file=None,
        provider_overrides={provider: ProviderPolicy(enabled=True)},
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
    )


def details(provider, source):
    return HotelData(
        provider=provider,
        provider_hotel_id=source if provider == "accor" else "OFFLINE1",
        hotel_name="Explicit offline directory hotel",
        official_url=f"https://all.accor.com/hotel/{source}/index.en.shtml"
        if provider == "accor"
        else source,
    )


async def test_resume_official_cursor_skips_previous_sample_and_retains_history(sessions, queue, monkeypatch):
    calls, http_calls = [], []

    async def execute(job, hotel, config):
        assert job.kind == JobKind.DISCOVER_HOTELS
        code = job.payload["provider_hotel_id"]
        calls.append(code)
        return [details("accor", code)]

    def respond(request):
        http_calls.append(str(request.url))
        return httpx.Response(200, content=sitemap(["0338", "0505", "0606"]))

    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = await run_batch(
            sessions, queue, settings(), client, "accor", 1, ["0338"], emit=lambda _: None
        )
    assert calls == ["0505"] and first["year_jobs_generated"] == 1
    async with sessions() as db, db.begin():
        hotel = await db.scalar(select(Hotel))
        price_job = await db.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))
        rate = fixture_rate().model_copy(update={"provider": ProviderName.ACCOR, "provider_hotel_id": "0505"})
        assert await persist_rates(db, hotel, [rate], price_job.id) == 1
    await queue.redis.flushdb()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as restarted:
        second = await run_batch(sessions, queue, settings(), restarted, "accor", 1, emit=lambda _: None)
    assert calls == ["0505", "0606"] and len(http_calls) == 1
    assert second["real_hotels_persisted"] == 2
    assert second["total_existing_price_history"] == 1
    assert second["directory_quotes"] == second["notifications"] == 0
    assert second["excluded_previously_tested_sources"] == ["0338"]
    async with sessions() as db:
        skipped = await db.scalar(
            select(CrawlJob).where(
                CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                CrawlJob.payload["provider_hotel_id"].as_string() == "0338",
            )
        )
        assert skipped.status == "PENDING"


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
async def test_existing_due_details_use_worker_without_unnecessary_source_request(
    sessions, queue, monkeypatch, provider
):
    await ensure_audit_database(sessions, provider)
    source = {
        "accor": "0505",
        "gha": "https://www.ghadiscovery.com/brand/offline-hotel",
        "hilton": "https://www.hilton.com/en/hotels/aahhxhx-offline-hotel",
    }[provider]
    payload = {"provider_hotel_id": source} if provider == "accor" else {"official_url": source}
    async with sessions() as db, db.begin():
        await enqueue(db, JobInput(provider=provider, kind=JobKind.DISCOVER_HOTELS, payload=payload))
    execute = AsyncMock(return_value=[details(provider, source)])
    monkeypatch.setattr(worker, "execute", execute)

    def no_http(_request):
        raise AssertionError("Existing due hotel needs no additional sitemap request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_http)) as client:
        report = await run_batch(
            sessions, queue, settings(provider), client, provider, 1, emit=lambda _: None
        )
    assert report["outcome"] == "BATCH_COMPLETE" and report["real_hotels_persisted"] == 1
    assert execute.await_count == 1


@pytest.mark.parametrize("code", [ErrorCode.RATE_LIMITED, ErrorCode.BLOCKED_BY_ANTIBOT, ErrorCode.TIMEOUT])
async def test_detail_failure_preserves_cooldown_and_does_not_make_second_request(
    sessions, queue, monkeypatch, code
):
    execute = AsyncMock(side_effect=ProviderError(code, "Explicit offline failure"))
    monkeypatch.setattr(worker, "execute", execute)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=sitemap(["0505", "0606"]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        report = await run_batch(sessions, queue, settings(), client, "accor", 2, emit=lambda _: None)
    assert report["outcome"] == "DETAIL_READ_UNSUCCESSFUL" and execute.await_count == 1
    async with sessions() as db:
        original = await db.scalar(select(CrawlJob))
        retry_at = original.scheduled_at
        assert retry_at.replace(tzinfo=utcnow().tzinfo) > utcnow()
        status = await db.get(ProviderStatus, "accor")
        pause = status.blocked_until
    if code != ErrorCode.TIMEOUT:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            second = await run_batch(sessions, queue, settings(), client, "accor", 2, emit=lambda _: None)
        assert second["outcome"] == "PROVIDER_PAUSED" and execute.await_count == len(calls) == 1
    async with sessions() as db:
        assert (await db.get(CrawlJob, original.id)).scheduled_at == retry_at
        assert (await db.get(ProviderStatus, "accor")).blocked_until == pause


async def test_source_failure_stops_before_any_detail_worker(sessions, queue, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    calls = []

    def respond(request):
        calls.append(str(request.url))
        return httpx.Response(429, headers={"Retry-After": "20000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        first = await run_batch(sessions, queue, settings(), client, "accor", 2, emit=lambda _: None)
        second = await run_batch(sessions, queue, settings(), client, "accor", 2, emit=lambda _: None)
    assert first["outcome"] == "SOURCE_READ_UNSUCCESSFUL"
    assert second["outcome"] == "PROVIDER_PAUSED"
    assert first["provider_status"] == second["provider_status"] == "BLOCKED"
    assert first["blocked_until"] == second["blocked_until"]
    assert len(calls) == 1
    execute.assert_not_awaited()


async def test_previously_successful_detail_is_not_repeated_after_refresh(sessions, queue, monkeypatch):
    await ensure_audit_database(sessions, "accor")
    async with sessions() as db, db.begin():
        first = await enqueue(
            db,
            JobInput(provider="accor", kind=JobKind.DISCOVER_HOTELS, payload={"provider_hotel_id": "0505"}),
        )
        first.status = "SUCCEEDED"
        await enqueue(
            db,
            JobInput(provider="accor", kind=JobKind.DISCOVER_HOTELS, payload={"provider_hotel_id": "0505"}),
        )
        db.add(
            AppSetting(
                key="catalog:accor",
                value={
                    "codes": ["0606"],
                    "cursor": 0,
                    "refresh_at": (utcnow() + timedelta(days=14)).timestamp(),
                },
            )
        )
    execute = AsyncMock(return_value=[details("accor", "0606")])
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No source read due"))
    ) as client:
        report = await run_batch(sessions, queue, settings(), client, "accor", 1, emit=lambda _: None)
    assert report["details_succeeded_this_run"] == 1
    assert execute.await_args.args[0].payload == {"provider_hotel_id": "0606"}


async def test_audit_marker_rejects_existing_or_other_provider_database(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="not-an-audit", value={}))
    with pytest.raises(AssertionError, match="without its sitemap audit marker"):
        await ensure_audit_database(sessions, "accor")


async def test_saved_marker_is_provider_specific(sessions):
    await ensure_audit_database(sessions, "accor")
    with pytest.raises(AssertionError, match="Different audit provider"):
        await ensure_audit_database(sessions, "gha")


async def test_saved_helpdesk_entry_is_deactivated_without_retesting_or_losing_history(
    sessions, queue, monkeypatch
):
    await ensure_audit_database(sessions, "accor")
    async with sessions() as db, db.begin():
        old = await upsert_hotel(
            db,
            details("accor", "0339").model_copy(update={"hotel_name": "Test Hotel for the helpdesk resynch"}),
        )
        assert await expand_global(db, settings()) == 1
        prior_job = await db.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))
        prior_quote = fixture_rate().model_copy(
            update={
                "provider": ProviderName.ACCOR,
                "provider_hotel_id": "0339",
                "check_in": prior_job.check_in,
                "check_out": prior_job.check_out,
                "captured_at": utcnow(),
            }
        )
        assert await persist_rates(db, old, [prior_quote], prior_job.id) == 1
        prior_job.status = "SUCCEEDED"
        db.add(
            AppSetting(
                key="catalog:accor",
                value={
                    "codes": ["0340"],
                    "cursor": 0,
                    "refresh_at": (utcnow() + timedelta(days=14)).timestamp(),
                },
            )
        )
    execute = AsyncMock(return_value=[details("accor", "0340")])
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No source request due"))
    ) as client:
        report = await run_batch(sessions, queue, settings(), client, "accor", 1, emit=lambda _: None)
    assert report["real_hotels_persisted"] == report["inactive_catalog_entries"] == 1
    assert report["total_existing_price_history"] == 1
    assert report["directory_quotes"] == report["notifications"] == 0
    async with sessions() as db:
        assert (await db.get(Hotel, old.id)).active is False
        assert execute.await_args.args[0].payload == {"provider_hotel_id": "0340"}


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
async def test_automatic_sitemap_hotel_price_executes_once_and_keeps_conditions(
    sessions, queue, monkeypatch, provider
):
    await ensure_audit_database(sessions, provider)
    source = {
        "accor": "0505",
        "gha": "https://www.ghadiscovery.com/brand/offline-hotel",
        "hilton": "https://www.hilton.com/en/hotels/aahhxhx-offline-hotel",
    }[provider]
    config = settings(provider)
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, details(provider, source))
        assert await expand_global(db, config) == 1
        generated = await db.scalar(select(CrawlJob))
        quote = fixture_rate().model_copy(
            update={
                "provider": ProviderName(provider),
                "provider_hotel_id": hotel.provider_hotel_id,
                "check_in": generated.check_in,
                "check_out": generated.check_out,
                "captured_at": utcnow(),
            }
        )
    execute = AsyncMock(return_value=[quote])
    monkeypatch.setattr(worker, "execute", execute)
    first = await run_existing_price(sessions, queue, config, emit=lambda _: None, provider=provider)
    assert first["provider"] == provider and first["outcome"] == "SUCCEEDED"
    assert first["history_rows_persisted"] == 1 and first["stay_scan_status"] == "AVAILABLE"
    second = await run_existing_price(sessions, queue, config, emit=lambda _: None, provider=provider)
    assert second["outcome"] == "NO_PENDING_PRICE_DUE" and execute.await_count == 1
    with pytest.raises(AssertionError, match="isolated IHG"):
        await run_existing_price(sessions, queue, config, emit=lambda _: None)
