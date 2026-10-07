from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import func, select

from app.api.main import app
from app.core.config import ProviderPolicy, Settings
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.ihg_sitemap import ROOT, STATE_KEY, property_code, sitemap_links
from app.schemas.domain import JobInput, JobKind
from app.services import catalogs, ihg_sitemaps
from app.services.ihg_catalogs import discover_ihg as discover_directory
from app.services.ihg_sitemaps import LANE_KEY, discover_ihg_global, discover_ihg_sitemap
from app.services.jobs import enqueue

MAP_ONE = "https://www.ihg.com/bin/sitemap.holidayinnresorts.en-us.hoteldetail.xml"
MAP_TWO = "https://www.ihg.com/bin/sitemap.intercontinental.en-us.hoteldetail.xml"


def hotel_url(code, brand="holidayinnresorts"):
    return f"https://www.ihg.com/{brand}/hotels/us/en/offline-city/{code.lower()}/hoteldetail"


def xml(urls):
    return ("<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>").encode()


def config(**changes):
    return Settings(_env_file=None, provider_overrides={"ihg": ProviderPolicy(enabled=True)}, **changes)


def test_robots_declared_index_only_keeps_advertised_english_hotel_detail_maps():
    assert sitemap_links(
        xml(
            [
                MAP_ONE,
                MAP_TWO,
                MAP_ONE,
                MAP_ONE.replace("en-us", "zh-cn"),
                MAP_ONE.replace("hoteldetail", "hotel-reviews"),
                MAP_ONE.replace("www.ihg.com", "example.com"),
                MAP_ONE + "?token=private",
                MAP_ONE + "#private",
            ]
        ),
        index=True,
    ) == sorted([MAP_ONE, MAP_TWO])


@pytest.mark.parametrize(
    "value",
    [
        None,
        12,
        hotel_url("ABC01") + "?token=private",
        hotel_url("ABC01") + "#private",
        hotel_url("ABC01").replace("https:", "http:"),
        hotel_url("ABC01").replace("www.ihg.com", "example.com"),
        hotel_url("ABC01").replace("www.ihg.com", "www.ihg.com:8443"),
        hotel_url("ABC01").replace("www.ihg.com", "user:private@www.ihg.com"),
        hotel_url("ABC01").replace("/en/", "/zh/"),
        hotel_url("ABCD"),
    ],
)
def test_property_identity_rejects_non_public_or_non_english_details(value):
    assert property_code(value) is None


def test_actual_map_details_deduplicate_property_codes_not_brand_or_market_aliases():
    first = "https://www.ihg.com/holidayinnresorts/hotels/us/en/krabi/kbvab/hoteldetail"
    alias = first.replace("holidayinnresorts", "holidayinn").replace("/us/", "/gb/")
    hualuxe = "https://www.ihg.com/hotels/us/en/beihai/bhybr/hoteldetail"
    result = sitemap_links(xml([first, first + "/", alias, hualuxe, first + "/rooms", first + "?ref=x"]))
    assert len(result) == 2 and {property_code(url) for url in result} == {"KBVAB", "BHYBR"}
    assert min(first, alias) in result


async def seed_source(sessions, urls, **changes):
    value = {"urls": urls, "cursor": 0, "next_fetch_at": utcnow().timestamp() + 3600, **changes}
    async with sessions() as db, db.begin():
        db.add(AppSetting(key=STATE_KEY, value=value))
    return value


