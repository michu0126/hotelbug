"""Synthetic boundary fixtures for the observed hotel-search redirect/card DOM.

These fixtures are not live DENLT quotes or claims of official inventory.
"""

from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from audit_ihg_price import requeue_fixed_room_job
from sqlalchemy import func, select
from test_catalog_audit_price import recorded_room_failure
from test_ihg_browser import provider_with_page
from test_ihg_page import HOTEL, SEARCH, URL

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, NotificationLog, PriceHistory, StayScan
from app.providers.ihg_page import NO_ROOMS, hotel_search_unavailable
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel

SEARCH_URL = URL.replace("/select-roomrate", "/hotel-search").replace("qSlH=LONLS&", "")
DATES = ["10/20/2026", "10/21/2026"]
GUESTS = "1 Room, 2 Guests"


def card_html(code="LONLS", name=HOTEL, message=NO_ROOMS):
    return (
        f'<app-hotel-card-list-view data-testid="hotel-card" id="{code}">'
        f'<h2 data-testid="brandHotelNameSID">{name}</h2>'
        f'<a href="https://www.ihg.com/hotelindigo/hotels/us/en/london/{code.lower()}/hoteldetail">Hotel website</a>'
        f"<p>{message}</p></app-hotel-card-list-view>"
    )


def unavailable(html=None, url=SEARCH_URL, dates=None, guests=GUESTS):
    return hotel_search_unavailable(html or card_html(), url, SEARCH, HOTEL, dates or DATES, guests)


def test_only_target_card_can_prove_no_rooms_near_other_hotels():
    assert unavailable(card_html("OTHER", "Other hotel", "From 120 HKD per night") + card_html())
    assert not unavailable(card_html(message="From 120 HKD per night") + card_html("OTHER", "Other hotel"))
    assert not unavailable(card_html(message="Loading") + f"<div>{NO_ROOMS}</div>")
    assert not unavailable(url=URL)


@pytest.mark.parametrize(
    "html",
    [
        card_html() + card_html(),
        card_html("OTHER", "Other hotel"),
        card_html(name="Different hotel"),
        card_html().replace("/lonls/hoteldetail", "/other/hoteldetail"),
        card_html().replace("https://www.ihg.com", "https://example.com"),
        card_html().replace('/hoteldetail"', '/hoteldetail?qSlH=OTHER"'),
        card_html().replace(
            "<p>",
            '<a href="https://www.ihg.com/hotelindigo/hotels/us/en/london/other/hoteldetail">Other</a><p>',
        ),
    ],
)
def test_ambiguous_card_identity_never_records_no_rooms(html):
    with pytest.raises(ProviderError) as caught:
        unavailable(html)
    assert caught.value.code == ErrorCode.INVALID_RESPONSE


def test_public_website_tracking_link_is_identity_only_not_a_price_query():
    assert unavailable(card_html().replace('/hoteldetail"', '/hoteldetail?cm_mmc=public_tracking#overview"'))


@pytest.mark.parametrize(
    "url",
    [
        SEARCH_URL.replace("https:", "http:"),
        SEARCH_URL.replace("www.ihg.com", "www.ihg.com:443"),
        SEARCH_URL.replace("www.ihg.com", "user@www.ihg.com"),
        SEARCH_URL + "#different",
        SEARCH_URL.replace("qCiMy=092026", "qCiMy=092027"),
        SEARCH_URL.replace("qAdlt=2", "qAdlt=1"),
        SEARCH_URL + "&qSlH=OTHER",
        SEARCH_URL + "&qCiD=20",
        SEARCH_URL.replace("qRtP=6CBARC", "qRtP=MEMBER"),
    ],
)
def test_wrong_search_identity_or_conditions_never_record_no_rooms(url):
    with pytest.raises(ProviderError):
        unavailable(url=url)


@pytest.mark.parametrize(
    "dates,guests",
    [
        (["10/20/2027", "10/21/2027"], GUESTS),
        (["10/20/2026", "10/22/2026"], GUESTS),
        (DATES, "1 Room, 1 Guest"),
        (DATES, "2 Rooms, 2 Guests"),
    ],
)
def test_displayed_conditions_must_match(dates, guests):
    with pytest.raises(ProviderError):
        unavailable(dates=dates, guests=guests)


