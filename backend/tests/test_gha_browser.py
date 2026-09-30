from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import ProviderError
from app.providers.gha_browser import booking_url, parse_public_offer, public_room, verify_stay
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


@pytest.mark.parametrize("old,new", [("07 Oct", "08 Oct"), ("2026", "2027"), ("2 ADULTS", "3 ADULTS")])
def test_stay_mismatch(old, new):
    with pytest.raises(ProviderError):
        verify_stay(STAY.replace(old, new), booking_url(SEARCH), SEARCH)


def test_wrong_hotel_url_and_missing_room_price():
    with pytest.raises(ProviderError):
        verify_stay(STAY, booking_url(SEARCH).replace("10624", "99999"), SEARCH)
    with pytest.raises(ProviderError):
        public_room(ROOM.replace("NON-MEMBER RATES", "MEMBER RATES FROM"))
