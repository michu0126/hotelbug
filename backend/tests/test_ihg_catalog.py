from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.ihg_catalog import REGIONS, ROOT, directory_url, parse_catalog
from app.schemas.domain import CatalogPageData, HotelData, JobInput, JobKind
from app.services.ihg_catalogs import discover_ihg, persist_catalog
from app.services.jobs import enqueue
from app.services.watchlists import expand_global

ALABAMA = "https://www.ihg.com/alabama-united-states"
HOTEL_URL = "https://www.ihg.com/holidayinnexpress/hotels/us/en/huntsville/hsvpp/hoteldetail"


def directory_html(total=1, extra=""):
    return f'''<h1>Alabama hotels</h1><div data-render-hotel-count="true">
    <span id="cmp-card__title-bar-count">{total}</span> hotels</div>
    <h4><a href="{HOTEL_URL}">Holiday Inn Express &amp; Suites Huntsville West - Research PK</a></h4>
    <p>From HKD 888 / night</p>{extra}
    <div class="explorelist"><h2>More Cities in Alabama</h2>
    <a href="https://www.ihg.com/huntsville-alabama">Huntsville Hotels</a></div>'''


def test_worldwide_directory_requires_all_regions_and_reads_links():
    html = "".join(
        f'<div class="cmp-accordion__item"><span class="cmp-accordion__title">{name}</span>'
        f'<a href="{ALABAMA}">Alabama Hotels</a><a href="https://evil.example/a">Other Hotels</a></div>'
        for name in REGIONS
    )
    result = parse_catalog(html, ROOT)
    assert result.complete and not result.hotels
    assert [str(u) for u in result.directory_urls] == [ALABAMA]
    with pytest.raises(ProviderError):
        parse_catalog(html.replace("US & Canada", "Different heading"), ROOT)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.ihg.com/explore",
        "https://example.com/explore",
        ROOT + "?token=a",
        ROOT + "#x",
        "https://www.ihg.com:8443/explore",
    ],
)
def test_directory_url_validation(url):
    with pytest.raises(ProviderError):
        directory_url(url)


def test_real_catalog_hotel_identity_without_directory_starting_price():
    result = parse_catalog(directory_html(), ALABAMA)
    assert result.complete and result.reported_total == 1
    assert result.hotels[0].provider_hotel_id == "HSVPP"
    assert result.hotels[0].hotel_name == "Holiday Inn Express & Suites Huntsville West - Research PK"
    assert [str(u) for u in result.directory_urls] == ["https://www.ihg.com/huntsville-alabama"]
    assert "cash_price" not in result.hotels[0].model_dump()


def test_featured_subset_is_explicitly_partial_but_preserves_discoverable_hotels():
    result = parse_catalog(directory_html(total=5), ALABAMA)
    assert not result.complete and len(result.hotels) == 1
    unsupported = '<h4><a href="https://www.ihg.com/other-format">New Hotel</a></h4>'
    partial = parse_catalog(directory_html(total=2, extra=unsupported), ALABAMA)
    assert not partial.complete and partial.unparsed_hotel_links == 1


@pytest.mark.parametrize(
    "bad_link",
    [
        '<h4><a href="https://example.com/hotel">External Hotel</a></h4>',
        '<h4><a href="https://www.ihg.com/other-format">Unsupported Hotel</a></h4>',
        f'<h4><a href="{HOTEL_URL}?token=private">Different query</a></h4>',
        f'<h4><a href="{HOTEL_URL}"> </a></h4>',
    ],
)
def test_unparsed_directory_links_are_counted_not_silently_accepted(bad_link):
    result = parse_catalog(directory_html(total=2, extra=bad_link), ALABAMA)
    assert result.unparsed_hotel_links == 1 and not result.complete
    assert len(result.hotels) == 1


def test_small_city_without_count_still_produces_real_hotel_not_timeout():
    html = directory_html().replace(
        '<div data-render-hotel-count="true">\n    <span id="cmp-card__title-bar-count">1</span> hotels</div>',
        "<h2>Featured Hotels in Alabama</h2>",
    )
    result = parse_catalog(html, ALABAMA)
    assert result.reported_total is None and not result.complete and len(result.hotels) == 1


