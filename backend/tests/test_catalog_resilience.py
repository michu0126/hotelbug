"""Offline source failures must not silently drop the rest of a global catalog."""

from datetime import timedelta
from email.utils import format_datetime

import httpx
import pytest
from sqlalchemy import func, select

from app.api.main import app
from app.core.config import ProviderPolicy, Settings
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.services.catalogs import (
    GHA_SITEMAP,
    HILTON_SITEMAP,
    catalog_retry,
    discover_accor,
    discover_gha,
    discover_hilton,
    read_sitemaps,
)
from tests.test_catalogs import gha_map


def sources(provider):
    if provider == "gha":
        root = "https://www.ghadiscovery.com"
        return (
            discover_gha,
            GHA_SITEMAP,
            [root + f"/sitemap-{i}.xml" for i in range(1, 4)],
            root + "/brand/hotel-one",
        )
    root = "https://www.hilton.com"
    return (
        discover_hilton,
        HILTON_SITEMAP,
        [root + f"/sitemap/en/sitemap-en-prop-hp-00{i}.xml" for i in range(1, 4)],
        root + "/en/hotels/aahhxhx-hampton-aachen-tivoli",
    )


def settings(provider):
    return Settings(provider_overrides={provider: ProviderPolicy(enabled=True)})


async def seed(sessions, provider, **changes):
    _, _, maps, _ = sources(provider)
    data = {
        "maps": maps,
        "map_cursor": 0,
        "refresh_at": (utcnow() + timedelta(days=14)).timestamp(),
        **changes,
    }
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:" + provider, value=data))
    return data


async def state(sessions, provider):
    async with sessions() as db:
        return (await db.get(AppSetting, "catalog:" + provider)).value


async def advance(sessions, provider, *, due_retries=False):
    # Simulate later timer ticks; production code never shortens retry deadlines.
    async with sessions() as db, db.begin():
        row = await db.get(AppSetting, "catalog:" + provider)
        data = {**row.value, "next_fetch_at": 0}
        if due_retries:
            data["map_failures"] = {
                url: {**item, "next_retry_at": 0} for url, item in data.get("map_failures", {}).items()
            }
        row.value = data