def redirected_provider(monkeypatch, visible=True):
    provider, page, metadata = provider_with_page(monkeypatch)
    page.url = SEARCH_URL
    page.content = AsyncMock(return_value=card_html())
    original_locator = page.locator
    card = MagicMock()
    card.count = AsyncMock(return_value=1)
    card.is_visible = AsyncMock(return_value=True)
    card.get_by_text.return_value.is_visible = AsyncMock(return_value=visible)
    page.locator = lambda selector: (
        card if selector.startswith("app-hotel-card-list-view") else original_locator(selector)
    )
    return provider, page, metadata


@pytest.mark.parametrize("visible", [True, False])
async def test_flow_reads_target_explicit_visible_message_not_list_starting_prices(monkeypatch, visible):
    provider, page, _ = redirected_provider(monkeypatch, visible)
    if visible:
        assert await provider.search_rates(SEARCH) == []
    else:
        with pytest.raises(ProviderError) as caught:
            await provider.search_rates(SEARCH)
        assert caught.value.code == ErrorCode.PROVIDER_CHANGED
    assert not page.expanded and not page.public


async def test_worker_records_real_empty_result_without_price_or_notification(sessions, queue, monkeypatch):
    provider, _, metadata = redirected_provider(monkeypatch)
    monkeypatch.setitem(FACTORIES, "ihg", lambda: provider)
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, metadata)
        job = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=SEARCH.check_in,
                check_out=SEARCH.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    await process_one(sessions, queue, Settings(provider_overrides={"ihg": ProviderPolicy(enabled=True)}))
    async with sessions() as db:
        current = await db.get(CrawlJob, job_id)
        assert current.status == "SUCCEEDED" and current.error_type is None and current.retry_count == 0
        scan = await db.scalar(select(StayScan).where(StayScan.job_id == job_id))
        assert scan.status == "UNAVAILABLE"
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_fix_retest_preserves_original_failure_and_future_deadline(sessions):
    original_id, deadline = await recorded_room_failure(sessions, future=True)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, original_id)
        original.error_message = "IHG booking page failed at room results"
    child_id = await requeue_fixed_room_job(sessions, "ihg", original_id, no_room=True)
    assert child_id != original_id
    assert await requeue_fixed_room_job(sessions, "ihg", original_id, no_room=True) == child_id
    async with sessions() as db, db.begin():
        original, child = await db.get(CrawlJob, original_id), await db.get(CrawlJob, child_id)
        assert original.status == "FAILED" and original.error_type == "TIMEOUT"
        assert child.scheduled_at == deadline.replace(tzinfo=None)
        child.status = "FAILED"
    with pytest.raises(AssertionError, match="already finished"):
        await requeue_fixed_room_job(sessions, "ihg", original_id, no_room=True)


@pytest.mark.parametrize("error", ["RATE_LIMITED", "BLOCKED_BY_ANTIBOT"])
async def test_no_room_fix_is_not_an_access_rejection_retry(sessions, error):
    original_id, _ = await recorded_room_failure(sessions)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, original_id)
        original.error_message = "IHG booking page failed at room results"
        original.error_type = error
    with pytest.raises(AssertionError):
        await requeue_fixed_room_job(sessions, "ihg", original_id, no_room=True)


async def test_recorded_no_room_stay_is_not_recreated_after_it_expires(sessions):
    original_id, _ = await recorded_room_failure(sessions)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, original_id)
        original.error_type = "INVALID_RESPONSE"
        original.error_message = "IHG unavailable hotel official link mismatch"
        original.check_in = date.today() - timedelta(days=2)
        original.check_out = date.today() - timedelta(days=1)
    with pytest.raises(AssertionError, match="outside current"):
        await requeue_fixed_room_job(sessions, "ihg", original_id, link_identity=True)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1
        assert (await db.get(CrawlJob, original_id)).status == "FAILED"