def test_public_hotel_schema_enriches_only_matching_visible_hotel():
    import json

    metadata = {
        "@type": "Hotel",
        "url": HOTEL_URL,
        "name": "Holiday Inn Express & Suites Huntsville West - Research PK",
        "address": {
            "addressCountry": "United States",
            "addressRegion": "Alabama",
            "addressLocality": "Huntsville",
            "streetAddress": "123 Test St",
        },
    }
    html = directory_html(extra='<script type="application/ld+json">' + json.dumps(metadata) + "</script>")
    hotel = parse_catalog(html, ALABAMA).hotels[0]
    assert (hotel.country, hotel.region, hotel.city, hotel.address) == (
        "United States",
        "Alabama",
        "Huntsville",
        "123 Test St",
    )
    metadata["name"] = "Different Hotel"
    hotel = parse_catalog(
        directory_html(extra='<script type="application/ld+json">' + json.dumps(metadata) + "</script>"),
        ALABAMA,
    ).hotels[0]
    assert hotel.country is None and hotel.address is None


async def test_browser_catalog_waits_for_hotel_links_when_counter_absent(monkeypatch):
    from unittest.mock import MagicMock

    from app.providers.ihg_browser import IHGBrowserProvider

    provider = IHGBrowserProvider()
    page = MagicMock()
    page.url = ALABAMA
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="Alabama Hotels")
    page.wait_for_function = AsyncMock()
    page.content = AsyncMock(return_value=directory_html())
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    result = await provider.discover_catalog({"official_url": ALABAMA})
    assert len(result.hotels) == 1 and result.complete
    page.wait_for_function.assert_awaited_once()
    assert "h4 a[href]" in page.wait_for_function.await_args.args[0]
    assert page.goto.await_args.kwargs["wait_until"] == "commit"


@pytest.mark.parametrize(
    "state", ["late_counter", "late_cards", "empty_counter", "no_counter", "redirect", "access_denied"]
)
async def test_directory_counter_hydration_does_not_fake_completeness(monkeypatch, state):
    from unittest.mock import MagicMock

    from playwright.async_api import TimeoutError as BrowserTimeout

    from app.providers.ihg_browser import IHGBrowserProvider

    provider = IHGBrowserProvider()
    page = MagicMock()
    page.url = ALABAMA
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="Alabama Hotels")
    initial = "<h2>Featured Hotels in Alabama</h2>" + directory_html(total="")
    final = directory_html(total=1)
    if state == "late_cards":
        initial = directory_html(total=2)
        second = '<h4><a href="https://www.ihg.com/holidayinn/hotels/us/en/offline/test1/hoteldetail">Second Hotel</a></h4>'
        final = directory_html(total=2, extra=second)
    if state == "no_counter":
        initial = "<h2>Featured Hotels in Alabama</h2>" + initial.replace(
            'data-render-hotel-count="true"', 'data-no-counter="true"'
        )
    if state == "empty_counter":
        final = initial
    page.content = AsyncMock(side_effect=[initial, final])

    async def wait(*args, **kwargs):
        if kwargs.get("timeout") == 5000:
            if state == "empty_counter":
                raise BrowserTimeout("Offline permanently empty counter")
            if state == "redirect":
                page.url = "https://www.ihg.com/another-directory"
            if state == "access_denied":
                page.title.return_value = "Access Denied"

    page.wait_for_function = AsyncMock(side_effect=wait)
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    if state in ("redirect", "access_denied"):
        with pytest.raises(ProviderError) as raised:
            await provider.discover_catalog({"official_url": ALABAMA})
        assert raised.value.code == (
            ErrorCode.INVALID_RESPONSE if state == "redirect" else ErrorCode.BLOCKED_BY_ANTIBOT
        )
    else:
        result = await provider.discover_catalog({"official_url": ALABAMA})
        assert result.complete is (state in ("late_counter", "late_cards"))
        if state == "no_counter":
            assert result.reported_total is None and page.wait_for_function.await_count == 1
        else:
            assert page.wait_for_function.await_count == 2
            assert page.wait_for_function.await_args.kwargs["timeout"] == 5000
        if state == "late_cards":
            assert len(result.hotels) == result.reported_total == 2


