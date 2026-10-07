from datetime import date
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProviderError
from app.providers.gha_browser import (
    GHABrowserProvider,
    booking_url,
    no_availability,
    parse_public_offer,
    public_room,
    verify_stay,
)
from app.schemas.domain import RateRequest

SEARCH = RateRequest(provider_hotel_id="10624", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
STAY = "<p>Wed, 07 Oct - Thu, 08 Oct 2026</p><p>ROOM 1: 2 ADULTS</p>"
ROOM = '<h5 class="px-5">DELUXE KING</h5><div><span>NON-MEMBER RATES</span><h5>THB 6,944</h5></div><span>Including taxes and fees</span>'
OFFER = '<div><h5>Avani Flexi</h5><div><h5 class="text-dark">THB 6,944/<span>night</span></h5><span>Including taxes and fees</span></div></div>'


def test_public_quote_matches_room_and_plan():
    verify_stay(STAY, booking_url(SEARCH), SEARCH)
    room = public_room(ROOM)
    rate = parse_public_offer(OFFER, room, SEARCH)
    assert rate.total_price == Decimal("6944")
    assert rate.member_rate is False
    assert rate.price_basis == "nightly"
    assert rate.room_type == "DELUXE KING"
    assert rate.rate_name == "Avani Flexi"


@pytest.mark.parametrize(
    "replacement", ["DISCOVERY Avani Flexi", "DISCOVERY - AVANI Flexi", "Members Avani Flexi"]
)
def test_member_plans_excluded_even_if_same_price(replacement):
    assert parse_public_offer(OFFER.replace("Avani Flexi", replacement), public_room(ROOM), SEARCH) is None


@pytest.mark.parametrize(
    "old,new", [("THB", "USD"), ("6,944", "0"), ("Including", "Excluding"), ("text-dark", "text-gold")]
)
def test_unverified_price_not_accepted(old, new):
    assert parse_public_offer(OFFER.replace(old, new), public_room(ROOM), SEARCH) is None


def test_public_named_plan_can_be_above_advertised_from_price():
    rate = parse_public_offer(
        OFFER.replace("Avani Flexi", "Best Flexible Rate").replace("6,944", "7,200"),
        public_room(ROOM),
        SEARCH,
    )
    assert rate is not None
    assert rate.total_price == Decimal("7200")
    assert rate.rate_name == "Best Flexible Rate"


def test_public_named_plan_below_advertised_from_price_remains_visible():
    rate = parse_public_offer(
        OFFER.replace("Avani Flexi", "Best Flexible Rate").replace("6,944", "1,000"),
        public_room(ROOM),
        SEARCH,
    )
    assert rate is not None
    assert rate.total_price == Decimal("1000")


def test_capella_from_only_room_is_identity_not_a_bookable_price():
    room_html = (
        '<h5 class="px-5">Superior Accessible King Room</h5>'
        "<div><span>FROM</span><h5>USD 1,189</h5></div>"
        "<span>Including taxes and fees</span>"
    )
    room = public_room(room_html)
    assert room == ("Superior Accessible King Room", "USD", Decimal("1189"))
    assert parse_public_offer(room_html, room, SEARCH) is None
    actual_plan = (
        OFFER.replace("THB", "USD").replace("6,944", "1,250").replace("Avani Flexi", "Flexible Rate")
    )
    rate = parse_public_offer(actual_plan, room, SEARCH)
    assert rate.total_price == Decimal("1250")
    assert rate.room_type == "Superior Accessible King Room"
    assert rate.rate_name == "Flexible Rate"
    assert (
        parse_public_offer(actual_plan.replace("Flexible Rate", "DISCOVERY Flexible Rate"), room, SEARCH)
        is None
    )


def test_explicit_member_only_summary_is_not_used_as_from_fallback():
    with pytest.raises(ProviderError):
        public_room(ROOM.replace("NON-MEMBER RATES", "MEMBER RATES FROM"))


def test_actual_amalfi_member_summary_is_identity_only_never_saved_as_public_price():
    card = '<h5 class="px-5">Junior Suite with Views and Extra Bed</h5><div><span>MEMBER RATES FROM</span><h5>EUR 1,877</h5></div><span>Including taxes and fees</span>'
    room = public_room(card, allow_member_identity=True)
    assert room == ("Junior Suite with Views and Extra Bed", "EUR", Decimal("1877"))
    assert parse_public_offer(card, room, SEARCH) is None
    member = OFFER.replace("THB", "EUR").replace("Avani Flexi", "DISCOVERY Flexible Rate")
    assert parse_public_offer(member, room, SEARCH) is None
    public = (
        OFFER.replace("THB", "EUR").replace("6,944", "1,920").replace("Avani Flexi", "Best Flexible Rate")
    )
    rate = parse_public_offer(public, room, SEARCH)
    assert rate.total_price == Decimal("1920") and rate.member_rate is False


@pytest.mark.parametrize(
    "old,new",
    [("Including taxes and fees", "Excluding taxes"), ("EUR 1,877", "1,877"), ("px-5", "different")],
)
def test_member_identity_does_not_relax_room_currency_or_tax_guards(old, new):
    card = '<h5 class="px-5">Junior Suite with Views and Extra Bed</h5><div><span>MEMBER RATES FROM</span><h5>EUR 1,877</h5></div><span>Including taxes and fees</span>'
    with pytest.raises(ProviderError):
        public_room(card.replace(old, new), allow_member_identity=True)


async def test_member_only_room_does_not_abort_public_quotes_from_other_rooms(monkeypatch):
    provider = GHABrowserProvider()
    page = MagicMock()
    page.url = booking_url(SEARCH)
    page.content = AsyncMock(return_value=STAY)
    page.wait_for_function = AsyncMock()
    checkbox = page.get_by_role.return_value
    checkbox.is_checked = AsyncMock(return_value=True)
    page.get_by_text.return_value.first.wait_for = AsyncMock()
    buttons = MagicMock()
    buttons.count = AsyncMock(return_value=3)
    controls = []
    for index in range(3):
        button = MagicMock()
        card = ROOM.replace("DELUXE KING", f"ROOM {index}")
        if index == 1:
            card = card.replace("NON-MEMBER RATES", "MEMBER RATES FROM")
        button.evaluate = AsyncMock(return_value=card)
        button.click = AsyncMock()
        controls.append(button)
    buttons.nth.side_effect = lambda index: controls[index]
    plans = MagicMock()
    plans.first.wait_for = AsyncMock()
    plans.evaluate_all = AsyncMock(
        side_effect=[
            [OFFER],
            [OFFER.replace("Avani Flexi", "DISCOVERY Flexible Rate")],
            [OFFER],
        ]
    )
    close = MagicMock()
    close.click = AsyncMock()
    page.locator.side_effect = lambda selector: (
        buttons if "tid-viewRates" in selector else close if "closeButton" in selector else plans
    )
    monkeypatch.setattr(provider, "_open_booking", AsyncMock(return_value=page))
    monkeypatch.setattr(provider, "_expand_rate_pages", AsyncMock())
    rates = await provider.search_rates(SEARCH)
    assert {rate.room_type for rate in rates} == {"ROOM 0", "ROOM 2"}
    assert all(rate.member_rate is False for rate in rates)
    assert all(button.click.await_count == 1 for button in controls) and close.click.await_count == 3
    plans.click.assert_not_called()


@pytest.mark.parametrize("old,new", [("07 Oct", "08 Oct"), ("2026", "2027"), ("2 ADULTS", "3 ADULTS")])
def test_stay_mismatch(old, new):
    with pytest.raises(ProviderError):
        verify_stay(STAY.replace(old, new), booking_url(SEARCH), SEARCH)


def test_wrong_hotel_url_and_missing_room_price():
    with pytest.raises(ProviderError):
        verify_stay(STAY, booking_url(SEARCH).replace("10624", "99999"), SEARCH)
    with pytest.raises(ProviderError):
        public_room(ROOM.replace("NON-MEMBER RATES", "MEMBER RATES FROM"))


def test_explicitly_unavailable_dates_are_not_zero_price():
    assert no_availability("<p>Selection not available for these dates. Select other room options.</p>")
    assert not no_availability(ROOM)


async def test_explicit_no_availability_returns_empty_quotes(monkeypatch):
    provider = GHABrowserProvider()
    page = MagicMock()
    page.wait_for_function = AsyncMock()
    page.locator.return_value.count = AsyncMock(return_value=0)
    page.content = AsyncMock(return_value="<p>Selection not available for these dates.</p>")
    monkeypatch.setattr(provider, "_open_booking", AsyncMock(return_value=page))

    assert await provider.search_rates(SEARCH) == []
    page.wait_for_function.assert_awaited_once()


async def test_rate_pagination_has_no_ten_page_truncation_or_duplicate_count_assumption():
    provider = GHABrowserProvider()
    page = MagicMock()
    more = MagicMock()
    more.count = AsyncMock(side_effect=[1] * 12 + [0])
    more.click = AsyncMock()
    cards = MagicMock()
    cards.count = AsyncMock(side_effect=range(1, 13))
    page.locator.side_effect = lambda selector: more if "viewMoreRates" in selector else cards
    page.wait_for_function = AsyncMock()

    await provider._expand_rate_pages(page)
    assert more.click.await_count == 12
    assert page.wait_for_function.await_count == 12
    for call in page.wait_for_function.await_args_list:
        assert "getClientRects" in call.args[0]
        assert "*2" not in call.args[0]


async def test_rate_pagination_timeout_does_not_silently_accept_partial_plans():
    provider = GHABrowserProvider()
    page = MagicMock()
    page.locator.return_value.count = AsyncMock(return_value=1)
    page.locator.return_value.click = AsyncMock()
    page.wait_for_function = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(ProviderError, match="did not finish loading"):
        await provider._expand_rate_pages(page)
