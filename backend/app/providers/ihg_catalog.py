"""Discover real hotel identities from IHG's rendered worldwide directory."""

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.providers.ihg_page import PROPERTY_PATH
from app.schemas.domain import CatalogPageData, HotelData

ROOT = "https://www.ihg.com/explore"
REGIONS = {
    "US & Canada",
    "Caribbean",
    "Mexico & Central America",
    "South America",
    "Europe",
    "Middle East",
    "Africa",
    "Asia",
    "Australia & Pacific Islands",
}


def directory_url(url: str) -> str:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.ihg.com"
        or not re.fullmatch(r"/[a-z0-9]+(?:-[a-z0-9]+)*/?", parsed.path)
        or parsed.query
        or parsed.fragment
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG catalog needs an official directory URL")
    return parsed.geturl().rstrip("/")


def parse_catalog(html: str, url: str) -> CatalogPageData:
    url = directory_url(url)
    soup = BeautifulSoup(html, "html.parser")
    directories = set()
    hotels = {}
    reported = None
    if url == ROOT:
        found = set()
        blocks = []
        for item in soup.select(".cmp-accordion__item"):
            title = item.select_one(".cmp-accordion__title")
            name = title.get_text(" ", strip=True) if title else ""
            if name in REGIONS:
                found.add(name)
                blocks.append(item)
        if found != REGIONS:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG worldwide directory regions not loaded")
        complete = True
    else:
        counter = soup.select_one('[data-render-hotel-count="true"] #cmp-card__title-bar-count')
        text = counter.get_text(strip=True).replace(",", "") if counter else ""
        if text.isdigit():
            reported = int(text)
        elif not any(
            h.get_text(" ", strip=True).startswith("Featured Hotels in ") for h in soup.select("h2")
        ):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG hotel directory section not loaded")
        metadata = {}
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                item = json.loads(script.string or script.get_text())
            except (ValueError, TypeError):
                continue
            if isinstance(item, dict) and item.get("@type") == "Hotel" and isinstance(item.get("url"), str):
                metadata[item["url"].rstrip("/")] = item
        links = soup.select("h4 a[href]")
        observed = 0
        for link in links:
            href = urlparse(urljoin(url, link["href"]))
            identity = PROPERTY_PATH.fullmatch(href.path)
            if (
                href.scheme != "https"
                or href.netloc != "www.ihg.com"
                or href.query
                or href.fragment
                or not identity
            ):
                continue
            name = link.get_text(" ", strip=True)
            if not name:
                continue
            observed += 1
            code = identity[4].upper()
            meta = metadata.get(href.geturl().rstrip("/"), {})
            address = meta.get("address", {}) if meta.get("name") == name else {}
            if not isinstance(address, dict):
                address = {}
            hotels.setdefault(
                code,
                HotelData(
                    provider="ihg",
                    provider_hotel_id=code,
                    hotel_name=name,
                    brand=identity[1],
                    country=address.get("addressCountry")
                    if isinstance(address.get("addressCountry"), str)
                    else None,
                    region=address.get("addressRegion")
                    if isinstance(address.get("addressRegion"), str)
                    else None,
                    address=address.get("streetAddress")
                    if isinstance(address.get("streetAddress"), str)
                    else None,
                    city=address.get("addressLocality")
                    if isinstance(address.get("addressLocality"), str)
                    else identity[3],
                    official_url=href.geturl().rstrip("/"),
                ),
            )
        # A featured subset or unsupported hotel URL is not full page coverage.
        if not links and reported != 0:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG directory hotel links not loaded")
        complete = reported is not None and observed == len(links) == len(hotels) == reported
        blocks = soup.select(".explorelist")
    for block in blocks:
        for link in block.select("a[href]"):
            if not link.get_text(" ", strip=True).endswith("Hotels"):
                continue
            try:
                child = directory_url(urljoin(url, link["href"]))
            except ProviderError:
                continue
            if child != url:
                directories.add(child)
    if url == ROOT and not directories:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG worldwide directory links missing")
    return CatalogPageData(
        provider="ihg",
        source_url=url,
        directory_urls=sorted(directories),
        hotels=list(hotels.values()),
        reported_total=reported,
        complete=complete,
    )
