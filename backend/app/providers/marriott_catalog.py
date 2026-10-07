"""Marriott's rendered worldwide property directory and public destination cards.

Observed on 2026-10-01 through Book -> Hotel Locations. Directory amounts are
starting prices with inconsistent dates; this module never produces RateData.
"""

import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import CatalogPageData, HotelData

ORIGIN = "https://www.marriott.com"
ROOT = ORIGIN + "/hotel-search.mi"
BUCKETS = ("A-C", "D-F", "G-I", "J-L", "M-O", "P-R", "S-U", "V-X", "Y-Z")
DESTINATION_PATH = re.compile(r"/(?:en|en-us)/destinations/(?:[a-z0-9-]+/)*[a-z0-9-]+\.mi")
PROPERTY_PATH = re.compile(r"/en-us/hotels/travel/([a-z0-9]{5})-[a-z0-9-]+/?")


def directory_url(value: str) -> str:
    url = urlparse(value)
    try:
        valid_origin = url.port in (None, 443) and url.username is None and url.password is None
    except ValueError:
        valid_origin = False
    if url.scheme != "https" or url.hostname != "www.marriott.com" or not valid_origin:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott directory origin invalid")
    if url.path == "/hotel-search.mi":
        if url.query or (url.fragment and not re.fullmatch(r"/\d+/", url.fragment)):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott directory root filters not supported")
        return ROOT
    params = parse_qs(url.query, keep_blank_values=True)
    if not DESTINATION_PATH.fullmatch(url.path) or url.fragment or set(params) - {"pg"}:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott destination URL invalid")
    page = params.get("pg", ["1"])
    if len(page) != 1 or not re.fullmatch(r"[1-9][0-9]*", page[0]):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott directory page number invalid")
    query = urlencode({"pg": int(page[0])}) if int(page[0]) > 1 else ""
    return ORIGIN + url.path + ("?" + query if query else "")


def verify_directory_redirect(source: str, final: str) -> None:
    expected, actual = map(directory_url, (source, final))
    if expected == ROOT:
        matches = actual == ROOT
    else:
        # Published /en/destinations aliases redirect to a country-qualified
        # /en-us/destinations page. Preserve the same destination slug and pg.
        left, right = map(urlparse, (expected, actual))
        matches = left.path.rsplit("/", 1)[-1] == right.path.rsplit("/", 1)[-1] and left.query == right.query
    if not matches:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott destination changed after navigation")


def parse_catalog(
    html: str,
    source: str,
    *,
    final_url: str | None = None,
    root_links: list[str] | None = None,
    visited_buckets: tuple[str, ...] = (),
) -> CatalogPageData:
    source = directory_url(source)
    final = directory_url(final_url or source)
    verify_directory_redirect(source, final)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h1")
    if source == ROOT:
        if heading is None or heading.get_text(" ", strip=True) != "Property Directory":
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott worldwide directory heading missing")
        count = next(
            (
                re.fullmatch(r"([0-9,]+) Properties", h.get_text(" ", strip=True))
                for h in soup.select("h2")
                if re.fullmatch(r"([0-9,]+) Properties", h.get_text(" ", strip=True))
            ),
            None,
        )
        if count is None or int(count[1].replace(",", "")) < 1:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott worldwide property count missing")
        if set(visited_buckets) != set(BUCKETS):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott worldwide letter buckets incomplete")
        links = (
            root_links
            if root_links is not None
            else [a["href"] for a in soup.select("a.destination-item-content-link[href]")]
        )
        children = sorted({directory_url(urljoin(ORIGIN, href)) for href in links})
        if not children or ROOT in children:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott worldwide destination links absent")
        return CatalogPageData(
            provider="marriott",
            source_url=source,
            directory_urls=children,
            reported_total=int(count[1].replace(",", "")),
            complete=False,
            page_complete=True,
        )
    if heading is None or not heading.get_text(" ", strip=True).startswith("Hotels in "):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott destination heading missing")
    counter = next(
        (
            re.fullmatch(r"Showing ([0-9,]+)-([0-9,]+) of ([0-9,]+) Hotels", p.get_text(" ", strip=True))
            for p in soup.select("p")
            if p.get_text(" ", strip=True).startswith("Showing ")
        ),
        None,
    )
    if counter is None:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott destination range missing")
    start, end, total = (int(value.replace(",", "")) for value in counter.groups())
    if not 1 <= start <= end <= total:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott destination range invalid")
    hotels = {}
    for anchor in soup.select("h2.property-card__title a[href]"):
        url = urlparse(urljoin(ORIGIN, anchor["href"]))
        match = PROPERTY_PATH.fullmatch(url.path)
        name = anchor.get_text(" ", strip=True)
        if (
            url.scheme != "https"
            or url.netloc != "www.marriott.com"
            or url.query
            or url.fragment
            or not match
            or not name
        ):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott property card identity unreadable")
        code = match[1].upper()
        if code in hotels:
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott directory contains duplicate hotel codes"
            )
        hotels[code] = HotelData(
            provider="marriott", provider_hotel_id=code, hotel_name=name, official_url=url.geturl()
        )
    if len(hotels) != end - start + 1:
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "Marriott directory hotel count does not match visible range"
        )
    next_cell = soup.select_one('[data-testid="pagination-next"]')
    child = next_cell.select_one("a[href]") if next_cell else None
    children = []
    if end < total:
        if not child or "disabled" in next_cell.get("class", []):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott directory continuation missing")
        continuation = directory_url(urljoin(final, child["href"]))
        current, following = map(urlparse, (final, continuation))

        def number(url):
            return int(parse_qs(url.query).get("pg", ["1"])[0])

        if current.path != following.path or number(following) != number(current) + 1:
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott directory continuation changed destination or page"
            )
        children.append(continuation)
    elif child is not None:
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "Marriott final directory page has unexpected continuation"
        )
    return CatalogPageData(
        provider="marriott",
        source_url=source,
        directory_urls=children,
        hotels=list(hotels.values()),
        reported_total=total,
        complete=start == 1 and end == total,
        page_complete=True,
    )
