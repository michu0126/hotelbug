"""Detached historical directory pages do not monopolize automatic discovery."""

import pytest
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, utcnow
from app.services.catalog_coverage import reachable_directory_pages
from app.services.ihg_catalogs import discover_browser_directory, selected_directory_pages

ROOT = "https://www.marriott.com.cn/hotel-search.mi"
COUNTRY = (
    "https://www.marriott.com.cn/search/findHotels.mi?"
    "destinationAddress.destination=Singapore&destinationAddress.country=SG&searchType=InCity&deviceType=desktop-web"
)


def state():
    return {
        "root": ROOT,
        "pages": {
            ROOT: {"directory_urls": [COUNTRY], "hotel_ids": [], "next_scan_at": utcnow().timestamp() + 3600},
            COUNTRY: {"directory_urls": [], "hotel_ids": ["SINNV"], "next_scan_at": 0},
            COUNTRY + "#/1/": {"directory_urls": [], "hotel_ids": ["SINAD"], "next_scan_at": 0},
        },
    }


def test_selected_source_counters_ignore_detached_historical_continuation():
    data = state()
    assert set(selected_directory_pages(data, "marriott")) == {ROOT, COUNTRY}
    assert COUNTRY + "#/1/" in data["pages"]  # Evidence is preserved.


def test_missing_advertised_page_is_reachable_and_cycles_are_bounded():
    data = state()
    data["pages"][COUNTRY]["directory_urls"] = [ROOT, COUNTRY + "#/1/", COUNTRY + "#/2/"]
    assert set(reachable_directory_pages(data, ROOT)) == {ROOT, COUNTRY, COUNTRY + "#/1/", COUNTRY + "#/2/"}


def test_legacy_unknown_root_graph_keeps_stored_pages_for_audit():
    data = state()
    del data["pages"][ROOT]["hotel_ids"]
    assert set(selected_directory_pages(data, "marriott")) == set(data["pages"])


@pytest.mark.parametrize("fresh_graph", [True, False])
async def test_only_current_graph_is_automatically_enqueued_after_root_refresh(sessions, queue, fresh_graph):
    data = state()
    if not fresh_graph:
        del data["pages"][ROOT]["hotel_ids"]
    settings = Settings(
        provider_overrides={"marriott": ProviderPolicy(enabled=True)}, catalog_jobs_per_tick=20
    )
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value=data))
    expected = 1 if fresh_graph else 2  # Legacy rows remain eligible until the root is audited.
    assert await discover_browser_directory(sessions, queue, settings, "marriott") == expected
    async with sessions() as db:
        urls = {j.payload["official_url"] for j in await db.scalars(select(CrawlJob))}
        assert (COUNTRY + "#/1/" in urls) is (not fresh_graph)
        stored = (await db.get(AppSetting, "catalog:marriott")).value
        assert COUNTRY + "#/1/" in stored["pages"]