async def test_fresh_disabled_and_live_aliases_skip_but_stale_and_new_details_are_queued(sessions, queue):
    now = utcnow()
    async with sessions() as db, db.begin():
        for code, days, active in [("ABC01", 0, True), ("ABC02", 20, True), ("ABC03", 20, False)]:
            db.add(
                Hotel(
                    provider="ihg",
                    provider_hotel_id=code,
                    hotel_name="Offline",
                    official_url=hotel_url(code),
                    updated_at=now - timedelta(days=days),
                    active=active,
                )
            )
        await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.DISCOVER_HOTELS,
                payload={"official_url": hotel_url("ABC04", "hotelindigo")},
            ),
        )
    original = [hotel_url(f"ABC{i:02}") for i in range(1, 6)] + [hotel_url("ABC05", "hotelindigo")]
    await seed_source(sessions, original)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No map read due"))
    ) as client:
        assert await discover_ihg_sitemap(sessions, queue, config(catalog_jobs_per_tick=5), client) == 2
        assert await discover_ihg_sitemap(sessions, queue, config(catalog_jobs_per_tick=5), client) == 0
    async with sessions() as db:
        state = (await db.get(AppSetting, STATE_KEY)).value
        assert state["urls"] == original and state["cursor"] == len(original)
        jobs = (await db.scalars(select(CrawlJob))).all()
        assert len(jobs) == 3
        new_jobs = [job for job in jobs if job.payload.get("catalog_source") == STATE_KEY]
        assert {property_code(job.payload["official_url"]) for job in new_jobs} == {"ABC02", "ABC05"}
        assert all(job.status == "PENDING" and job.priority == 70 for job in new_jobs)
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 3
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert (await db.scalar(select(Hotel).where(Hotel.provider_hotel_id == "ABC03"))).active is False


async def test_normal_map_ticks_resume_and_skip_live_codes_across_brand_maps(sessions, queue, monkeypatch):
    clock = [utcnow()]
    monkeypatch.setattr(catalogs, "utcnow", lambda: clock[0])
    calls = []

    def response(request):
        calls.append(str(request.url))
        urls = {
            ROOT: [MAP_ONE, MAP_TWO],
            MAP_ONE: [hotel_url("ABC01")],
            MAP_TWO: [hotel_url("ABC01", "hotelindigo"), hotel_url("ABC02")],
        }[str(request.url)]
        return httpx.Response(200, content=xml(urls))

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover_ihg_sitemap(sessions, queue, config(), client) == 0
        clock[0] += timedelta(seconds=31)
        assert await discover_ihg_sitemap(sessions, queue, config(), client) == 1
    # New client emulates restart: state, maps and queue identities are durable.
    clock[0] += timedelta(seconds=31)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover_ihg_sitemap(sessions, queue, config(), client) == 1
    assert calls == [ROOT, MAP_ONE, MAP_TWO]
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob))).all()
        assert {property_code(job.payload["official_url"]) for job in jobs} == {"ABC01", "ABC02"}
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 0
        assert (await db.get(AppSetting, STATE_KEY)).value["read_maps"] == sorted([MAP_ONE, MAP_TWO])


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_source_rejection_pauses_directory_and_repeated_source_requests(sessions, queue, status):
    calls = []

    def response(request):
        calls.append(str(request.url))
        return httpx.Response(status, headers={"Retry-After": "22000"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        assert await discover_ihg_sitemap(sessions, queue, config(), client) == 0
        assert await discover_ihg_sitemap(sessions, queue, config(), client) == 0
        assert await discover_directory(sessions, queue, config(), client) == 0
    assert calls == [ROOT]
    async with sessions() as db:
        state = await db.get(ProviderStatus, "ihg")
        assert state.status == "BLOCKED" and state.blocked_until is not None
        assert await db.get(AppSetting, "catalog:ihg") is None
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0


async def test_global_source_lanes_alternate_after_restart_using_one_budget(sessions, queue, monkeypatch):
    order = []

    async def sitemap(*args):
        order.append("sitemap")
        assert args[2].catalog_jobs_per_tick == 3
        return 0

    async def directory(*args):
        order.append("directory")
        assert args[2].catalog_jobs_per_tick == 3
        return 3

    monkeypatch.setattr(ihg_sitemaps, "discover_ihg_sitemap", sitemap)
    monkeypatch.setattr(ihg_sitemaps, "discover_directory", directory)
    for expected in (0, 3, 0, 3):
        async with httpx.AsyncClient() as restarted_client:
            assert (
                await discover_ihg_global(sessions, queue, config(catalog_jobs_per_tick=3), restarted_client)
                == expected
            )
    assert order == ["sitemap", "directory", "sitemap", "directory"]


async def test_non_rejection_map_failure_does_not_starve_directory_lane(sessions, queue, monkeypatch):
    directory = AsyncMock(return_value=1)
    monkeypatch.setattr(ihg_sitemaps, "discover_directory", directory)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        assert await discover_ihg_global(sessions, queue, config(), client) == 0
        assert await discover_ihg_global(sessions, queue, config(), client) == 1
    directory.assert_awaited_once()
    async with sessions() as db:
        assert await db.get(ProviderStatus, "ihg") is None
        assert (await db.get(AppSetting, STATE_KEY)).value["index_error"] == "HTTPStatusError"


@pytest.mark.parametrize("reason", ["disabled", "global_disabled", "paused", "locked"])
async def test_inactive_or_locked_global_discovery_does_not_switch_lanes_or_read_sources(
    sessions, queue, monkeypatch, reason
):
    source = AsyncMock(side_effect=AssertionError("No discovery should run"))
    monkeypatch.setattr(ihg_sitemaps, "discover_ihg_sitemap", source)
    monkeypatch.setattr(ihg_sitemaps, "discover_directory", source)
    settings = config()
    if reason == "disabled":
        settings.provider_overrides["ihg"].enabled = False
    if reason == "global_disabled":
        settings.global_monitoring_enabled = False
    if reason == "paused":
        async with sessions() as db, db.begin():
            db.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=1)))
    if reason == "locked":
        await queue.lock("hotelbug:catalog-lane:ihg", 60)
    async with httpx.AsyncClient() as client:
        assert await discover_ihg_global(sessions, queue, settings, client) == 0
    source.assert_not_awaited()
    async with sessions() as db:
        assert await db.get(AppSetting, LANE_KEY) is None