@pytest.mark.parametrize("provider", ["gha", "hilton"])
async def test_failed_map_does_not_trap_later_maps_and_is_repaired_after_restart(sessions, queue, provider):
    discover, _, maps, hotel = sources(provider)
    await seed(sessions, provider)
    calls = []
    recovered = False

    def response(request):
        calls.append(str(request.url))
        if str(request.url) == maps[0] and not recovered:
            return httpx.Response(503)
        return httpx.Response(200, content=gha_map([hotel]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover(sessions, queue, settings(provider), client) == 0
        failed = await state(sessions, provider)
        assert failed["map_cursor"] == 1 and not read_sitemaps(failed)
        assert set(failed["map_failures"]) == {maps[0]}
        await advance(sessions, provider)
        assert await discover(sessions, queue, settings(provider), client) == 1
        assert calls == maps[:2]
        assert read_sitemaps(await state(sessions, provider)) == {maps[1]}
    # New HTTP client and Settings simulate a restarted process, with only the
    # persisted AppSetting as the repair cursor; duplicate hotel links stay deduped.
    recovered = True
    await advance(sessions, provider, due_retries=True)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as restarted:
        assert await discover(sessions, queue, settings(provider), restarted) == 0
        repaired = await state(sessions, provider)
        assert not repaired["map_failures"] and read_sitemaps(repaired) == set(maps[:2])
        assert repaired["map_cursor"] == 2
        await advance(sessions, provider)
        assert await discover(sessions, queue, settings(provider), restarted) == 0
    assert calls == [maps[0], maps[1], maps[0], maps[2]]
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1


@pytest.mark.parametrize("provider", ["gha", "hilton"])
async def test_failed_root_refresh_keeps_known_maps_progressing(sessions, queue, provider):
    discover, index, maps, hotel = sources(provider)
    await seed(sessions, provider, refresh_at=0)
    calls = []

    def response(request):
        calls.append(str(request.url))
        return (
            httpx.Response(500)
            if str(request.url) == index
            else httpx.Response(200, content=gha_map([hotel]))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover(sessions, queue, settings(provider), client) == 0
        failed = await state(sessions, provider)
        assert failed["index_retry_at"] > utcnow().timestamp()
        await advance(sessions, provider)
        assert await discover(sessions, queue, settings(provider), client) == 1
        await advance(sessions, provider)
        assert await discover(sessions, queue, settings(provider), client) == 0
    assert calls == [index, maps[0], maps[1]]
    saved = await state(sessions, provider)
    assert saved["index_error"] == "HTTPStatusError" and saved["last_error"] == "HTTPStatusError"


async def test_overdue_repairs_and_unseen_sources_alternate_without_starvation(sessions, queue):
    discover, _, maps, _ = sources("gha")
    await seed(
        sessions,
        "gha",
        map_cursor=1,
        map_failures={maps[0]: {"next_retry_at": 0, "error": "HTTPStatusError"}},
    )
    calls = []

    def response(request):
        calls.append(str(request.url))
        return (
            httpx.Response(500) if str(request.url) == maps[0] else httpx.Response(200, content=gha_map([]))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        for _ in range(4):
            await discover(sessions, queue, settings("gha"), client)
            await advance(sessions, "gha", due_retries=True)
    assert calls == [maps[0], maps[1], maps[0], maps[2]]


@pytest.mark.parametrize("status,headers,delay", [(429, {"Retry-After": "20000"}, 20000), (403, {}, 21600)])
async def test_explicit_rejection_pauses_sources_instead_of_switching_urls(
    sessions, queue, status, headers, delay
):
    discover, _, maps, _ = sources("gha")
    await seed(sessions, "gha")
    before = utcnow().timestamp()
    calls = []

    def response(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers=headers)

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        for _ in range(3):
            assert await discover(sessions, queue, settings("gha"), client) == 0
    assert calls == [maps[0]]
    saved = await state(sessions, "gha")
    assert saved["next_fetch_at"] >= before + delay
    assert saved["map_failures"][maps[0]]["next_retry_at"] >= before + delay


@pytest.mark.parametrize(
    "value,expected", [("NaN", 3600), ("inf", 3600), ("-1", 3600), ("bad", 3600), ("5", 30)]
)
def test_retry_after_invalid_values_and_minimum_spacing(value, expected):
    response = httpx.Response(429, headers={"Retry-After": value}, request=httpx.Request("GET", GHA_SITEMAP))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    assert catalog_retry(caught.value, utcnow()) == (expected, True)


def test_retry_after_http_date_is_honored():
    now = utcnow().replace(microsecond=0)
    response = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(now + timedelta(hours=5))},
        request=httpx.Request("GET", GHA_SITEMAP),
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    assert catalog_retry(caught.value, now) == (18000, True)


@pytest.mark.parametrize("provider", ["gha", "hilton", "accor"])
async def test_provider_pause_applies_to_catalog_requests_and_candidate_jobs(sessions, queue, provider):
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider=provider, blocked_until=utcnow() + timedelta(hours=1)))
    discover = {"gha": discover_gha, "hilton": discover_hilton, "accor": discover_accor}[provider]

    def response(_request):
        raise AssertionError("Paused provider attempted a network request")

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover(sessions, queue, settings(provider), client) == 0
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0


async def test_api_counts_successes_not_advanced_failed_cursor(sessions):
    _, _, maps, _ = sources("gha")
    await seed(
        sessions,
        "gha",
        map_cursor=3,
        read_maps=maps,
        missing_maps=[maps[1]],
        map_failures={maps[0]: {"next_retry_at": 0, "error": "HTTPStatusError"}},
        index_error="HTTPStatusError",
    )

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get("/api/catalogs")
        item = next(item for item in result.json() if item["provider"] == "gha")
        assert item["sitemaps_read"] == 1 and item["sitemaps_total"] == 3
        assert item["sitemaps_failed"] == 1 and item["sitemaps_missing"] == 1
        assert item["sitemap_index_error"] == "HTTPStatusError"
    finally:
        app.dependency_overrides.clear()


def test_legacy_read_count_excludes_missing_maps_and_is_still_upgradable():
    data = {"maps": ["one", "two", "three"], "map_cursor": 2, "missing_maps": ["one"]}
    assert read_sitemaps(data) == {"two"}
