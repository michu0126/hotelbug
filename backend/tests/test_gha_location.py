from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.gha_browser import GHABrowserProvider, public_detail_location
from app.schemas.domain import HotelData


def rendered_header(
    name="Anantara Maia Seychelles Villas",
    address="West Coast Road, Anse Louis, Seychelles",
    location="Anse Louis, Seychelles",
):
    # Public header structure observed on an actual new directory task.
    return f"""<section><div class="my-auto"><h1 class="HotelPage_hotelName__5psLP">{name}</h1>
    <div class="flex flex-col mb-8"><div class="hidden md:block">{address}</div>
    <div class="hidden md:block">{location}</div>
    <div class="flex hidden md:block"><span>Tel.: public hotel phone</span><span>View Map</span></div></div>
    </div></section>"""


def test_observed_public_address_city_country_not_guessed_from_hotel_name():
    result = public_detail_location(rendered_header())
    assert result == {
        "hotel_name": "Anantara Maia Seychelles Villas",
        "address": "West Coast Road, Anse Louis, Seychelles",
        "city": "Anse Louis",
        "country": "Seychelles",
    }
    assert public_detail_location(rendered_header(name="Hotel in Anywhere"))["country"] == "Seychelles"


def test_observed_nice_leading_numeric_postal_slot_is_not_misread_as_city():
    result = public_detail_location(
        rendered_header(
            name="Anantara Plaza Nice Hotel", address="12 Avenue de Verdun", location="06 000, Nice, France"
        )
    )
    assert result["city"] == "Nice" and result["country"] == "France"
    assert result["address"] == "12 Avenue de Verdun"


@pytest.mark.parametrize("label", ["Unknown", "City, Region, Country", ", Country", "City, ", "City, Room 2"])
def test_ambiguous_city_country_preserves_address_but_leaves_unknowns(label):
    result = public_detail_location(rendered_header(location=label))
    assert result["address"] == "West Coast Road, Anse Louis, Seychelles"
    assert result["city"] is result["country"] is None


def test_changed_header_slots_not_filled_from_other_menu_or_advertisement():
    result = public_detail_location(rendered_header().replace("View Map", "Marketing"))
    assert result["address"] is result["city"] is result["country"] is None
    assert public_detail_location("<h1>Unrelated page</h1><address>Country</address>") is None
    assert public_detail_location(rendered_header() + rendered_header()) is None


@pytest.mark.parametrize(
    "name,url,expected",
    [
        (
            "Anantara Maia Seychelles Villas",
            "https://www.ghadiscovery.com/anantara/anantara-maia-seychelles-villas",
            True,
        ),
        (
            " ANANTARA Maia Seychelles Villas ",
            "https://www.ghadiscovery.com/anantara/anantara-maia-seychelles-villas/",
            True,
        ),
        ("Another hotel", "https://www.ghadiscovery.com/anantara/anantara-maia-seychelles-villas", False),
        ("Anantara Maia Seychelles Villas", "https://www.ghadiscovery.com/anantara/another-hotel", False),
    ],
)
async def test_details_location_only_after_booking_identity_matches(monkeypatch, name, url, expected):
    source = "https://www.ghadiscovery.com/anantara/anantara-maia-seychelles-villas"
    provider = GHABrowserProvider()
    page = MagicMock()
    page.url = source
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.content = AsyncMock(return_value=rendered_header())
    link = MagicMock()
    link.first = link
    link.wait_for = AsyncMock()
    link.get_attribute = AsyncMock(return_value="/booking/select_room?hotelId=12345")
    page.locator.return_value = link
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    original = HotelData(provider="gha", provider_hotel_id="12345", hotel_name=name, official_url=url)
    monkeypatch.setattr(provider, "get_hotel_details", AsyncMock(return_value=original))
    result = (await provider.search_hotels({"official_url": source}))[0]
    assert result.country == ("Seychelles" if expected else None)
    assert result.city == ("Anse Louis" if expected else None)
    assert result.address == ("West Coast Road, Anse Louis, Seychelles" if expected else None)
    assert result.provider_hotel_id == "12345" and "active" not in result.model_fields_set
