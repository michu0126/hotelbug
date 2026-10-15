"""Offline projections of the Chinese public directory observed 2026-10-08."""

from urllib.parse import urlencode

import pytest

from app.core.errors import ProviderError
from app.providers.marriott_cn_catalog import (
    REGIONS,
    ROOT,
    directory_url,
    parse_country,
    parse_root,
    result_range,
    verify_directory_redirect,
)

COUNTRY = "https://www.marriott.com.cn/search/findHotels.mi?" + urlencode(
    {
        "destinationAddress.destination": "新加坡",
        "destinationAddress.country": "SG",
        "searchType": "InCity",
        "deviceType": "desktop-web",
    }
)


def test_observed_country_directory_preserves_published_country_and_label():
    assert directory_url(ROOT) == ROOT
    assert directory_url(COUNTRY) == COUNTRY
    reversed_query = COUNTRY.split("?", 1)[0] + "?" + "&".join(reversed(COUNTRY.split("?", 1)[1].split("&")))
    assert directory_url(reversed_query) == COUNTRY
    verify_directory_redirect(COUNTRY, reversed_query)
    anchor = COUNTRY.replace("deviceType=desktop-web", "filterApplied=false")
    assert directory_url(anchor) == COUNTRY
    verify_directory_redirect(anchor, COUNTRY)


def test_observed_region_view_all_link_is_not_relabelled_as_a_country():
    source = COUNTRY.replace("destinationAddress.country=SG", "destinationAddress.region=asia")
    assert directory_url(source) == source


@pytest.mark.parametrize(
    "value",
    [
        ROOT + "?destination=Singapore",
        ROOT.replace("https", "http"),
        ROOT.replace("www.marriott.com.cn", "www.marriott.com"),
        ROOT.replace("www.marriott.com.cn", "user:password@www.marriott.com.cn"),
        COUNTRY + "&destinationAddress.country=CN",
        COUNTRY + "&page=2",
        COUNTRY.replace("country=SG", "country="),
        COUNTRY.replace("searchType=InCity", "searchType=NearCity"),
        COUNTRY.replace("deviceType=desktop-web", "deviceType=mobile"),
        COUNTRY + "&filterApplied=true",
        COUNTRY + "&destinationAddress.region=asia",
        COUNTRY + "#next",
    ],
)
def test_directory_rejects_unobserved_filters_or_ambiguous_destinations(value):
    with pytest.raises(ProviderError):
        directory_url(value)


def test_redirect_cannot_turn_a_country_into_the_home_or_a_different_country():
    for final in (ROOT, COUNTRY.replace("country=SG", "country=CN")):
        with pytest.raises(ProviderError):
            verify_directory_redirect(COUNTRY, final)


def test_result_range_retains_full_reported_count_for_lazy_loaded_cards():
    assert result_range("1 - 22 / 22 结果") == (1, 22, 22)
    assert result_range("1 - 50 / 1,577 结果") == (1, 50, 1577)
    for text in ("22 酒店", "0 - 0 / 0 结果", "1 - 23 / 22 结果", "23 - 22 / 22 结果"):
        with pytest.raises(ProviderError):
            result_range(text)


def cards_html(total=2):
    # Shortened projection of the observed Singapore 22-result page.
    return (
        f'<div class="search-summary-container"><span>1 - {total} / </span><span>{total} 结果</span></div>'
        + "".join(
            f'<div data-testid="cardContainer-testid"><button class="title-container" aria-label="{name}">{name}<div>英语</div></button><a class="view-rates-button-container" href="/search/availabilityCalendar.mi?isRateCalendar=true&amp;propertyCode={code}&amp;isSearch=true&amp;currency=">查看房价</a></div>'
            for code, name in (
                ("SINNV", "Aloft by Marriott Singapore Novena"),
                ("SINAD", "新加坡 Duxton 精品酒店，傲途格精选"),
            )
        )
    )


