"""Offline projection of the public list's mounted accordion and property URLs."""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.hyatt_browser import HyattBrowserProvider
from app.providers.hyatt_catalog import (
    REGIONS,
    ROOT,
    combine_regions,
    directory_url,
    parse_region,
    property_identity,
)
from app.providers.hyatt_page import PROPERTY_URL, parse_hotel
from app.schemas.domain import JobKind
from app.services.catalog_coverage import directory_coverage
from app.services.ihg_catalogs import discover_hyatt
from app.services.watchlists import expand_global


def html(area="亚洲", url=PROPERTY_URL, name="上海中山公园凯悦嘉荟酒店", extra=""):
    buttons = "".join(
        f'<button class="dream-list-accordion__trigger" id="b{i}" aria-expanded="{str(a == area).lower()}" aria-controls="r{i}">{a}</button>'
        for i, a in enumerate(REGIONS)
    )
    i = REGIONS.index(area)
    return f'''<main>{buttons}<div id="r{i}" role="region" data-state="open" aria-labelledby="b{i}">
      <div class="dream-list-accordion__country-container"><h3 class="dream-list-accordion__country-header">大中华地区</h3>
      <div class="dream-list-accordion__province-container"><h4 class="dream-list-accordion__province-header">中国大陆</h4>
      <ul><li class="dream-list-accordion__property-list-item"><a href="{url}"><span>{name}</span><span class="property-badge">即将开业</span></a></li>{extra}</ul>
      </div></div></div></main>'''


@pytest.mark.parametrize(
    "url,code,brand",
    [
        (PROPERTY_URL, "SHACP", "caption-by-hyatt"),
        (PROPERTY_URL.replace("zh-CN", "en-US"), "SHACP", "caption-by-hyatt"),
        ("https://www.hyatt.com/hotel/zh-CN/china/grand-hyatt-kunming/kmggh", "KMGGH", None),
        ("https://www.hyatt.com/zh-CN/hotel/florida/the-standard-spa-miami-beach/miasm", "MIASM", None),
        ("https://www.hyatt.com/en-US/hotel/california/ventana-campground/sjcac", "SJCAC", None),
        ("https://www.hyatt.com/mr-and-mrs-smith/m1885-pemako-punakha", "M1885", "mr-and-mrs-smith"),
        ("https://www.hyatt.com/urcove/zh-CN/uc074-urcove-lhasa-Potala-Palace/", "UC074", "urcove"),
    ],
)
def test_actual_public_detail_formats(url, code, brand):
    assert property_identity(url) == (code, brand)
    hotel = parse_hotel("<h1>Actual heading</h1>", url)
    assert hotel.provider_hotel_id == code and hotel.brand == brand
    assert str(hotel.official_url) == url


@pytest.mark.parametrize(
    "url",
    [
        ROOT + "?brands=Hyatt",
        ROOT + "#x",
        ROOT.replace("https:", "http:"),
        ROOT.replace("www.hyatt.com", "example.com"),
        ROOT.replace("www.hyatt.com", "user:pass@www.hyatt.com"),
        ROOT.replace("www.hyatt.com", "www.hyatt.com:8443"),
    ],
)
def test_no_filtered_or_nonofficial_catalog(url):
    with pytest.raises(ProviderError):
        directory_url(url)


@pytest.mark.parametrize(
    "url",
    [
        PROPERTY_URL + "?token=a",
        PROPERTY_URL + "#rooms",
        PROPERTY_URL.replace("www.hyatt.com", "example.com"),
        PROPERTY_URL.replace("zh-CN", "de-DE"),
        ROOT,
        "https://www.hyatt.com/zh-CN/shop/rooms/shacp",
        "https://www.hyatt.com/unknown/shacp-not-a-detail",
    ],
)
def test_detail_url_identity_not_guessed(url):
    with pytest.raises(ProviderError):
        property_identity(url)


def test_region_metadata_is_explicit_and_not_a_directory_starting_price():
    hotels, missed = parse_region(html(), "亚洲")
    assert missed == 0 and len(hotels) == 1
    h = hotels[0]
    assert h.hotel_name == "上海中山公园凯悦嘉荟酒店" and h.country == "大中华地区" and h.region == "中国大陆"
    assert h.city is None and h.default_currency is None and "cash_price" not in h.model_dump()


