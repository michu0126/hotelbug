"""Offline public-DOM projections; these tests do not contact Marriott."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_catalog import BUCKETS, ORIGIN, ROOT, directory_url, parse_catalog
from app.schemas.domain import JobInput, JobKind
from app.services.ihg_catalogs import discover_ihg, discover_marriott, persist_catalog
from app.services.jobs import enqueue
from app.services.watchlists import expand_global

CORUNA = ORIGIN + "/en/destinations/a-coruna.mi"
ABU_DHABI = ORIGIN + "/en/destinations/abu-dhabi.mi"
ABU_CANONICAL = ORIGIN + "/en-us/destinations/united-arab-emirates/abu-dhabi.mi"
PAGE_TWO = ABU_CANONICAL + "?pg=2"


def root_html():
    return (
        "<h1>Property Directory</h1><h2>10,423 Properties</h2>"
        + "".join(f'<button aria-label="{name}">{name}</button>' for name in BUCKETS)
        + f'<a class="destination-item-content-link" href="{CORUNA}">A Coruña, Spain</a><a class="destination-item-content-link" href="{ABU_DHABI}">Abu Dhabi, United Arab Emirates</a>'
    )


def coruna_html():
    return """<h1>Hotels in A Coruña, Spain</h1><p>Showing 1-2 of 2 Hotels</p>
    <h2 class="property-card__title"><a href="/en-us/hotels/travel/lcgco-ac-hotel-a-coruna">AC Hotel A Coruna</a></h2>
    <h2 class="property-card__title"><a href="/en-us/hotels/travel/scqak-hotel-palacio-del-carmen-autograph-collection">Hotel Palacio del Carmen, Autograph Collection</a></h2>
    <p>Lowest Regular Rates for Sat, Oct 31 - Sun, Nov 1</p><a href="/reservation/availabilitySearch.mi?propertyCode=LCGCO&amp;fromDate=10/01/2026">68 EUR / night</a>
    <h2>Related hotels</h2><a href="https://other.example/hotel">Not a property card</a>"""


def abu_last_page_html():
    return """<h1>Hotels in Abu Dhabi, United Arab Emirates</h1><p>Showing 13-14 of 14 Hotels</p>
    <h2 class="property-card__title"><a href="/en-us/hotels/travel/auhwh-w-abu-dhabi-yas-island">W Abu Dhabi - Yas Island</a></h2>
    <h2 class="property-card__title"><a href="/en-us/hotels/travel/auhlc-al-wathba-a-luxury-collection-desert-resort-and-spa-abu-dhabi">Al Wathba, a Luxury Collection Desert Resort &amp; Spa, Abu Dhabi</a></h2>
    <li data-testid="pagination-next" class="pagination-arrow disabled" aria-label="NextPage"><span>Next</span></li>"""


def first_page_html():
    # Shortened pagination-layout projection: count 4 is synthetic, not a new
    # claim about Abu Dhabi inventory. The href/control is the observed one.
    return (
        coruna_html()
        .replace("A Coruña, Spain", "Abu Dhabi, United Arab Emirates")
        .replace("1-2 of 2", "1-2 of 4")
        + f'<li data-testid="pagination-next" class="pagination-arrow"><a href="{PAGE_TWO}">Next</a></li>'
    )


def test_root_enumerates_destinations_without_claiming_worldwide_hotels_imported():
    result = parse_catalog(root_html(), ROOT, visited_buckets=BUCKETS)
    assert result.reported_total == 10423 and not result.complete and result.page_complete
    assert set(map(str, result.directory_urls)) == {CORUNA, ABU_DHABI}
    assert not result.hotels
    with pytest.raises(ProviderError):
        parse_catalog(root_html(), ROOT, visited_buckets=BUCKETS[:-1])


def test_directory_cards_save_real_names_codes_not_inconsistent_starting_prices():
    result = parse_catalog(coruna_html(), CORUNA)
    assert result.complete and result.page_complete and result.reported_total == 2
    assert [h.provider_hotel_id for h in result.hotels] == ["LCGCO", "SCQAK"]
    assert result.hotels[0].hotel_name == "AC Hotel A Coruna"
    assert all("cash_price" not in h.model_dump() for h in result.hotels)
    assert all(h.city is None for h in result.hotels)  # nearby hotel is not necessarily in this city


def test_only_advertised_next_page_is_discovered_and_last_page_retains_two_hotels():
    result = parse_catalog(first_page_html(), ABU_DHABI, final_url=ABU_CANONICAL)
    assert not result.complete and result.page_complete
    assert list(map(str, result.directory_urls)) == [PAGE_TWO]
    last = parse_catalog(abu_last_page_html(), PAGE_TWO)
    assert not last.complete and last.page_complete and last.reported_total == 14
    assert [h.provider_hotel_id for h in last.hotels] == ["AUHWH", "AUHLC"]
    assert not last.directory_urls


@pytest.mark.parametrize(
    "value",
    [
        "http://www.marriott.com/hotel-search.mi",
        "https://other.example/hotel-search.mi",
        ROOT + "?token=private",
        ROOT + "#arbitrary-filter",
        CORUNA + "?pg=0",
        CORUNA + "?pg=2&pg=3",
        CORUNA + "?pg=2&rateCode=member",
        "https://name:password@www.marriott.com/hotel-search.mi",
        "https://www.marriott.com:8080/hotel-search.mi",
    ],
)
def test_directory_url_rejects_unknown_origins_filters_and_invalid_pages(value):
    with pytest.raises(ProviderError):
        directory_url(value)


def test_published_alias_redirects_and_root_route_fragments_are_normalized():
    assert directory_url(ROOT + "#/0/") == ROOT
    assert directory_url(ROOT + "#/1/") == ROOT
    assert directory_url(CORUNA + "?pg=1") == CORUNA
    assert parse_catalog(first_page_html(), ABU_DHABI, final_url=ABU_CANONICAL).hotels
    with pytest.raises(ProviderError):
        parse_catalog(first_page_html(), ABU_DHABI, final_url=CORUNA)


@pytest.mark.parametrize(
    "old,new",
    [
        ("Showing 1-2 of 2 Hotels", "Showing 1-3 of 3 Hotels"),
        ("lcgco-ac-hotel-a-coruna", "scqak-ac-hotel-a-coruna"),
        ("/en-us/hotels/travel/lcgco", "https://other.example/en-us/hotels/travel/lcgco"),
        ("Hotels in A Coruña, Spain", "Search failed"),
    ],
)
def test_incomplete_or_wrong_identity_directory_does_not_import(old, new):
    with pytest.raises(ProviderError):
        parse_catalog(coruna_html().replace(old, new), CORUNA)


@pytest.mark.parametrize(
    "href",
    [
        PAGE_TWO.replace("pg=2", "pg=3"),
        PAGE_TWO.replace("abu-dhabi", "other-place"),
        "https://other.example/page",
    ],
)
def test_pagination_does_not_guess_next_page_or_switch_destination(href):
    with pytest.raises(ProviderError):
        parse_catalog(first_page_html().replace(PAGE_TWO, href), ABU_DHABI, final_url=ABU_CANONICAL)


async def test_browser_walks_all_nine_root_buckets_and_uses_only_public_page(monkeypatch):
    provider = MarriottBrowserProvider()
    page = MagicMock()
    page.url = ROOT + "#/0/"
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.wait_for_function = AsyncMock()
    page.content = AsyncMock(return_value=root_html())
    page.get_by_role.return_value.wait_for = AsyncMock()
    page.get_by_role.return_value.get_attribute = AsyncMock(return_value="false")
    page.get_by_role.return_value.click = AsyncMock()
    page.get_by_role.return_value.evaluate = AsyncMock(return_value=[CORUNA, ABU_DHABI])
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr(provider, "_check_access", AsyncMock())
    result = await provider.discover_catalog({"official_url": ROOT})
    assert provider._headless is False
    assert page.goto.await_args.args == (ROOT,) and page.goto.await_args.kwargs["wait_until"] == "commit"
    assert page.get_by_role.return_value.click.await_count == len(BUCKETS)
    assert [call.kwargs["arg"] for call in page.wait_for_function.await_args_list] == list(BUCKETS)
    assert set(map(str, result.directory_urls)) == {CORUNA, ABU_DHABI}
    assert not result.hotels


async def test_scheduler_worker_directory_hotels_enter_year_schedule_without_quotes(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    settings = Settings(catalog_jobs_per_tick=1, global_jobs_per_tick=2)
    assert await discover_marriott(sessions, queue, settings) == 1
    assert await discover_marriott(sessions, queue, settings) == 0
    async with sessions() as session:
        root_job = await session.scalar(select(CrawlJob))
    monkeypatch.setattr(
        worker, "execute", AsyncMock(return_value=parse_catalog(root_html(), ROOT, visited_buckets=BUCKETS))
    )
    await queue.put(root_job.id, root_job.priority)
    await worker.process_one(sessions, queue, settings)
    assert await discover_marriott(sessions, queue, settings) == 1
    async with sessions() as session:
        leaf = await session.scalar(select(CrawlJob).where(CrawlJob.id != root_job.id))
        assert leaf.payload["official_url"] == CORUNA
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=parse_catalog(coruna_html(), CORUNA)))
    await queue.put(leaf.id, leaf.priority)
    await worker.process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        assert await session.scalar(select(func.count()).select_from(Hotel)) == 2
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
        state = (await session.get(AppSetting, "catalog:marriott")).value
        assert state["pages"][ROOT]["page_complete"] and not state["pages"][ROOT]["coverage_complete"]
        assert state["pages"][ROOT]["next_scan_at"] > (utcnow() + timedelta(days=1)).timestamp()
        assert state["pages"][CORUNA]["status"] == "SUCCEEDED"
        assert await expand_global(session, settings) == 2
        rates = (await session.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))).all()
        assert len(rates) == 2 and all(j.check_out and j.hotel_id for j in rates)


async def test_advertised_continuations_persist_as_independent_tasks(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as session, session.begin():
        job = await enqueue(
            session,
            JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ABU_DHABI}),
        )
        await persist_catalog(
            session, job, parse_catalog(first_page_html(), ABU_DHABI, final_url=ABU_CANONICAL), Settings()
        )
        job.status = "SUCCEEDED"
        # Root isn't part of this isolated leaf test's dispatch budget.
        state = await session.get(AppSetting, "catalog:marriott")
        state.value = {**state.value, "pages": {u: p for u, p in state.value["pages"].items() if u != ROOT}}
    assert await discover_marriott(sessions, queue, Settings(catalog_jobs_per_tick=2)) == 2
    async with sessions() as session:
        jobs = (await session.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert {j.payload["official_url"] for j in jobs} == {ROOT, PAGE_TWO}


async def test_failed_marriott_catalog_is_not_online_and_does_not_block_ihg(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    monkeypatch.setenv("IHG_ENABLED", "true")
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    settings = Settings()
    await discover_marriott(sessions, queue, settings)
    async with sessions() as session:
        job = await session.scalar(select(CrawlJob))
    monkeypatch.setattr(
        worker, "execute", AsyncMock(side_effect=ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Access Denied"))
    )
    await queue.put(job.id, job.priority)
    await worker.process_one(sessions, queue, settings)
    async with sessions() as session:
        assert (await session.get(ProviderStatus, "marriott")).status == "BLOCKED"
        state = (await session.get(AppSetting, "catalog:marriott")).value
        assert state["pages"][ROOT]["last_error"] == "BLOCKED_BY_ANTIBOT"
        assert await session.scalar(select(func.count()).select_from(Hotel)) == 0
    assert await discover_marriott(sessions, queue, settings) == 0
    assert await discover_ihg(sessions, queue, settings) == 1


async def test_more_than_hundred_destinations_are_retained_with_bounded_dispatch(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    # Generated URLs only exercise queue rotation; not an official inventory.
    urls = [ORIGIN + f"/en/destinations/offline-only-{i}.mi" for i in range(105)]
    async with sessions() as session, session.begin():
        job = await enqueue(
            session,
            JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ROOT}),
        )
        result = parse_catalog(root_html(), ROOT, root_links=urls, visited_buckets=BUCKETS)
        await persist_catalog(session, job, result, Settings())
        job.status = "SUCCEEDED"
    for _ in range(6):
        await discover_marriott(sessions, queue, Settings(catalog_jobs_per_tick=20))
    async with sessions() as session:
        jobs = (await session.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 105 and {j.payload["official_url"] for j in jobs} == set(urls)
