from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from audit_sitemap_catalog import ensure_audit_database, run_batch
from sqlalchemy import func, select

from app.api.main import app
from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.gha_browser import GHABrowserProvider
from app.providers.gha_catalog import ROOT, candidate_progress, hotel_candidate_url
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services.catalogs import discover_gha, gha_links
from app.services.jobs import enqueue

CORPORATE = ROOT + "/buy-discovery-dollars-for-red-members/terms-and-conditions"
HOTEL = ROOT + "/new-unlisted-brand/new-public-hotel"


def settings():
    return Settings(_env_file=None, provider_overrides={"gha": ProviderPolicy(enabled=True)})


@pytest.mark.parametrize(
    "path",
    [
        "/buy-discovery-dollars-for-red-members/terms-and-conditions",
        "/destination-guides/thailand",
        "/complimentary-breakfast/brand-benefit",
        "/additional-brand-benefits/red-membership-level",
        "/test-landing-page/example",
        "/fast-track-to-higher-status/example",
        "/regional-brand-d-promotions/example",
        "/scavenger-hunt-2026/terms-and-conditions",
        "/kyros-promotion/terms-and-conditions4",
        "/regent-cruises/sweepstakes-terms-and-conditions",
    ],
)
def test_observed_corporate_sections_are_not_hotel_candidates(path):
    assert hotel_candidate_url(ROOT + path) is None
    xml = f"<urlset><url><loc>{ROOT + path}</loc></url></urlset>".encode()
    assert gha_links(xml) == []


@pytest.mark.parametrize("section", ["", "/stay-offers/sale", "/dining/public-offer", "/experiences/local"])
def test_new_brands_and_real_property_offer_parents_remain_discoverable(section):
    assert hotel_candidate_url(HOTEL + section) == HOTEL


@pytest.mark.parametrize(
    "value",
    [
        None,
        123,
        "http://www.ghadiscovery.com/brand/hotel",
        "https://example.com/brand/hotel",
        "https://user:secret@www.ghadiscovery.com/brand/hotel",
        "https://www.ghadiscovery.com:8443/brand/hotel",
        HOTEL + "?token=secret",
        HOTEL + "#private",
        HOTEL + "/unknown-child",
    ],
)
def test_invalid_or_non_property_sources_are_not_dispatched(value):
    assert hotel_candidate_url(value) is None


async def test_cached_cursor_skips_corporate_entries_without_shifting_old_source_list(sessions, queue):
    other = ROOT + "/future-brand/future-hotel"
    original = [CORPORATE, HOTEL, ROOT + "/destination-guides/city", other]
    async with sessions() as db, db.begin():
        db.add(
            AppSetting(
                key="catalog:gha",
                value={"urls": original, "cursor": 0, "next_fetch_at": utcnow().timestamp() + 3600},
            )
        )

    def unexpected_request(request):
        raise AssertionError("Cached candidate filtering must not re-read a sitemap")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        for expected in (1, 1, 0):
            assert await discover_gha(sessions, queue, settings(), client) == expected
    async with sessions() as db:
        state = (await db.get(AppSetting, "catalog:gha")).value
        assert state["urls"] == original and state["cursor"] == len(original)
        assert state["skipped_non_hotel_urls"] == sorted([CORPORATE, ROOT + "/destination-guides/city"])
        jobs = (await db.scalars(select(CrawlJob))).all()
        assert {job.payload["official_url"] for job in jobs} == {HOTEL, other}
        assert all(job.status == "PENDING" for job in jobs)
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 0
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0


async def test_paused_provider_does_not_scan_or_rewrite_cached_candidate_cursor(sessions, queue):
    deadline = utcnow() + timedelta(hours=1)
    original = {"urls": [CORPORATE, HOTEL], "cursor": 0, "next_fetch_at": 0}
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:gha", value=original))
        db.add(ProviderStatus(provider="gha", status="BLOCKED", blocked_until=deadline))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("Paused provider requested a sitemap"))
    ) as client:
        assert await discover_gha(sessions, queue, settings(), client) == 0
    async with sessions() as db:
        assert (await db.get(AppSetting, "catalog:gha")).value == original
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0


