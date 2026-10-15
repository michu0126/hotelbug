"""Observed IGMAV hero title differs from its marketing H1 and booking brand spelling."""

from datetime import date

import pytest

from app.core.errors import ProviderError
from app.providers.ihg_page import NO_ROOMS, hotel_search_unavailable, parse_hotel, verify_displayed_stay
from app.schemas.domain import RateRequest

PROPERTY = "https://www.ihg.com/avidhotels/hotels/us/en/kingman/igmav/hoteldetail"
NAME = "avid hotel Kingman - Medical Center Area"
REQUEST = RateRequest(provider_hotel_id="IGMAV", check_in=date(2026, 10, 8), check_out=date(2026, 10, 9))
DATES = ["10/08/2026", "10/09/2026"]


def test_avid_fix_preserves_other_brand_first_h1_behavior():
    url = "https://www.ihg.com/holidayinnexpress/hotels/us/en/flagstaff/flgll/hoteldetail"
    hotel = parse_hotel("<h1>Holiday Inn Express Flagstaff</h1><h1>Marketing headline</h1>", url)
    assert hotel.hotel_name == "Holiday Inn Express Flagstaff" and hotel.provider_hotel_id == "FLGLL"


def test_missing_display_heading_cannot_match_an_empty_expected_name():
    with pytest.raises(ProviderError):
        verify_displayed_stay("<p>No hotel heading</p>", REQUEST, "", DATES, "1 Room, 2 Guests")


@pytest.mark.parametrize("link_code", ["igmav", "other"])
def test_unavailable_avid_alias_still_requires_exact_target_card_link(link_code):
    html = (
        '<app-hotel-card-list-view data-testid="hotel-card" id="IGMAV">'
        f'<h2 data-testid="brandHotelNameSID">{NAME.replace("hotel ", "hotels ")}</h2>'
        f'<a href="{PROPERTY.replace("igmav", link_code)}">Hotel website</a>'
        f"<p>{NO_ROOMS}</p></app-hotel-card-list-view>"
    )
    url = "https://www.ihg.com/avidhotels/hotels/us/en/find-hotels/hotel-search?qRms=1&qAdlt=2&qChld=0&qCiD=08&qCiMy=092026&qCoD=09&qCoMy=092026&qRtP=6CBARC"
    if link_code == "igmav":
        assert hotel_search_unavailable(html, url, REQUEST, NAME, DATES, "1 Room, 2 Guests")
    else:
        with pytest.raises(ProviderError):
            hotel_search_unavailable(html, url, REQUEST, NAME, DATES, "1 Room, 2 Guests")


@pytest.mark.parametrize("marketing_first", [False, True])
def test_avid_hero_title_not_marketing_h1_is_property_name(marketing_first):
    hero = f'<div id="hotel-hero-left"><div class="rte text"><div class="cmp-text"><h2><span>{NAME}</span></h2></div></div></div>'
    marketing = "<h1>Welcome to avid hotels in Kingman, where the essentials are done right. Every time.</h1>"
    hotel = parse_hotel(marketing + hero if marketing_first else hero + marketing, PROPERTY)
    assert hotel.hotel_name == NAME and hotel.provider_hotel_id == "IGMAV"


@pytest.mark.parametrize("hero", ["", "<h2></h2>", f"<h2>{NAME}</h2><h2>Other hotel</h2>"])
def test_missing_or_ambiguous_avid_hero_is_not_replaced_with_marketing_title(hero):
    with pytest.raises(ProviderError):
        parse_hotel(f'<div id="hotel-hero-left">{hero}</div><h1>Welcome to Kingman</h1>', PROPERTY)


@pytest.mark.parametrize("reverse", [False, True])
def test_observed_avid_singular_plural_alias_requires_same_official_review_code(reverse):
    displayed, expected = NAME.replace("avid hotel ", "avid hotels "), NAME
    if reverse:
        displayed, expected = expected, displayed
    html = f'<h2 data-testid="hotelDetailsInfoHotelName">{displayed}</h2><a href="{PROPERTY}/hotel-reviews?qSlH=IGMAV">3 reviews</a>'
    verify_displayed_stay(html, REQUEST, expected, DATES, "1 Room, 2 Guests")


@pytest.mark.parametrize(
    "displayed,review",
    [
        (NAME.replace("hotel ", "hotels "), ""),
        (NAME.replace("hotel ", "hotels "), PROPERTY.replace("igmav", "lonls") + "/hotel-reviews"),
        (
            NAME.replace("hotel ", "hotels "),
            PROPERTY.replace("www.ihg.com", "other.example") + "/hotel-reviews",
        ),
        (NAME.replace("Kingman", "Flagstaff"), PROPERTY + "/hotel-reviews"),
        (NAME.replace("hotel ", "hotels "), PROPERTY + "/hotel-reviews?qSlH=LONLS"),
    ],
)
def test_avid_alias_never_accepts_missing_conflicting_or_different_property(displayed, review):
    html = f'<h2 data-testid="hotelDetailsInfoHotelName">{displayed}</h2><a href="{review}">Reviews</a>'
    with pytest.raises(ProviderError):
        verify_displayed_stay(html, REQUEST, NAME, DATES, "1 Room, 2 Guests")


@pytest.mark.parametrize(
    "dates,guests", [(["10/09/2026", "10/10/2026"], "1 Room, 2 Guests"), (DATES, "1 Room, 1 Guest")]
)
def test_valid_avid_name_alias_does_not_relax_dates_or_occupancy(dates, guests):
    html = f'<h2 data-testid="hotelDetailsInfoHotelName">{NAME.replace("hotel ", "hotels ")}</h2><a href="{PROPERTY}/hotel-reviews">Reviews</a>'
    with pytest.raises(ProviderError):
        verify_displayed_stay(html, REQUEST, NAME, dates, guests)
