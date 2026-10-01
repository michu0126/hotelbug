from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import ProviderError
from app.providers.ihg_page import (
    WINDOW_CLOSED,
    booking_window_closed,
    parse_hotel,
    parse_room,
    verify_displayed_stay,
)
from app.schemas.domain import RateRequest

SEARCH = RateRequest(provider_hotel_id="LONLS", check_in=date(2026, 10, 20), check_out=date(2026, 10, 21))
HOTEL = "Hotel Indigo London - 1 Leicester Square"
URL = (
    "https://www.ihg.com/hotelindigo/hotels/us/en/find-hotels/select-roomrate"
    "?qSlH=LONLS&qRms=1&qAdlt=2&qChld=0&qCiD=20&qCiMy=092026&qCoD=21&qCoMy=092026&qRtP=6CBARC"
)


def room_html():
    # Minimal projection of actual LONLS public DOM, two adults, 2026-10-01.
    html = '<h3 data-testid="roomNameTestId" id="room-card-title-OAAN">1 Double Standard Windowless</h3>'
    html += '<input role="switch" aria-checked="false">'
    for code, title, amount, policy, breakfast in (
        ("IDAPF", "Book Early & Save", "368", "non-refundable", False),
        ("IDFSR", "IHG Flexible Saver", "394", "refundable", False),
        ("IKAPF", "Book Early & Save Breakfast", "398", "non-refundable", True),
        ("IGCOR", "Best Flexible Rate", "409", "refundable", False),
        ("IKFSR", "IHG Flexible Saver Breakfast", "424", "refundable", True),
        ("IGBBB", "Best Flexible With Breakfast", "439", "refundable", True),
    ):
        html += f'''<div data-testid="rateCard"><button data-testid="rateNameOrPolicy">{title}</button>
        <span data-testid="{policy}">Terms</span>
        <div data-testid="priceSID"><span class="cash">{amount}</span><span class="currency">GBP</span></div>
        <span data-testid="guest-count-info">per night</span>
        <button data-testid="select-btn" data-slnm-ihg="roomRateOAAN{code}">Select</button>'''
        if breakfast:
            html += '<span data-testid="amenityTextID">Includes breakfast</span>'
        html += "</div>"
    return html


def test_six_public_plans_have_explicit_room_and_rate_codes():
    rates = parse_room(room_html(), SEARCH, URL, "OAAN")
    assert [r.cash_price for r in rates] == list(map(Decimal, ("368", "394", "398", "409", "424", "439")))
    assert [r.rate_code for r in rates] == ["IDAPF", "IDFSR", "IKAPF", "IGCOR", "IKFSR", "IGBBB"]
    assert all(r.room_code == "OAAN" and r.total_price is None and r.member_rate is False for r in rates)
    assert rates[0].refundable is False and rates[1].refundable is True
    assert rates[0].breakfast_included is None and rates[2].breakfast_included is True


@pytest.mark.parametrize(
    "old,new",
    [
        ('aria-checked="false"', 'aria-checked="true"'),
        ("room-card-title-OAAN", "room-card-title-OQNN"),
        ("roomRateOAANIDAPF", "roomRateOQNNIDAPF"),
        ("roomRateOAANIDAPF", "roomRateOAANIDFSR"),
        (">368<", ">368,50<"),
        (">GBP<", ">£<"),
        (">per night<", ">starting from<"),
        ("rateCard", "missingRateCard"),
    ],
)
def test_member_wrong_identity_or_unreadable_quote_is_rejected(old, new):
    with pytest.raises(ProviderError):
        parse_room(room_html().replace(old, new), SEARCH, URL, "OAAN")


@pytest.mark.parametrize(
    "old,new",
    [
        ("qCiMy=092026", "qCiMy=102026"),
        ("qSlH=LONLS", "qSlH=LONHB"),
        ("qAdlt=2", "qAdlt=1"),
        ("qRms=1", "qRms=2"),
        ("qChld=0", "qChld=1"),
        ("qRtP=6CBARC", "qRtP=GOV01"),
        ("qCoD=21", "qCoD=22"),
        ("www.ihg.com", "example.com"),
    ],
)
def test_booking_query_matches_full_stay_and_eligibility(old, new):
    with pytest.raises(ProviderError):
        parse_room(room_html(), SEARCH, URL.replace(old, new), "OAAN")


def test_rendered_stay_and_official_hotel_metadata():
    html = f'<h2 data-testid="hotelDetailsInfoHotelName">{HOTEL}</h2>'
    verify_displayed_stay(html, SEARCH, HOTEL, ["10/20/2026", "10/21/2026"], "1 Room, 2 Guests")
    with pytest.raises(ProviderError):
        verify_displayed_stay(html, SEARCH, HOTEL, ["10/20/2027", "10/21/2027"], "1 Room, 2 Guests")
    with pytest.raises(ProviderError):
        verify_displayed_stay(html, SEARCH, HOTEL, ["10/20/2026", "10/21/2026"], "1 Room, 1 Guest")
    hotel = parse_hotel(
        f"<h1>{HOTEL}</h1>", "https://www.ihg.com/hotelindigo/hotels/us/en/london/lonls/hoteldetail"
    )
    assert hotel.provider_hotel_id == "LONLS" and hotel.hotel_name == HOTEL


def window_html():
    return f'<h2 data-testid="hotelDetailsInfoHotelName">{HOTEL}</h2><p>{WINDOW_CLOSED}</p>' + (
        '<a href="https://www.ihg.com/hotelindigo/hotels/us/en/london/lonls/hoteldetail/hotel-reviews">Reviews</a>'
    )


def test_not_open_requires_explicit_message_full_dates_and_hotel_identity():
    dates, guests = ["10/20/2026", "10/21/2026"], "1 Room, 2 Guests"
    assert booking_window_closed(window_html(), SEARCH, HOTEL, dates, guests)
    assert not booking_window_closed(
        window_html().replace(WINDOW_CLOSED, "0 rooms found"), SEARCH, HOTEL, dates, guests
    )
    with pytest.raises(ProviderError):
        booking_window_closed(
            window_html().replace("lonls/hoteldetail", "lonhb/hoteldetail"), SEARCH, HOTEL, dates, guests
        )
    with pytest.raises(ProviderError):
        booking_window_closed(window_html(), SEARCH, HOTEL, ["10/20/2027", "10/21/2027"], guests)