async def test_old_due_corporate_job_cancels_without_request_or_losing_prior_failure(
    sessions, queue, monkeypatch
):
    async with sessions() as db, db.begin():
        job = await enqueue(
            db, JobInput(provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": CORPORATE})
        )
        job.error_type, job.error_message, job.retry_count = "TIMEOUT", "GHA catalog detail failed", 1
        job_id, due = job.id, job.scheduled_at
    execute = AsyncMock(side_effect=AssertionError("Non-hotel job must not open an official page"))
    monkeypatch.setattr(worker, "execute", execute)
    await queue.put(job_id, 70)
    assert await worker.process_one(sessions, queue, settings())
    execute.assert_not_awaited()
    async with sessions() as db:
        saved = await db.get(CrawlJob, job_id)
        assert saved.status == "CANCELLED"
        assert saved.error_type == "TIMEOUT" and saved.retry_count == 1
        assert saved.error_message == "GHA catalog detail failed; GHA public URL is not a hotel candidate"
        assert saved.scheduled_at.replace(tzinfo=due.tzinfo) == due
        assert saved.lease_owner is saved.lease_until is None
        assert await db.get(ProviderStatus, "gha") is None


async def test_rejected_corporate_detail_never_starts_a_browser(monkeypatch):
    provider = GHABrowserProvider()
    start = AsyncMock(side_effect=AssertionError("Invalid detail must fail before browser startup"))
    monkeypatch.setattr(provider, "_get_page", start)
    from app.core.errors import ProviderError

    with pytest.raises(ProviderError, match="GHA catalog URL invalid"):
        await provider.search_hotels({"official_url": CORPORATE})
    start.assert_not_awaited()


def test_progress_filters_corporate_entries_without_counting_raw_cursor_as_hotel_work():
    assert candidate_progress([CORPORATE, HOTEL, HOTEL, ROOT + "/destination-guides/city"], 2) == {
        "hotel_candidates": 1,
        "candidate_tasks_dispatched": 1,
        "excluded_non_hotel_entries": 2,
    }


async def test_catalog_api_omits_non_hotel_candidates_but_keeps_raw_source_state(sessions):
    original = {"urls": [CORPORATE, HOTEL, ROOT + "/destination-guides/city"], "cursor": 1}
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:gha", value=original))

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/catalogs")
        item = next(row for row in response.json() if row["provider"] == "gha")
        assert item["hotel_candidates"] == 1 and item["candidate_tasks_dispatched"] == 0
        assert item["excluded_non_hotel_entries"] == 2 and item["hotels"] == 0
        async with sessions() as db:
            assert (await db.get(AppSetting, "catalog:gha")).value == original
    finally:
        app.dependency_overrides.clear()


async def test_resumed_audit_cancels_old_non_hotel_then_visits_new_hotel_without_a_fake_success(
    sessions, queue, monkeypatch
):
    await ensure_audit_database(sessions, "gha")
    async with sessions() as db, db.begin():
        invalid = await enqueue(
            db, JobInput(provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": CORPORATE})
        )
        invalid.error_type, invalid.error_message, invalid.retry_count = "TIMEOUT", "Original timeout", 1
        valid = await enqueue(
            db, JobInput(provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": HOTEL})
        )
        invalid_id, valid_id = invalid.id, valid.id
    execute = AsyncMock(
        return_value=[
            HotelData(
                provider="gha", provider_hotel_id="7894", hotel_name="Offline hotel", official_url=HOTEL
            )
        ]
    )
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No source fetch needed"))
    ) as client:
        report = await run_batch(sessions, queue, settings(), client, "gha", 1, emit=lambda _: None)
    assert report["outcome"] == "BATCH_COMPLETE" and report["details_succeeded_this_run"] == 1
    assert report["non_hotel_jobs_cancelled_this_run"] == 1
    assert report["directory_quotes"] == report["notifications"] == 0
    execute.assert_awaited_once()
    assert execute.await_args.args[0].payload["official_url"] == HOTEL
    async with sessions() as db:
        assert (await db.get(CrawlJob, invalid_id)).status == "CANCELLED"
        assert (await db.get(CrawlJob, valid_id)).status == "SUCCEEDED"
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 1
