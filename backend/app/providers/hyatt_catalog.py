"""Hyatt's rendered worldwide accordion list, observed 2026-10-04.

The accordion mounts only the selected region. Read every region through its
normal button; never treat one mounted region as the worldwide directory.
"""

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import CatalogPageData, HotelData

ROOT = "https://www.hyatt.com/zh-CN/explore-hotels/list"
REGIONS = (
    "美国及加拿大",
    "加勒比海地区及拉丁美洲",
    "欧洲",
    "非洲和中东地区",
    "亚洲",
    "澳大利亚及太平洋地区",
)


def directory_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.hyatt.com"
        or parsed.path.rstrip("/") != "/zh-CN/explore-hotels/list"
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt catalog needs its unfiltered official list")
    return ROOT


def property_identity(url: str) -> tuple[str, str | None]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc != "www.hyatt.com" or parsed.query or parsed.fragment:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt property link is not an official detail URL")
    path = parsed.path.rstrip("/")
    modern = re.fullmatch(r"/([a-z0-9-]+)/(?:[a-z]{2}-[A-Z]{2}/)?([a-zA-Z0-9]{5})-[a-zA-Z0-9-]+", path)
    if modern and ("/zh-CN/" in path or "/en-US/" in path or modern[1] == "mr-and-mrs-smith"):
        return modern[2].upper(), modern[1]
    legacy = re.fullmatch(
        r"/(?:hotel/(?:zh-CN|en-US)|(?:zh-CN|en-US)/hotel)/[a-zA-Z0-9-]+/[a-zA-Z0-9-]+/([a-zA-Z0-9]{5})",
        path,
    )
    if legacy:
        return legacy[1].upper(), None
    raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt property link format has changed")


def region_names(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    names = [b.get_text(" ", strip=True) for b in soup.select("main button.dream-list-accordion__trigger")]
    if names != list(REGIONS):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt worldwide region controls changed")
    return names


def parse_region(html: str, name: str) -> tuple[list[HotelData], int]:
    region_names(html)
    soup = BeautifulSoup(html, "html.parser")
    buttons = [
        b
        for b in soup.select("main button.dream-list-accordion__trigger")
        if b.get_text(" ", strip=True) == name
    ]
    if len(buttons) != 1 or buttons[0].get("aria-expanded") != "true":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt requested region is not expanded")
    region = soup.find(id=buttons[0].get("aria-controls"))
    if (
        region is None
        or region.get("role") != "region"
        or region.get("data-state") != "open"
        or region.get("aria-labelledby") != buttons[0].get("id")
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt mounted region identity does not match")
    links = region.select(".dream-list-accordion__property-list-item a[href]")
    if not links:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt expanded region hotel links are missing")
    hotels, unparsed = [], 0
    for link in links:
        title = link.find("span", recursive=False)
        hotel_name = title.get_text(" ", strip=True) if title else ""
        try:
            code, brand = property_identity(link["href"])
        except ProviderError:
            unparsed += 1
            continue
        if not hotel_name:
            unparsed += 1
            continue
        country_node = link.find_parent(class_="dream-list-accordion__country-container")
        country = country_node.select_one("h3.dream-list-accordion__country-header") if country_node else None
        province_node = link.find_parent(class_="dream-list-accordion__province-container")
        province = (
            province_node.select_one("h4.dream-list-accordion__province-header") if province_node else None
        )
        hotels.append(
            HotelData(
                provider="hyatt",
                provider_hotel_id=code,
                hotel_name=hotel_name,
                brand=brand,
                official_url=link["href"],
                country=(country.get_text(" ", strip=True) or None) if country else None,
                region=(province.get_text(" ", strip=True) or None) if province else None,
            )
        )
    return hotels, unparsed


def combine_regions(url: str, observations: dict[str, tuple[list[HotelData], int]]) -> CatalogPageData:
    url = directory_url(url)
    if set(observations) != set(REGIONS):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt worldwide scan omitted a region")
    hotels = {}
    for name in REGIONS:
        for hotel in observations[name][0]:
            old = hotels.get(hotel.provider_hotel_id)
            # Preserve the actual public URL, prefer its Chinese variant when
            # this same property is advertised twice. Never invent a translation.
            if old is None or (
                "/zh-CN/" in str(hotel.official_url) and "/zh-CN/" not in str(old.official_url)
            ):
                hotels[hotel.provider_hotel_id] = hotel
    return CatalogPageData(
        provider="hyatt",
        source_url=url,
        hotels=list(hotels.values()),
        complete=False,
        # No explicit published worldwide total exists on this list. Traversal
        # may be complete without being proof of the hotel's global inventory.
        page_complete=not any(value[1] for value in observations.values()),
        unparsed_hotel_links=sum(value[1] for value in observations.values()),
    )
