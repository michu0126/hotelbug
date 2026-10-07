"""Synthetic directory graphs exercise audits, not real global inventories."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app.api.main import app
from app.core.config import Settings
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, utcnow
from app.providers.marriott_catalog import ROOT
from app.schemas.domain import CatalogPageData, HotelData, JobInput, JobKind
from app.services.catalog_coverage import directory_coverage
from app.services.ihg_catalogs import discover_marriott, persist_catalog, record_catalog_failure
from app.services.jobs import enqueue

LEAF = "https://www.marriott.com/en/destinations/offline-test.mi"
NEXT = LEAF + "?pg=2"


def page(*, children=(), hotels=(), total=None, age=0, complete=True, status="SUCCEEDED"):
    return {
        "directory_urls": list(children),
        "hotel_ids": list(hotels),
        "reported_total": total,
        "page_complete": complete,
        "last_scan_at": (utcnow() - timedelta(days=age)).timestamp(),
        "status": status,
    }


def audit(pages):
    return directory_coverage({"pages": pages}, ROOT, utcnow(), 14)


def test_graph_requires_all_advertised_pages_and_deduplicates_overlapping_hotels():
    pages = {ROOT: page(children=[LEAF], total=2), LEAF: page(children=[NEXT], hotels=["ONE"])}
    first = audit(pages)
    assert first["catalog_coverage_status"] == "IN_PROGRESS"
    assert first["reachable_directory_pages"] == 3
    assert first["pending_directory_pages"] == 1
    assert not first["catalog_coverage_complete"]
    pages[NEXT] = page(hotels=["ONE", "TWO"])
    final = audit(pages)
    assert final["directory_unique_hotels"] == 2
    assert final["complete_directory_pages"] == 3
    assert final["catalog_coverage_complete"]


@pytest.mark.parametrize(
    "replacement,expected",
    [
        (page(hotels=["ONE"], status="FAILED"), "FAILED_PAGES"),
        (page(hotels=["ONE"], status="QUEUED"), "IN_PROGRESS"),
        (page(hotels=["ONE"], age=15), "STALE_PAGES"),
        (page(hotels=["ONE"], complete=False), "UNVERIFIED_PAGES"),
        ({"status": "SUCCEEDED"}, "UNVERIFIED_PAGES"),
        (page(hotels=["ONE"], age=-1), "STALE_PAGES"),
    ],
)
def test_failure_legacy_incomplete_or_expired_pages_cannot_prove_coverage(replacement, expected):
    result = audit({ROOT: page(children=[LEAF], total=1), LEAF: replacement})
    assert result["catalog_coverage_status"] == expected
    assert not result["catalog_coverage_complete"]


def test_count_mismatch_and_no_worldwide_total_are_not_complete():
    assert audit({ROOT: page(hotels=["ONE"], total=2)})["catalog_coverage_status"] == "COUNT_MISMATCH"
    assert audit({ROOT: page(hotels=["ONE"])})["catalog_coverage_status"] == "TRAVERSED_TOTAL_UNVERIFIED"
    assert audit({})["catalog_coverage_status"] == "NOT_STARTED"


def test_current_graph_ignores_detached_old_pages_and_cycles_do_not_double_count():
    pages = {
        ROOT: page(children=[LEAF], total=1),
        LEAF: page(children=[ROOT], hotels=["ONE"]),
        NEXT: page(hotels=["OBSOLETE"], status="FAILED"),
    }
    result = audit(pages)
    assert result["catalog_coverage_complete"]
    assert result["directory_unique_hotels"] == 1
    assert result["reachable_directory_pages"] == 2


async def test_persisted_graph_survives_sessions_and_api_reports_no_price_coverage(sessions):
    async with sessions() as session, session.begin():
        root = await enqueue(
            session,
            JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ROOT}),
        )
        await persist_catalog(
            session,
            root,
            CatalogPageData(
                provider="marriott",
                source_url=ROOT,
                directory_urls=[LEAF],
                reported_total=1,
                complete=False,
                page_complete=True,
            ),
            Settings(),
        )
    async with sessions() as session, session.begin():
        leaf = await enqueue(
            session,
            JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": LEAF}),
        )
        await persist_catalog(
            session,
            leaf,
            CatalogPageData(
                provider="marriott",
                source_url=LEAF,
                reported_total=1,
                complete=True,
                hotels=[
                    HotelData(
                        provider="marriott",
                        provider_hotel_id="ONE",
                        hotel_name="Offline test",
                        official_url="https://www.marriott.com/en-us/hotels/travel/one00-offline",
                    )
                ],
            ),
            Settings(),
        )

    async def dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/catalogs")
            assert response.status_code == 200
            item = next(row for row in response.json() if row["provider"] == "marriott")
            assert item["catalog_coverage_complete"]
            assert item["directory_unique_hotels"] == item["official_worldwide_hotels"] == 1
            assert item["hotels"] == 1 and item["hotels_with_quotes"] == 0
            assert item["hotel_candidates"] == item["candidate_tasks_dispatched"] == 0
    finally:
        app.dependency_overrides.clear()


async def test_old_success_without_graph_is_reaudited_once_not_assumed_complete(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as session, session.begin():
        session.add(
            AppSetting(
                key="catalog:marriott",
                value={
                    "pages": {
                        ROOT: {
                            "status": "SUCCEEDED",
                            "next_scan_at": (utcnow() + timedelta(days=10)).timestamp(),
                        }
                    }
                },
            )
        )
    assert await discover_marriott(sessions, queue, Settings()) == 1
    assert await discover_marriott(sessions, queue, Settings()) == 0
    async with sessions() as session:
        job = await session.scalar(select(CrawlJob))
        assert job.payload["official_url"] == ROOT


async def test_failure_preserves_edges_but_invalidates_previous_success(sessions):
    from app.core.errors import ErrorCode, ProviderError

    async with sessions() as session, session.begin():
        session.add(
            AppSetting(
                key="catalog:marriott",
                value={
                    "pages": {
                        ROOT: page(children=[LEAF], total=1),
                        LEAF: page(hotels=["ONE"]),
                    }
                },
            )
        )
        job = await enqueue(
            session,
            JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": LEAF}),
        )
        await record_catalog_failure(session, job, ProviderError(ErrorCode.TIMEOUT, "timeout"))
    async with sessions() as session:
        state = await session.get(AppSetting, "catalog:marriott")
        result = directory_coverage(state.value, ROOT, utcnow(), 14)
        assert not result["catalog_coverage_complete"]
        assert result["reachable_failed_directory_pages"] == 1
        assert state.value["pages"][LEAF]["hotel_ids"] == ["ONE"]
