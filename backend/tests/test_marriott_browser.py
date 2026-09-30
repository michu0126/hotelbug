import base64
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import (
    MarriottBrowserProvider,
    day_label,
    guest_count,
    parse_hotel_page,
    parse_room_page,
)
from app.providers.registry import create_provider
from app.schemas.domain import RateRequest


def room_html(check_in: str = "2026-10-07") -> str:
    identity = f"NYCMQ|REGB|DBDB|{check_in}|2026-10-08|sample"
    product = base64.urlsafe_b64encode(identity.encode()).decode().rstrip("=")
    return f"""
      <div class="drop-shadow">
        <h3>Deluxe, Guest room, 2 Double</h3>
        <a href="/reservation/ersViewRoomPool.mi?marshaCode=NYCMQ&amp;roomPoolCode=dbdb&amp;productId={product}">Room Details</a>
        <div class="rate-card-content">
          <h4 class="title-name">Flexible Rate</h4>
          <div class="taxes-fees-label">Taxes and all fees included</div>
          <div data-testid="ratedetails">
            <span class="rate-name">Member Rate</span>
            <a href="/reservation/ersViewRateRules.mi?rateProgramCode=RMOK">Details</a>
            <div class="price"><span aria-hidden="false">751</span><span class="avg-per-night">USD / Night</span></div>
          </div>
          <div data-testid="ratedetails">
            <span class="rate-name">Non-Member Rate</span>
            <a href="/reservation/ersViewRateRules.mi?rateProgramCode=REGB">Details</a>
            <div class="price"><span aria-hidden="false">765</span><span class="avg-per-night">USD / Night</span></div>
          </div>
        </div>
      </div>
    """


def test_public_page_hotel_and_nonmember_total():
    hotel = parse_hotel_page(
        '<a href="/hotels/travel/nycmq-new-york-marriott-marquis"><h3 class="hotel-name">New York Marriott Marquis</h3></a>',
        "NYCMQ",
    )
    assert hotel.hotel_name == "New York Marriott Marquis"
    assert str(hotel.official_url).endswith("nycmq-new-york-marriott-marquis")
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    rates = parse_room_page(room_html(), request)
    assert len(rates) == 1
    assert (rates[0].total_price, rates[0].cash_price, rates[0].currency) == (Decimal("765"), None, "USD")
    assert (rates[0].room_code, rates[0].rate_code, rates[0].member_rate) == ("dbdb", "REGB", False)


def test_room_date_mismatch_is_not_saved():
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    with pytest.raises(ProviderError) as exc:
        parse_room_page(room_html("2026-10-06"), request)
    assert exc.value.code == ErrorCode.INVALID_RESPONSE


def test_eligibility_rate_is_not_treated_as_public_nonmember_price():
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    assert parse_room_page(room_html().replace("Non-Member Rate", "AAA Member Rate"), request) == []


def test_english_calendar_label():
    assert day_label(date(2026, 10, 7)) == "Wed Oct 07 2026"


def test_guest_count_from_rendered_summary():
    assert guest_count("ROOMS & GUESTS\n1 Room, 2 Guests") == 2
    assert guest_count("1 Room, 1 Guest") == 1
    with pytest.raises(ProviderError):
        guest_count("Guests unavailable")


def test_worker_factory_uses_browser_and_passes_hotel_name():
    provider = create_provider("marriott", "New York Marriott Marquis")
    assert isinstance(provider, MarriottBrowserProvider)
    assert provider._hotel_name == "New York Marriott Marquis"


@pytest.mark.asyncio
async def test_homepage_403_stops_before_search(monkeypatch):
    provider = MarriottBrowserProvider(hotel_name="New York Marriott Marquis")
    page = MagicMock()
    page.goto = AsyncMock(return_value=SimpleNamespace(status=403))
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))

    with pytest.raises(ProviderError) as exc:
        await provider._open_hotel("NYCMQ")

    assert exc.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    page.goto.assert_awaited_once_with(
        "https://www.marriott.com/default.mi", wait_until="domcontentloaded", timeout=30000
    )
    page.get_by_role.assert_not_called()