async def test_scheduler_root_resume_and_worker_hotels_reach_global_price_jobs(sessions, queue, monkeypatch):
    monkeypatch.setenv("IHG_ENABLED", "true")
    settings = Settings(catalog_jobs_per_tick=1, global_jobs_per_tick=1)
    assert await discover_ihg(sessions, queue, settings) == 1
    assert await discover_ihg(sessions, queue, settings) == 0
    async with sessions() as session:
        root_job = await session.scalar(select(CrawlJob))
    monkeypatch.setattr(
        worker,
        "execute",
        AsyncMock(
            return_value=CatalogPageData(
                provider="ihg",
                source_url=ROOT,
                directory_urls=[ALABAMA],
                complete=True,
            )
        ),
    )
    await queue.put(root_job.id, root_job.priority)
    assert await worker.process_one(sessions, queue, settings)
    assert await discover_ihg(sessions, queue, settings) == 1
    async with sessions() as session:
        leaf = await session.scalar(select(CrawlJob).where(CrawlJob.id != root_job.id))
        assert leaf.payload["official_url"] == ALABAMA
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=parse_catalog(directory_html(), ALABAMA)))
    # Finish through the actual lease/rate-limit/transaction Worker path.
    await queue.redis.delete("hotelbug:rate:ihg")
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    await queue.put(leaf.id, leaf.priority)
    assert await worker.process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Hotel)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
        state = (await session.get(AppSetting, "catalog:ihg")).value
        assert state["pages"][ALABAMA]["status"] == "SUCCEEDED"
        assert "https://www.ihg.com/huntsville-alabama" in state["pages"]
        assert await expand_global(session, settings) == 1
        rate_job = await session.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))
        assert rate_job.hotel_id and rate_job.check_in and rate_job.check_out


async def test_catalog_failure_retains_progress_and_failed_page_becomes_retryable(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("IHG_ENABLED", "true")
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    settings = Settings()
    await discover_ihg(sessions, queue, settings)
    async with sessions() as session:
        job = await session.scalar(select(CrawlJob))
    monkeypatch.setattr(
        worker,
        "execute",
        AsyncMock(side_effect=ProviderError(ErrorCode.PROVIDER_CHANGED, "Directory changed")),
    )
    await queue.put(job.id, job.priority)
    await worker.process_one(sessions, queue, settings)
    assert await discover_ihg(sessions, queue, settings) == 0
    async with sessions() as session, session.begin():
        state = await session.get(AppSetting, "catalog:ihg")
        info = state.value["pages"][ROOT]
        assert info["last_error"] == "PROVIDER_CHANGED"
        state.value = {**state.value, "pages": {ROOT: {**info, "next_scan_at": 0}}}
    assert await discover_ihg(sessions, queue, settings) == 1


async def test_partial_pages_and_queue_cap_do_not_fake_full_coverage(sessions, queue, monkeypatch):
    monkeypatch.setenv("IHG_ENABLED", "true")
    async with sessions() as session, session.begin():
        job = await enqueue(
            session,
            JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ALABAMA}),
        )
        await persist_catalog(session, job, parse_catalog(directory_html(total=5), ALABAMA), Settings())
        state = (await session.get(AppSetting, "catalog:ihg")).value
        assert state["pages"][ALABAMA]["status"] == "PARTIAL"
        assert state["last_error"] == "DIRECTORY_PARTIAL"
        for i in range(19):
            await enqueue(
                session, JobInput(provider="ihg", kind=JobKind.DISCOVER_HOTELS, payload={"test": i})
            )
    assert await discover_ihg(sessions, queue, Settings(max_pending_jobs=20)) == 0


async def test_disabled_or_blocked_provider_does_not_dispatch(sessions, queue, monkeypatch):
    monkeypatch.setenv("IHG_ENABLED", "false")
    assert await discover_ihg(sessions, queue, Settings()) == 0
    monkeypatch.setenv("IHG_ENABLED", "true")
    async with sessions() as session, session.begin():
        session.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=1)))
    assert await discover_ihg(sessions, queue, Settings()) == 0


def test_catalog_rejects_mismatched_provider_hotels():
    hotel = HotelData(
        provider="accor",
        provider_hotel_id="0338",
        hotel_name="Test",
        official_url="https://all.accor.com/hotel/0338/index.en.shtml",
    )
    with pytest.raises(ValueError):
        CatalogPageData(provider="ihg", source_url=ALABAMA, hotels=[hotel], complete=True)
