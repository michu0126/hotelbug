"""Synthetic replay of the observed public regional -> explore redirect, not prices."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceHistory,
    ProviderStatus,
    utcnow,
)
from app.providers.ihg_browser import IHGBrowserProvider
from app.providers.ihg_catalog import ROOT, verify_directory_source
from app.providers.registry import FACTORIES
from app.schemas.domain import CatalogPageData, JobInput, JobKind
from app.services.catalog_coverage import directory_coverage
from app.services.ihg_catalogs import discover_ihg, persist_catalog, record_catalog_failure
from app.services.jobs import enqueue

SOURCE = "https://www.ihg.com/new-caledonia"


def test_observed_root_fallback_is_not_a_successful_empty_country():
    with pytest.raises(ProviderError) as raised:
        verify_directory_source(SOURCE, ROOT)
    assert raised.value.code == ErrorCode.DIRECTORY_REDIRECT
    assert "regional coverage unconfirmed" in str(raised.value)


@pytest.mark.parametrize("actual", [SOURCE, SOURCE + "/"])
def test_unchanged_directory_identity_is_accepted(actual):
    assert verify_directory_source(SOURCE, actual) is None


@pytest.mark.parametrize(
    "actual",
    [
        "https://www.ihg.com/australia",
        ROOT + "?private=SECRET",
        ROOT + "#SECRET",
        "https://example.com/explore",
        "http://www.ihg.com/explore",
        "https://www.ihg.com:8443/explore",
    ],
)
def test_other_redirects_are_not_silently_reclassified(actual):
    with pytest.raises(ProviderError) as raised:
        verify_directory_source(SOURCE, actual)
    assert raised.value.code == ErrorCode.INVALID_RESPONSE
    assert "SECRET" not in str(raised.value)


@pytest.mark.parametrize(
    "status,expected",
    [
        (200, ErrorCode.DIRECTORY_REDIRECT),
        (403, ErrorCode.BLOCKED_BY_ANTIBOT),
        (429, ErrorCode.RATE_LIMITED),
    ],
)
async def test_provider_stops_before_reading_global_hotel_cards_or_changing_access_classification(
    status, expected
):
    page = MagicMock()
    page.url = ROOT
    page.goto = AsyncMock(return_value=MagicMock(status=status))
    page.title = AsyncMock(return_value="Where to Next: Year End Edition")
    page.wait_for_function = AsyncMock()
    page.content = AsyncMock(side_effect=AssertionError("No global hotel attribution"))
    provider = IHGBrowserProvider()
    provider._get_page = AsyncMock(return_value=page)
    with pytest.raises(ProviderError) as raised:
        await provider.discover_catalog({"official_url": SOURCE})
    assert raised.value.code == expected
    page.goto.assert_awaited_once_with(SOURCE, wait_until="commit", timeout=45000)
    page.content.assert_not_awaited()


async def test_worker_records_redirect_as_failed_without_hourly_retry_prices_or_notification(
    sessions, queue, monkeypatch
):
    instance = MagicMock()
    instance.discover_catalog = AsyncMock(
        side_effect=ProviderError(
            ErrorCode.DIRECTORY_REDIRECT,
            "IHG directory redirects to worldwide explore page; regional coverage unconfirmed",
        )
    )
    instance.close = AsyncMock()
    monkeypatch.setitem(FACTORIES, "ihg", lambda: instance)
    config = Settings(provider_overrides={"ihg": ProviderPolicy(enabled=True)}, discovery_interval_days=21)
    async with sessions() as db, db.begin():
        job = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": SOURCE})
        )
        job_id = job.id
    await queue.put(job_id, 70)
    assert await process_one(sessions, queue, config)
    async with sessions() as db, db.begin():
        job = await db.get(CrawlJob, job_id)
        assert job.status == "FAILED" and job.error_type == "DIRECTORY_REDIRECT" and job.retry_count == 1
        source = (await db.get(AppSetting, "catalog:ihg")).value["pages"][SOURCE]
        assert source["status"] == "FAILED" and source["redirected_to"] == ROOT
        assert source["next_scan_at"] > (utcnow() + timedelta(days=20)).timestamp()
        for model in (Hotel, PriceHistory, NotificationLog):
            assert await db.scalar(select(func.count()).select_from(model)) == 0
        state = await db.get(ProviderStatus, "ihg")
        assert state.status == "DEGRADED" and state.blocked_until is None
    assert await discover_ihg(sessions, queue, config) == 1  # Root is still normally discoverable.
    await queue.put(job_id, 70)
    assert await process_one(sessions, queue, config)
    instance.discover_catalog.assert_awaited_once()


async def test_redirect_keeps_old_metadata_but_does_not_count_or_traverse_it_as_current_coverage(sessions):
    now = utcnow()
    stale_child = "https://www.ihg.com/old-regional-city"
    root = {
        "status": "SUCCEEDED",
        "directory_urls": [SOURCE],
        "hotel_ids": ["CURRENT"],
        "reported_total": 1,
        "page_complete": True,
        "last_scan_at": now.timestamp(),
    }
    old = {
        "status": "SUCCEEDED",
        "directory_urls": [stale_child],
        "hotel_ids": ["OLD"],
        "reported_total": 1,
        "page_complete": True,
        "last_scan_at": now.timestamp(),
    }
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:ihg", value={"pages": {ROOT: root, SOURCE: old}, "urls": []}))
        job = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": SOURCE})
        )
        await record_catalog_failure(
            db, job, ProviderError(ErrorCode.DIRECTORY_REDIRECT, "Observed fallback")
        )
        data = (await db.get(AppSetting, "catalog:ihg")).value
        assert data["pages"][SOURCE]["hotel_ids"] == ["OLD"]
        assert data["pages"][SOURCE]["directory_urls"] == [stale_child]
        coverage = directory_coverage(data, ROOT, utcnow(), 14)
        assert coverage["reachable_directory_pages"] == 2
        assert coverage["pending_directory_pages"] == 0
        assert coverage["directory_unique_hotels"] == 1
        assert coverage["redirected_directory_pages"] == 1
        assert coverage["reachable_failed_directory_pages"] == 1
        assert not coverage["catalog_coverage_complete"]


@pytest.mark.parametrize(
    "code", [ErrorCode.TIMEOUT, ErrorCode.INVALID_RESPONSE, ErrorCode.BLOCKED_BY_ANTIBOT]
)
async def test_unrelated_failure_keeps_ordinary_repair_policy(sessions, code):
    async with sessions() as db, db.begin():
        job = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": SOURCE})
        )
        await record_catalog_failure(db, job, ProviderError(code, "Synthetic failure"), Settings())
        source = (await db.get(AppSetting, "catalog:ihg")).value["pages"][SOURCE]
        assert "redirected_to" not in source
        assert (
            (utcnow() + timedelta(minutes=59)).timestamp()
            < source["next_scan_at"]
            < (utcnow() + timedelta(minutes=61)).timestamp()
        )


async def test_later_normal_source_recovery_clears_redirect_without_erasing_failure_job(sessions):
    async with sessions() as db, db.begin():
        job = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": SOURCE})
        )
        job.status, job.error_type = "FAILED", "DIRECTORY_REDIRECT"
        await record_catalog_failure(
            db, job, ProviderError(ErrorCode.DIRECTORY_REDIRECT, "Observed fallback")
        )
        result = CatalogPageData(
            provider="ihg", source_url=SOURCE, hotels=[], reported_total=0, complete=True
        )
        recovered = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": SOURCE})
        )
        assert recovered.id != job.id
        await persist_catalog(db, recovered, result, Settings())
        source = (await db.get(AppSetting, "catalog:ihg")).value["pages"][SOURCE]
        assert source["status"] == "SUCCEEDED" and "redirected_to" not in source
        assert job.status == "FAILED" and job.error_type == "DIRECTORY_REDIRECT"