def test_missing_country_and_province_are_null_not_inferred_from_area_or_name():
    sample = html().replace("大中华地区", "").replace("中国大陆", "")
    hotels, _ = parse_region(sample, "亚洲")
    assert hotels[0].country is None and hotels[0].region is None and hotels[0].city is None


@pytest.mark.parametrize(
    "old,new",
    [
        ("美国及加拿大", "Unknown"),
        ('aria-expanded="true"', 'aria-expanded="false"'),
        ('data-state="open"', 'data-state="closed"'),
        ('aria-labelledby="b4"', 'aria-labelledby="b0"'),
        ('class="dream-list-accordion__property-list-item"', 'class="unloaded"'),
    ],
)
def test_unmounted_wrong_or_unloaded_region_is_not_coverage(old, new):
    with pytest.raises(ProviderError):
        parse_region(html().replace(old, new), "亚洲")


def observations():
    return {area: parse_region(html(area), area) for area in REGIONS}


def test_all_regions_deduplicate_property_not_sum_links_and_no_global_total_claim():
    result = combine_regions(ROOT, observations())
    assert (
        len(result.hotels) == 1
        and result.page_complete
        and not result.complete
        and result.reported_total is None
    )
    with pytest.raises(ProviderError):
        combine_regions(ROOT, {"亚洲": parse_region(html(), "亚洲")})


def test_unrecognized_link_is_counted_and_good_hotels_preserved_without_full_coverage():
    extra = '<li class="dream-list-accordion__property-list-item"><a href="https://www.hyatt.com/new-format"><span>New Format</span></a></li>'
    obs = observations()
    obs["亚洲"] = parse_region(html(extra=extra), "亚洲")
    result = combine_regions(ROOT, obs)
    assert result.unparsed_hotel_links == 1 and len(result.hotels) == 1 and not result.page_complete


def test_duplicate_prefers_actual_chinese_link_not_rewritten_english_url():
    obs = observations()
    obs["美国及加拿大"] = parse_region(
        html("美国及加拿大", url=PROPERTY_URL.replace("zh-CN", "en-US")), "美国及加拿大"
    )
    assert str(combine_regions(ROOT, obs).hotels[0].official_url) == PROPERTY_URL


class CatalogUI:
    def __init__(self):
        self.url = ROOT + "/"
        self.area = "亚洲"
        self.clicks = []

    async def content(self):
        return html(self.area)

    async def title(self):
        return "Explore Hotels"

    def locator(self, selector):
        result = MagicMock()
        result.first.wait_for = AsyncMock()
        return result

    def get_by_role(self, role, *, name, exact):
        assert exact and name in REGIONS
        result = MagicMock()
        if role == "button":
            result.get_attribute = AsyncMock(side_effect=lambda key: "true" if self.area == name else "false")

            async def click():
                self.area = name
                self.clicks.append(name)

            result.click = AsyncMock(side_effect=click)
        elif role == "region":
            result.wait_for = AsyncMock()
            result.locator.side_effect = self.locator
        else:
            raise AssertionError("Unexpected potentially consequential control")
        return result


async def test_browser_visits_each_region_once_with_normal_buttons(monkeypatch):
    provider = HyattBrowserProvider()
    page = CatalogUI()
    navigate = AsyncMock(return_value=page)
    monkeypatch.setattr(provider, "_navigate", navigate)
    result = await provider.discover_catalog({"official_url": ROOT})
    assert page.clicks == list(REGIONS) and result.page_complete
    navigate.assert_awaited_once_with(ROOT + "/")


async def test_catalog429_never_clicks_controls(monkeypatch):
    provider = HyattBrowserProvider()
    page = MagicMock()
    page.goto = AsyncMock(return_value=MagicMock(status=429, header_value=AsyncMock(return_value="900")))
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        await provider.discover_catalog({"official_url": ROOT})
    assert caught.value.code == ErrorCode.RATE_LIMITED
    page.get_by_role.assert_not_called()


