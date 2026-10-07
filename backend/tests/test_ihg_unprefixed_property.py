"""Actual public HUALUXE directory paths without a brand prefix."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProviderError
from app.providers.ihg_browser import IHGBrowserProvider
from app.providers.ihg_catalog import parse_catalog
from app.providers.ihg_page import parse_hotel, property_identity


@pytest.mark.parametrize(
    "name,city,code",
    [
        ("HUALUXE Beihai Silver Beach Resort", "beihai", "bhybr"),
        ("HUALUXE Xianyang", "xianyang", "sialk"),
        ("HUALUXE Leshan", "leshan", "ctule"),
        ("HUALUXE Chongqing Science Hall", "chongqing", "ckghl"),
        ("HUALUXE Kunshan Huaqiao", "kunshan", "shakh"),
        ("HUALUXE Xi'an Chanba", "xian", "siaxc"),
        ("HUALUXE Xi'an Tanghua", "xian", "siath"),
        ("HUALUXE Qidong", "qidong", "ntgqg"),
        ("HUALUXE Sanya Yalong Bay Resort", "sanya", "syxyl"),
        ("HUALUXE Haining", "haining", "tvxux"),
        ("HUALUXE Nanjing Yangtze River", "nanjing", "nkgyr"),
        ("HUALUXE Beijing Xinan", "beijing", "pekbx"),
        ("HUALUXE Yibin", "yibin", "ybpys"),
        ("HUALUXE Xiamen Haicang Harbour View", "xiamen", "xmnad"),
    ],
)
def test_real_brandless_directory_link_retains_code_and_unknown_brand(name, city, code):
    url = f"https://www.ihg.com/hotels/us/en/{city}/{code}/hoteldetail"
    html = f'''<div data-render-hotel-count="true"><span id="cmp-card__title-bar-count">1</span></div>
        <h4><a href="{url}">{name}</a></h4>'''
    result = parse_catalog(html, "https://www.ihg.com/mainland-china")
    hotel = result.hotels[0]
    assert result.complete and result.unparsed_hotel_links == 0
    assert hotel.provider_hotel_id == code.upper() and hotel.hotel_name == name
    assert hotel.brand is None and hotel.country is None
    assert str(hotel.official_url) == url
    parsed = parse_hotel(f"<h1>{name}</h1>", url)
    assert parsed.provider_hotel_id == code.upper() and parsed.brand is None


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/hotels/us/en/beihai/bhybr/hoteldetail",
        "http://www.ihg.com/hotels/us/en/beihai/bhybr/hoteldetail",
        "https://www.ihg.com/hotels/us/en/beihai/bhybr/hoteldetail?private=1",
        "https://www.ihg.com/hotels/us/en/beihai/bhyb/hoteldetail",
    ],
)
def test_brandless_path_does_not_accept_different_host_or_ambiguous_code(url):
    with pytest.raises(ProviderError):
        property_identity(url)


async def test_normal_detail_flow_accepts_unprefixed_hotel_without_changing_code(monkeypatch):
    url = "https://www.ihg.com/hotels/us/en/beihai/bhybr/hoteldetail"
    provider = IHGBrowserProvider()
    provider.configure_hotel_url(url)
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="HUALUXE Beihai Silver Beach Resort")
    page.content = AsyncMock(return_value="<h1>HUALUXE Beihai Silver Beach Resort</h1>")
    page.locator.return_value.first.wait_for = AsyncMock()
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    hotel = await provider.get_hotel_details("BHYBR")
    assert hotel.provider_hotel_id == "BHYBR" and hotel.brand is None
    assert page.goto.await_args.args == (url,)