def test_bulk_country_cards_produce_hotel_identities_not_prices():
    result = parse_country(cards_html(), COUNTRY)
    assert result.complete and result.page_complete
    assert [hotel.provider_hotel_id for hotel in result.hotels] == ["SINNV", "SINAD"]
    assert all(hotel.country == "SG" for hotel in result.hotels)
    assert result.hotels[0].hotel_name == "Aloft by Marriott Singapore Novena"
    assert "availabilityCalendar.mi" in str(result.hotels[0].official_url)


def test_unmounted_results_remain_partial_with_reported_total():
    result = parse_country(cards_html(22), COUNTRY)
    assert result.reported_total == 22 and len(result.hotels) == 2
    assert result.complete is False and result.page_complete is False


def test_malformed_or_duplicate_hotel_does_not_prove_full_coverage():
    result = parse_country(cards_html().replace("propertyCode=SINAD", "propertyCode=SINNV"), COUNTRY)
    assert not result.complete and result.unparsed_hotel_links == 1


def test_other_dates_link_keeps_hotel_in_catalog_without_asserting_availability():
    html = cards_html().replace('currency=">查看房价', 'currency=&amp;showFullPrice=false">查看其他日期')
    result = parse_country(html, COUNTRY)
    assert result.page_complete and len(result.hotels) == 2


def root_observations():
    # Minimal 11-region projection. Each recorded body is parsed only when
    # expanded; tests intentionally use one country to avoid fake hotel counts.
    return {
        region: f'<button id="{region}_heading" aria-expanded="true"><h3 class="region-card-title">亚洲</h3></button><div id="{region}_body"><a data-custom_click_track_value="Phoenix Hotel Directory |  Region Card: 亚洲: 新加坡 | internal" href="{COUNTRY}">新加坡 22</a></div>'
        for region in REGIONS
    }


def test_root_keeps_worldwide_total_separate_from_directory_links():
    result = parse_root('<span role="heading" aria-level="2">10,434 酒店</span>', root_observations())
    assert result.reported_total == 10434 and len(result.hotels) == 0
    assert result.page_complete is True and result.complete is False


def test_repeated_stale_hidden_region_data_is_rejected():
    observations = root_observations()
    observations["africa"] = observations["africa"].replace(
        'class="region-card-title">亚洲', 'class="region-card-title">非洲'
    )
    with pytest.raises(ProviderError, match="stale"):
        parse_root('<span role="heading" aria-level="2">10,434 酒店</span>', observations)


async def test_explicit_chinese_root_continues_automatic_directory_and_year_jobs(sessions, queue):
    from datetime import date, timedelta

    from sqlalchemy import select

    from app.core.config import ProviderPolicy, Settings
    from app.models.tables import AppSetting, CrawlJob, Hotel
    from app.schemas.domain import JobInput, JobKind
    from app.services.ihg_catalogs import (
        discover_marriott,
        persist_catalog,
        request_directory_root,
        selected_directory_root,
    )
    from app.services.jobs import enqueue
    from app.services.watchlists import expand_global

    settings = Settings(provider_overrides={"marriott": ProviderPolicy(enabled=True)}, global_jobs_per_tick=2)
    result = parse_root('<span role="heading" aria-level="2">10,434 酒店</span>', root_observations())
    async with sessions() as db, db.begin():
        root_job = await enqueue(
            db, JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ROOT})
        )
        await request_directory_root(db, root_job)
        await persist_catalog(db, root_job, result, settings)
        root_job.status = "SUCCEEDED"
        assert (
            selected_directory_root((await db.get(AppSetting, "catalog:marriott")).value, "marriott") == ROOT
        )
    assert await discover_marriott(sessions, queue, settings) == 1
    async with sessions() as db, db.begin():
        country_job = await db.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
        assert country_job.payload["official_url"] == COUNTRY
        await persist_catalog(db, country_job, parse_country(cards_html(), COUNTRY), settings)
        country_job.status = "SUCCEEDED"
        assert len((await db.scalars(select(Hotel))).all()) == 2
        assert await expand_global(db, settings) == 2
        dates = (await db.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))).all()
        assert len(dates) == 2
        assert len({job.hotel_id for job in dates}) == 2
        assert all(date.today() <= job.check_in < date.today() + timedelta(days=365) for job in dates)
        assert all(job.check_out == job.check_in + timedelta(days=1) for job in dates)