async def test_automatic_catalog_worker_to_year_jobs_without_fake_quotes(sessions, queue, monkeypatch):
    monkeypatch.setenv("HYATT_ENABLED", "true")
    settings = Settings(catalog_jobs_per_tick=1, global_jobs_per_tick=1)
    assert await discover_hyatt(sessions, queue, settings) == 1
    assert await discover_hyatt(sessions, queue, settings) == 0
    async with sessions() as db:
        job = await db.scalar(select(CrawlJob))
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=combine_regions(ROOT, observations())))
    await queue.put(job.id, job.priority)
    assert await worker.process_one(sessions, queue, settings)
    async with sessions() as db, db.begin():
        assert (await db.get(CrawlJob, job.id)).status == "SUCCEEDED"
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 1
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        state = (await db.get(AppSetting, "catalog:hyatt")).value
        info = state["pages"][ROOT]
        assert info["page_complete"] and info["hotel_ids"] == ["SHACP"]
        coverage = directory_coverage(state, ROOT, utcnow(), settings.discovery_interval_days)
        assert coverage["catalog_coverage_status"] == "TRAVERSED_TOTAL_UNVERIFIED"
        assert not coverage["catalog_coverage_complete"]
        assert await expand_global(db, settings) == 1
        price_job = await db.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))
        assert price_job.check_in == date.today() and price_job.check_out == date.today() + timedelta(days=1)
    assert await discover_hyatt(sessions, queue, settings) == 0


async def test_failure_persisted_and_provider_pause_prevents_new_catalog(sessions, queue, monkeypatch):
    monkeypatch.setenv("HYATT_ENABLED", "true")
    await discover_hyatt(sessions, queue, Settings())
    async with sessions() as db:
        job = await db.scalar(select(CrawlJob))
    monkeypatch.setattr(
        worker,
        "execute",
        AsyncMock(side_effect=ProviderError(ErrorCode.RATE_LIMITED, "429", retry_after=7200)),
    )
    await queue.put(job.id, job.priority)
    await worker.process_one(sessions, queue, Settings())
    async with sessions() as db:
        assert (await db.get(AppSetting, "catalog:hyatt")).value["pages"][ROOT]["status"] == "FAILED"
        assert (await db.get(ProviderStatus, "hyatt")).blocked_until
        assert await db.scalar(select(func.count()).select_from(Hotel)) == 0
    assert await discover_hyatt(sessions, queue, Settings()) == 0


async def test_disabled_catalog_no_request_or_job(sessions, queue, monkeypatch):
    monkeypatch.setenv("HYATT_ENABLED", "false")
    assert await discover_hyatt(sessions, queue, Settings()) == 0


def test_more_than_a_thousand_mounted_links_are_not_truncated():
    extra = "".join(
        f'<li class="dream-list-accordion__property-list-item"><a href="https://www.hyatt.com/hyatt-regency/zh-CN/t{i:04d}-offline-hotel"><span>Offline Hotel {i}</span></a></li>'
        for i in range(1105)
    )
    hotels, missed = parse_region(html(extra=extra), "亚洲")
    assert len(hotels) == 1106 and missed == 0
    assert hotels[-1].provider_hotel_id == "T1104"


async def test_catalog_api_reports_hyatt_directory_and_unparsed_count(sessions):
    import httpx

    from app.api.main import app
    from app.database.session import get_session

    async with sessions() as db, db.begin():
        db.add(
            AppSetting(
                key="catalog:hyatt",
                value={
                    "pages": {
                        ROOT: {
                            "status": "PARTIAL",
                            "page_complete": False,
                            "directory_urls": [],
                            "hotel_ids": [],
                            "last_scan_at": utcnow().timestamp(),
                            "unparsed_hotel_links": 3,
                        }
                    }
                },
            )
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
        assert response.status_code == 200
        row = next(r for r in response.json() if r["provider"] == "hyatt")
        assert row["source"] == "OFFICIAL_BROWSER_DIRECTORY" and row["unparsed_hotel_links"] == 3
        assert row["catalog_coverage_status"] == "UNVERIFIED_PAGES"
        assert not row["catalog_coverage_complete"] and row["hotels"] == row["hotels_with_quotes"] == 0
    finally:
        app.dependency_overrides.clear()