@pytest.mark.asyncio
async def test_search_uses_public_homepage_and_view_rates(monkeypatch):
    name = "New York Marriott Marquis"
    provider = MarriottBrowserProvider(hotel_name=name)
    locator = MagicMock()
    locator.first = locator
    locator.filter.return_value = locator
    locator.get_by_role.return_value = locator
    locator.fill = AsyncMock()
    locator.click = AsyncMock()
    locator.wait_for = AsyncMock()
    locator.inner_text = AsyncMock(return_value="Marriott booking page")
    page = MagicMock()
    page.goto = AsyncMock(return_value=SimpleNamespace(status=200))
    page.get_by_role.return_value = locator
    page.locator.return_value = locator
    page.content = AsyncMock(
        return_value='<a href="/hotels/travel/nycmq-new-york-marriott-marquis">'
        '<h3 class="hotel-name">New York Marriott Marquis</h3></a>'
    )
    page.url = "https://www.marriott.com/reservation/rateListMenu.mi"
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))

    await provider._open_hotel("NYCMQ")

    page.goto.assert_awaited_once()
    locator.fill.assert_awaited_once_with(name)
    assert call("textbox", name="Destination") in page.get_by_role.call_args_list
    assert call("button", name="Find Hotels") in page.get_by_role.call_args_list
    assert call("link", name="View Rates") in locator.get_by_role.call_args_list
    assert locator.click.await_count == 3


@pytest.mark.asyncio
async def test_homepage_search_requires_hotel_name(monkeypatch):
    provider = MarriottBrowserProvider()
    get_page = AsyncMock()
    monkeypatch.setattr(provider, "_get_page", get_page)
    with pytest.raises(ProviderError) as exc:
        await provider._open_hotel("NYCMQ")
    assert exc.value.code == ErrorCode.NOT_IMPLEMENTED
    get_page.assert_not_awaited()


@pytest.mark.asyncio
async def test_delayed_access_denied_is_not_reported_as_timeout(monkeypatch):
    provider = MarriottBrowserProvider(hotel_name="New York Marriott Marquis")
    page = MagicMock()
    page.locator.return_value.inner_text = AsyncMock(return_value="Access Denied")
    provider._page = page
    monkeypatch.setattr(
        provider, "_open_hotel", AsyncMock(side_effect=PlaywrightTimeout("search box missing"))
    )
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))

    with pytest.raises(ProviderError) as exc:
        await provider.search_rates(request)

    assert exc.value.code == ErrorCode.BLOCKED_BY_ANTIBOT


def quote_page(*, room_count=1, missing=False, html=None):
    room = MagicMock()
    room.inner_html = AsyncMock(return_value=room_html() if html is None else html)
    button = MagicMock()
    button.count = AsyncMock(return_value=1)
    button.click = AsyncMock()
    room.get_by_role.return_value = button
    room.locator.return_value.first.wait_for = AsyncMock()
    rooms = MagicMock()
    rooms.count = AsyncMock(return_value=room_count)
    rooms.nth.return_value = room
    links = MagicMock()
    links.count = AsyncMock(return_value=0 if missing else room_count)
    page = MagicMock()
    page.locator.side_effect = lambda selector: (
        links if selector.startswith("a[") else SimpleNamespace(filter=lambda **_: rooms)
    )
    page.get_by_role.return_value.check = AsyncMock()
    return page, rooms, room


@pytest.mark.asyncio
async def test_all_rooms_are_read_not_only_first_eight():
    provider = MarriottBrowserProvider()
    page, rooms, room = quote_page(room_count=9)
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    rates = await provider._collect_rates(page, request)
    assert len(rates) == 9
    assert rooms.nth.call_args_list == [call(index) for index in range(9)]
    assert room.inner_html.await_count == 9


@pytest.mark.asyncio
async def test_missing_room_results_are_not_reported_as_no_availability():
    page, _, _ = quote_page(missing=True)
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    with pytest.raises(ProviderError) as exc:
        await MarriottBrowserProvider()._collect_rates(page, request)
    assert exc.value.code == ErrorCode.PROVIDER_CHANGED
    page.get_by_role.assert_not_called()


@pytest.mark.asyncio
async def test_unreadable_quotes_do_not_create_successful_empty_result():
    page, _, _ = quote_page(html=room_html().replace("Non-Member Rate", "Unreadable Rate"))
    request = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
    with pytest.raises(ProviderError) as exc:
        await MarriottBrowserProvider()._collect_rates(page, request)
    assert exc.value.code == ErrorCode.PROVIDER_CHANGED