async def test_full_queue_leaves_source_cursor_at_the_unvisited_candidate(sessions, queue):
    original = await seed_source(sessions, [hotel_url("NEW01")])
    async with sessions() as db, db.begin():
        for i in range(20):
            await enqueue(
                db,
                JobInput(
                    provider="ihg",
                    kind=JobKind.DISCOVER_HOTELS,
                    payload={"official_url": hotel_url(f"A{i:04}")},
                ),
            )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No source read due"))
    ) as client:
        assert await discover_ihg_sitemap(sessions, queue, config(max_pending_jobs=20), client) == 0
    async with sessions() as db:
        state = (await db.get(AppSetting, STATE_KEY)).value
        assert state["cursor"] == original["cursor"] == 0
        assert state["urls"] == original["urls"]
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 20


async def test_api_reports_auxiliary_map_candidates_separately_from_hotels_and_dispatch(sessions):
    await seed_source(
        sessions,
        [hotel_url("ABC01"), hotel_url("ABC01", "hotelindigo")],
        cursor=2,
        maps=[MAP_ONE, MAP_TWO],
        read_maps=[MAP_ONE],
        map_failures={MAP_TWO: {"error": "TimeoutException"}},
    )

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/catalogs")
        item = next(row for row in response.json() if row["provider"] == "ihg")
        assert item["hotels"] == item["hotels_with_quotes"] == 0
        assert item["hotel_candidates"] == item["candidate_tasks_dispatched"] == 0
        assert item["auxiliary_sitemap"] == {
            "sitemaps_total": 2,
            "sitemaps_read": 1,
            "sitemaps_missing": 0,
            "sitemaps_failed": 1,
            "hotel_candidates": 1,
            "candidate_positions_processed": 2,
            "last_error": None,
        }
        assert item["catalog_coverage_complete"] is False
    finally:
        app.dependency_overrides.clear()
