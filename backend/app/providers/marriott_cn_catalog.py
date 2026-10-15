"""Marriott Chinese public directory URLs observed on 2026-10-08.

The country search is reached from the worldwide hotel directory. Its prices
are not dated room offers and are never emitted as RateData by this module.
"""

import asyncio
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import CatalogPageData, HotelData

ORIGIN = "https://www.marriott.com.cn"
ROOT = ORIGIN + "/hotel-search.mi"
COUNTRY_PATH = "/search/findHotels.mi"
REGIONS = (
    "asia",
    "africa",
    "australia-pi",
    "canada",
    "caribbean",
    "central-america",
    "europe",
    "mexico",
    "middle-east",
    "south-america",
    "united-states",
)
DESTINATION = "destinationAddress.destination"
COUNTRY = "destinationAddress.country"
REGION = "destinationAddress.region"


def directory_url(value: str) -> str:
    """Normalize the observed root and unfiltered country/region-search routes.

    Directory anchors contain filterApplied=false; the resulting browser URL
    contains deviceType=desktop-web instead. Both identify the same directory.
    """
    url = urlparse(value)
    try:
        valid_origin = url.port in (None, 443) and url.username is None and url.password is None
    except ValueError:
        valid_origin = False
    if url.scheme != "https" or url.hostname != "www.marriott.com.cn" or not valid_origin:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory origin invalid")
    if url.fragment and not re.fullmatch(r"/(0|[1-9][0-9]{0,4})/", url.fragment):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory fragment unsupported")
    if url.path == "/hotel-search.mi" and not url.query and url.fragment in ("", "/0/"):
        return ROOT
    params = parse_qs(url.query, keep_blank_values=True)
    scope = COUNTRY if COUNTRY in params else REGION
    required = {DESTINATION, scope, "searchType"}
    if (
        url.path != COUNTRY_PATH
        or not required <= set(params)
        or set(params) - required - {"filterApplied", "deviceType", "view"}
        or any(len(values) != 1 for values in params.values())
        or not params[DESTINATION][0].strip()
        or (scope == COUNTRY and not re.fullmatch(r"[A-Z]{2}", params[scope][0]))
        or (scope == REGION and params[scope][0] not in REGIONS)
        or params["searchType"] != ["InCity"]
        or params.get("deviceType", ["desktop-web"]) != ["desktop-web"]
        or params.get("filterApplied", ["false"]) != ["false"]
        or params.get("view", ["list"]) != ["list"]
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese country directory URL invalid")
    return (
        ORIGIN
        + COUNTRY_PATH
        + "?"
        + urlencode(
            {
                DESTINATION: params[DESTINATION][0],
                scope: params[scope][0],
                "searchType": "InCity",
                "deviceType": "desktop-web",
            }
        )
        + ("#" + url.fragment if url.fragment not in ("", "/0/") else "")
    )


def verify_directory_redirect(source: str, final: str) -> None:
    if directory_url(source) != directory_url(final):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory destination changed")


def verify_country_location(source: str, final: str) -> None:
    """Check country/filter identity; the SPA hash is not a reliable page index.

    Observed 2026-10-14: clicking published page 3 returned offset 80 and
    rendered 81–120, but the URL was #/1/. The verified response offset and
    rendered result range determine the page, never this navigation counter.
    Both complete URLs still pass the official-origin/query/fragment validator.
    """
    verify_directory_redirect(directory_url(source).split("#", 1)[0], directory_url(final).split("#", 1)[0])


def result_range(text: str) -> tuple[int, int, int]:
    """Read the public search counter, not a guessed pagination size."""
    match = re.fullmatch(r"\s*([0-9,]+)\s*-\s*([0-9,]+)\s*/\s*([0-9,]+)\s*结果\s*", text)
    if match is None:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott Chinese result range missing")
    start, end, total = (int(value.replace(",", "")) for value in match.groups())
    if not 1 <= start <= end <= total:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese result range invalid")
    return start, end, total


async def wait_country_range(page, expected_start: int | None = None) -> tuple[int, int, int]:
    """Wait past the initial 1-0/0 shell and stale data after a page click."""
    await page.wait_for_function(
        r"""expected => {
          const text = document.querySelector('.search-summary-container')?.textContent || '';
          const m = text.match(/^\s*([0-9,]+)\s*-\s*([0-9,]+)\s*\/\s*([0-9,]+)\s*结果\s*$/);
          if (!m) return false;
          const [start, end, total] = m.slice(1).map(s => Number(s.replaceAll(',', '')));
          return (expected === null || start === expected) && start >= 1 && end >= start && total >= end;
        }""",
        arg=expected_start,
        timeout=30000,
    )
    return result_range(await page.locator(".search-summary-container").inner_text())


async def navigate_country_page(provider, page, source: str, click_page=None) -> tuple[int, int, int]:
    """Use published numeric pagination; a fresh #/1/ URL resets to page one.

    Choose the visible page label closest to the requested index. In a large
    list this can jump to the published last page rather than visiting every
    intermediate page. Each click must strictly reduce the remaining distance.
    """
    target = int(urlparse(source).fragment.strip("/") or "0")
    start, end, total = await wait_country_range(page)
    if target * 40 >= total:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory page outside results")
    current = (start - 1) // 40
    while current != target:
        labels = await page.locator("a.shop-pagination-item[aria-label]").evaluate_all(
            "nodes => nodes.filter(n => !n.classList.contains('disabled')).map(n => n.getAttribute('aria-label'))"
        )
        choices = [
            int(label) - 1
            for label in labels
            if label
            and re.fullmatch(r"[1-9][0-9]*", label)
            and abs(int(label) - 1 - target) < abs(current - target)
            and (int(label) - 1) * 40 < total
        ]
        if not choices:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott Chinese published page control missing")
        next_index = min(choices, key=lambda index: (abs(index - target), index))
        if click_page is None:
            await page.locator(f'a.shop-pagination-item[aria-label="{next_index + 1}"]').click()
        else:
            await click_page(next_index)
        await provider._check_access(page, None)
        start, end, total = await wait_country_range(page, next_index * 40 + 1)
        verify_country_location(source, page.url)
        current = next_index
    return start, end, total


async def discover_country_response(provider, page, source: str) -> CatalogPageData:
    """Read complete identities from requests issued by normal page navigation.

    Do not replay requests or manufacture API tokens. The page's initial query
    and its actual pagination clicks supply the public request and response.
    """
    from app.providers.marriott_cn_country_response import PATH, parse_country_response

    base = source.split("#", 1)[0]

    async def collect(index, action):
        def matches(response):
            if (
                urlparse(response.url).hostname != "www.marriott.com.cn"
                or urlparse(response.url).path != PATH
            ):
                return False
            try:
                return response.request.post_data_json["variables"]["offset"] == index * 40
            except (KeyError, TypeError, ValueError):
                return False

        async with page.expect_response(matches, timeout=45000) as event:
            await action()
        response = await event.value
        await provider._check_access(page, response.status)
        request = response.request.post_data_json
        body = await asyncio.wait_for(response.json(), timeout=15)
        return parse_country_response(request, body, base + (f"#/{index}/" if index else ""))

    async def open_country():
        response = await page.goto(base, wait_until="commit", timeout=45000)
        await provider._check_access(page, response.status if response else None)

    result = await collect(0, open_country)
    if urlparse(source).fragment:

        async def click_page(index):
            nonlocal result

            async def click():
                await page.locator(f'a.shop-pagination-item[aria-label="{index + 1}"]').click()

            result = await collect(index, click)

        await navigate_country_page(provider, page, source, click_page=click_page)
    offset = int(urlparse(source).fragment.strip("/") or "0") * 40
    actual = await wait_country_range(page, offset + 1)
    if actual != (offset + 1, offset + len(result.hotels), result.reported_total):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott country response and page range disagree")
    await provider._check_access(page, None)
    verify_country_location(source, page.url)
    return result


def parse_country(html: str, source: str) -> CatalogPageData:
    source = directory_url(source)
    soup = BeautifulSoup(html, "html.parser")
    counter = soup.select_one(".search-summary-container")
    if counter is None:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott Chinese result counter missing")
    start, end, total = result_range(counter.get_text(" ", strip=True))
    index = int(urlparse(source).fragment.strip("/") or "0")
    if start != index * 40 + 1 or end != min((index + 1) * 40, total):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory page range mismatch")
    active = {a.get("aria-label") for a in soup.select("a.shop-pagination-item.active")}
    if (active and active != {str(index + 1)}) or (index > 0 and not active):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese directory page index mismatch")
    next_links = soup.select('a[aria-label="NextPage"]:not(.disabled)')
    if bool(next_links) != (end < total):
        # An unmounted/partial list cannot claim a verified continuation.
        pagination_complete = False
    else:
        pagination_complete = True
    country = parse_qs(urlparse(source).query).get(COUNTRY, [None])[0]
    if country == "CN":
        # Observed official CN search sends [CN,TW,MO,HK]. A search scope is
        # not per-hotel address evidence; don't label every result as CN.
        country = None
    hotels = {}
    unparsed = 0
    for card in soup.select('[data-testid="cardContainer-testid"]'):
        title = card.select_one("button.title-container[aria-label]")
        link = card.select_one('a.view-rates-button-container[href*="propertyCode="]')
        if title is None or link is None:
            unparsed += 1
            continue
        href = urljoin(ORIGIN, link["href"])
        # Reuse the actual quote adapter's validation: this link must later be
        # usable by the automatic price job without inventing a hotel slug.
        from app.providers.marriott_browser import MarriottBrowserProvider

        provider = MarriottBrowserProvider()
        try:
            provider.configure_hotel_url(href)
        except ProviderError:
            unparsed += 1
            continue
        name = title.get("aria-label", "").strip()
        if not name or provider._official_code in hotels:
            unparsed += 1
            continue
        hotels[provider._official_code] = HotelData(
            provider="marriott",
            provider_hotel_id=provider._official_code,
            hotel_name=name,
            country=country,
            official_url=href,
        )
    page_complete = len(hotels) == end - start + 1 and not unparsed and pagination_complete
    complete = start == 1 and end == total and page_complete
    return CatalogPageData(
        provider="marriott",
        source_url=source,
        hotels=list(hotels.values()),
        reported_total=total,
        directory_urls=[source.split("#", 1)[0] + f"#/{index + 1}/"] if next_links and end < total else [],
        complete=complete,
        page_complete=page_complete,
        unparsed_hotel_links=unparsed,
    )


def parse_root(html: str, observations: dict[str, str]) -> CatalogPageData:
    soup = BeautifulSoup(html, "html.parser")
    totals = []
    for heading in soup.select('[role="heading"][aria-level="2"]'):
        match = re.fullmatch(r"([0-9,]+)\s*酒店", heading.get_text(" ", strip=True))
        if match:
            totals.append(int(match[1].replace(",", "")))
    if len(totals) != 1 or set(observations) != set(REGIONS):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese worldwide regions incomplete")
    links = set()
    for region, rendered in observations.items():
        current = BeautifulSoup(rendered, "html.parser")
        button = current.select_one(f'button[id="{region}_heading"][aria-expanded="true"]')
        body = current.select_one(f'[id="{region}_body"]')
        title = button.select_one(".region-card-title") if button else None
        if title is None or body is None or "hide-visibility" in body.get("class", []):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese region is not expanded")
        region_name = title.get_text(" ", strip=True)
        region_links = []
        for link in body.select('a[href*="findHotels.mi"]'):
            value = directory_url(urljoin(ORIGIN, link["href"]))
            params = parse_qs(urlparse(value).query)
            # Inactive accordions repeat the last region's country data.
            if COUNTRY in params:
                track = link.get("data-custom_click_track_value", "")
                if f"Region Card: {region_name}:" not in track:
                    raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese region data is stale")
                region_links.append(value)
            elif params.get(REGION) == [region]:
                region_links.append(value)
        if not region_links:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese region has no verified links")
        links.update(region_links)
    return CatalogPageData(
        provider="marriott",
        source_url=ROOT,
        directory_urls=sorted(links),
        reported_total=totals[0],
        complete=False,
        page_complete=True,
    )


async def discover(provider, query: dict) -> CatalogPageData:
    source = directory_url(query.get("official_url", ""))
    page = await provider._get_page()
    if source == ROOT:
        from app.providers.marriott_cn_directory_response import PATH, parse_worldwide_response

        # Let the normal public page issue its own observed directory query.
        # No replayed authentication headers, user cookies or guessed API calls.
        async with page.expect_response(
            lambda response: (
                urlparse(response.url).hostname == "www.marriott.com.cn"
                and urlparse(response.url).path == PATH
            ),
            timeout=45000,
        ) as event:
            response = await page.goto(source, wait_until="commit", timeout=45000)
            await provider._check_access(page, response.status if response else None)
        response = await event.value
        await provider._check_access(page, response.status)
        request = response.request.post_data_json
        body = await asyncio.wait_for(response.json(), timeout=15)
        verify_directory_redirect(source, page.url)
        return parse_worldwide_response(request, body)
    if COUNTRY in parse_qs(urlparse(source).query):
        return await discover_country_response(provider, page, source)
    # The official application ignores a fresh deep-linked pagination hash.
    # Open its country route, then click real published controls to the target.
    response = await page.goto(source.split("#", 1)[0], wait_until="commit", timeout=45000)
    await provider._check_access(page, response.status if response else None)
    counter = page.locator(".search-summary-container")
    await counter.wait_for(timeout=30000)
    start, end, _ = await navigate_country_page(provider, page, source)
    cards = page.locator('[data-testid="cardContainer-testid"]')
    await cards.first.wait_for(timeout=30000)
    while (count := await cards.count()) < end - start + 1:
        await cards.last.scroll_into_view_if_needed()
        await page.wait_for_function(
            "count => document.querySelectorAll('[data-testid=cardContainer-testid]').length > count",
            arg=count,
            timeout=20000,
        )
    result = parse_country(await page.content(), source)
    await provider._check_access(page, None)
    verify_country_location(source, page.url)
    return result
