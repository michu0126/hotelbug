"""Actual public ALL Hotel schema projection, not API/application state."""

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.providers.accor_browser import AccorBrowserProvider, public_detail_location

NAME = "Mercure Lyon Charbonnières Hotel"
URL = "https://all.accor.com/hotel/0345/index.en.shtml"


def observed_schema():
    # Public projection of the new 0345 detail page read 2026-10-05.
    return {
        "@type": "Hotel",
        "name": NAME + " - ALL",
        "address": {
            "streetAddress": "78  bis route de Paris, Route nationale 7",
            "addressLocality": "CHARBONNIERES LES BAINS",
            "postalCode": "69260",
            "addressCountry": "FR",
        },
        "geo": {"latitude": "45.780829", "longitude": "4.750488"},
    }


def html(value):
    return '<script type="application/ld+json">' + json.dumps(value) + "</script>"


def test_actual_public_location_keeps_country_code_city_and_coordinates():
    value = public_detail_location(html(observed_schema()), NAME, "0345", URL)
    assert value == {
        "address": "78 bis route de Paris, Route nationale 7",
        "city": "CHARBONNIERES LES BAINS",
        "country": "FR",
        "latitude": Decimal("45.780829"),
        "longitude": Decimal("4.750488"),
    }
    assert "brand" not in value and "default_currency" not in value


@pytest.mark.parametrize(
    "name,url",
    [
        ("Another hotel", URL),
        (NAME, "https://all.accor.com/hotel/0346/index.en.shtml"),
        (NAME, "https://example.com/hotel/0345/index.en.shtml"),
        (NAME, "http://all.accor.com/hotel/0345/index.en.shtml"),
    ],
)
def test_wrong_page_or_name_does_not_merge_location(name, url):
    assert public_detail_location(html(observed_schema()), name, "0345", url) == {}


def test_ambiguous_hotels_missing_names_and_nonhotel_scripts_are_not_guessed():
    value = observed_schema()
    assert public_detail_location(html([value, value]), NAME, "0345", URL) == {}
    assert public_detail_location(html({**value, "name": None}), NAME, "0345", URL) == {}
    assert public_detail_location(html({**value, "@type": "Organization"}), NAME, "0345", URL) == {}
    assert public_detail_location('<script type="application/ld+json">bad</script>', NAME, "0345", URL) == {}


def test_graph_selects_exact_hotel_not_nearby_hotels_or_other_private_fields():
    value = observed_schema()
    value["token"] = "PRIVATE"
    value["address"]["cookie"] = "PRIVATE"
    result = public_detail_location(
        html({"@graph": [{**value, "name": "Nearby hotel"}, value]}), NAME, "0345", URL
    )
    assert result["country"] == "FR" and "PRIVATE" not in str(result)


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "bad", True, 181])
def test_invalid_coordinate_keeps_valid_address_without_poisoning_discovery(bad):
    value = observed_schema()
    value["geo"] = {"latitude": bad, "longitude": bad}
    result = public_detail_location(html(value), NAME, "0345", URL)
    assert result["country"] == "FR"
    assert "latitude" not in result and "longitude" not in result


def test_missing_or_object_country_stays_unknown_and_private_state_is_ignored():
    value = observed_schema()
    value["address"] = {"addressLocality": " ", "addressCountry": {"name": "France"}}
    result = public_detail_location(
        html(value) + '<script>window.country="Guessed"</script>', NAME, "0345", URL
    )
    assert "city" not in result and "country" not in result


async def test_provider_persists_location_from_same_page_without_extra_navigation(monkeypatch):
    provider = AccorBrowserProvider()
    page = MagicMock()
    page.url = URL
    page.content = AsyncMock(return_value=html(observed_schema()))
    heading = MagicMock()
    heading.first = heading
    heading.inner_text = AsyncMock(return_value=NAME + " 4 stars")
    page.locator.return_value = heading
    opened = AsyncMock(return_value=page)
    monkeypatch.setattr(provider, "_open", opened)
    details = await provider.get_hotel_details("0345")
    assert details.country == "FR" and details.city == "CHARBONNIERES LES BAINS"
    assert details.latitude == Decimal("45.780829") and "active" not in details.model_fields_set
    opened.assert_awaited_once_with("0345")
    page.goto.assert_not_called()


def test_actual_seo_title_difference_accepts_only_named_public_location_address():
    # Minimal DOM projection of the observed Auxerre labels; not full HTML.
    name = "Hotel Mercure Auxerre Autoroute du Soleil"
    value = {
        "@type": "Hotel",
        "name": "4-star hotel in Auxerre - Mercure Auxerre - ALL - ALL",
        "address": {
            "streetAddress": 'Route nationale 6, lieu dit "le chaumois"',
            "addressLocality": "APPOIGNY",
            "addressCountry": "FR",
        },
        "geo": {"latitude": "47.852896", "longitude": "3.543895"},
    }
    visible = (
        '<section><h2>Hotel location</h2><div><p class="infos__title">'
        + name
        + '</p><p>Route nationale 6, lieu dit "le chaumois" 89000 APPOIGNY France</p></div></section>'
    )
    result = public_detail_location(
        html(value) + visible, name, "0348", "https://all.accor.com/hotel/0348/index.en.shtml"
    )
    assert result["city"] == "APPOIGNY" and result["country"] == "FR"
    assert result["latitude"] == Decimal("47.852896")
    for wrong in (
        visible.replace(name, "Nearby hotel"),
        visible.replace("APPOIGNY", "Other town"),
        visible.replace("infos__title", "place__title"),
        visible.replace("Hotel location", "Access and transport"),
    ):
        assert (
            public_detail_location(
                html(value) + wrong, name, "0348", "https://all.accor.com/hotel/0348/index.en.shtml"
            )
            == {}
        )


def test_seo_name_cannot_borrow_address_from_body_or_ambiguous_sections():
    value = {**observed_schema(), "name": "Different public SEO title"}
    unrelated = (
        '<p class="infos__title">'
        + NAME
        + "</p><p>78 bis route de Paris, Route nationale 7 CHARBONNIERES LES BAINS France</p>"
    )
    assert public_detail_location(html(value) + unrelated, NAME, "0345", URL) == {}
    section = "<section><h2>Hotel location</h2>" + unrelated + "</section>"
    assert public_detail_location(html(value) + section + section, NAME, "0345", URL) == {}
