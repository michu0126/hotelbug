"""Marriott public-page adapter. It reads rendered booking pages, not private APIs.

This is deliberately a small pilot: one room, one night, and publicly bookable
non-member cash rates. A missing price is not a zero-price offer.
"""

import base64
import binascii
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider
from app.providers.browser_session import new_provider_page
from app.providers.marriott import hotel_code
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.marriott.com"
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PRICE = re.compile(r"^([0-9][0-9,]*(?:\.[0-9]{1,2})?)$")
CURRENCY = re.compile(r"\b([A-Z]{3})\s*/\s*Night\b")
GUESTS = re.compile(r"1 Room,\s*(\d+) Guests?\b", re.IGNORECASE)


def day_label(day: date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day:02d} {day.year}"


def guest_count(label: str) -> int:
    match = GUESTS.search(label)
    if not match:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott guest count is not readable")
    return int(match.group(1))


def parse_hotel_page(html: str, code: str) -> HotelData:
    code = hotel_code(code)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(f'a[href^="/hotels/travel/{code.lower()}-"] h3.hotel-name')
    link = heading.parent if heading else None
    if link is None or not heading.get_text(" ", strip=True):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott hotel identity missing from booking page")
    href = urljoin(ROOT, link["href"])
    if urlparse(href).hostname != "www.marriott.com":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Unexpected Marriott hotel URL")
    return HotelData(
        provider="marriott",
        provider_hotel_id=code,
        hotel_name=heading.get_text(" ", strip=True),
        official_url=href,
    )


def _product_identity(href: str) -> list[str]:
    encoded = parse_qs(urlparse(href).query).get("productId", [""])[0]
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode().split("|")
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Invalid Marriott room identity") from exc


def parse_room_page(html: str, search: RateRequest) -> list[RateData]:
    """Parse one expanded room card, rejecting wrong hotel or stay dates."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h3")
    details = soup.select_one('a[href*="roomPoolCode="]')
    if heading is None or details is None:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott room heading or identity missing")
    params = parse_qs(urlparse(details["href"]).query)
    code = params.get("marshaCode", [""])[0]
    room_code = params.get("roomPoolCode", [""])[0]
    if code != hotel_code(search.provider_hotel_id) or not room_code:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott room hotel mismatch")
    identity = _product_identity(details["href"])
    if (
        len(identity) < 5
        or identity[0] != code
        or identity[2].lower() != room_code.lower()
        or identity[3:5] != [search.check_in.isoformat(), search.check_out.isoformat()]
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott room dates or code mismatch")
    digest = hashlib.sha256(html.encode()).hexdigest()
    result = []
    for card in soup.select(".rate-card-content"):
        title = card.select_one("h4.title-name")
        if title is None or "Taxes and all fees included" not in card.get_text(" ", strip=True):
            continue
        for offer in card.select('[data-testid="ratedetails"]'):
            name = offer.select_one(".rate-name")
            amount = offer.select_one('div.price > span[aria-hidden="false"]:not(.d-none)')
            units = offer.select_one(".avg-per-night")
            rule = offer.select_one('a[href*="rateProgramCode="]')
            if not all((name, amount, units, rule)):
                continue
            rate_type = name.get_text(" ", strip=True)
            if rate_type != "Non-Member Rate":
                continue  # Other rate types may require membership or eligibility.
            value = amount.get_text(" ", strip=True)
            currency = CURRENCY.search(units.get_text(" ", strip=True))
            if not PRICE.fullmatch(value) or not currency:
                continue
            rate_code = parse_qs(urlparse(rule["href"]).query).get("rateProgramCode", [""])[0]
            if not rate_code:
                continue
            result.append(
                RateData(
                    **search.model_dump(exclude={"provider_hotel_id"}),
                    provider="marriott",
                    provider_hotel_id=code,
                    room_type=heading.get_text(" ", strip=True),
                    room_code=room_code,
                    rate_name=f"{title.get_text(' ', strip=True)} · {rate_type}",
                    rate_code=rate_code,
                    total_price=Decimal(value.replace(",", "")),
                    currency=currency.group(1),
                    member_rate=False,
                    availability=True,
                    price_basis="stay_total",
                    source_url=ROOT + "/reservation/rateListMenu.mi",
                    raw_response_hash=digest,
                )
            )
    return result


class MarriottBrowserProvider(HotelProvider):
    def __init__(
        self,
        hotel_name: str | None = None,
        browser_channel: str | None = None,
        headless: bool = True,
        proxy_server: str | None = None,
        session_dir: str | None = None,
    ):
        self._hotel_name = hotel_name
        self._browser_channel = browser_channel
        self._headless = headless
        self._proxy_server = proxy_server
        self._session_dir = session_dir
        self._page: Page | None = None

    def configure_hotel(self, hotel_name: str) -> None:
        self._hotel_name = hotel_name

    async def _get_page(self) -> Page:
        if self._page is None:
            self._page = await new_provider_page(
                "marriott",
                proxy_server=self._proxy_server,
                browser_channel=self._browser_channel,
                headless=self._headless,
                session_dir=self._session_dir,
            )
            self._page.set_default_timeout(15000)
        return self._page

    @staticmethod
    async def _check_access(page: Page, status: int | None) -> None:
        if status == 403:
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott returned HTTP 403")
        if status == 429:
            raise ProviderError(ErrorCode.RATE_LIMITED, "Marriott returned HTTP 429")
        if status is not None and status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"Marriott returned HTTP {status}")
        body = (await page.locator("body").inner_text()).lower()
        if any(marker in body for marker in ("verify you are human", "captcha challenge", "access denied")):
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Marriott challenge page")

    async def _open_hotel(self, code: str, hotel_name: str | None = None) -> Page:
        code = hotel_code(code)
        name = hotel_name or self._hotel_name
        if not name:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Marriott homepage search needs a hotel name")
        page = await self._get_page()
        response = await page.goto(ROOT + "/default.mi", wait_until="domcontentloaded", timeout=30000)
        await self._check_access(page, response.status if response else None)
        await page.get_by_role("textbox", name="Destination").fill(name)
        suggestion = page.locator('[id^="downshift-0-item-"]').filter(has_text=name)
        try:
            await suggestion.first.click(timeout=15000)
        except PlaywrightTimeout:
            await self._check_access(page, None)
            raise
        await page.get_by_role("button", name="Find Hotels").click()
        card = page.locator(".property-card-container").filter(
            has=page.get_by_role("button", name=name, exact=True)
        )
        try:
            await card.get_by_role("link", name="View Rates").click(timeout=30000)
        except PlaywrightTimeout:
            await self._check_access(page, None)
            raise
        try:
            await page.get_by_role("heading", name="Select a Room and Rate").wait_for(timeout=30000)
        except PlaywrightTimeout:
            await self._check_access(page, None)
            raise
        if urlparse(page.url).hostname != "www.marriott.com":
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott booking page redirected off site")
        await self._check_access(page, None)
        parse_hotel_page(await page.content(), code)
        return page

    async def _choose_day(self, page: Page, day: date) -> None:
        cell = page.get_by_role("gridcell", name=day_label(day))
        for _ in range(14):
            if await cell.count():
                await cell.click()
                return
            await page.get_by_role("button", name="Next Month").click()
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott date is outside the displayed calendar")

    async def _set_search(self, page: Page, search: RateRequest) -> None:
        if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
            raise ProviderError(
                ErrorCode.NOT_IMPLEMENTED, "Browser quotes currently require one room and one night"
            )
        guests = page.get_by_role("button", name=re.compile(r"ROOMS & GUESTS|1 Room, \d+ Guests?"))
        current_adults = guest_count(await guests.first.inner_text())
        await guests.first.click()
        for _ in range(max(0, search.adults - current_adults)):
            await page.get_by_role("button", name="Add Adults").click()
        for _ in range(max(0, current_adults - search.adults)):
            await page.get_by_role("button", name="Remove Adults").click()
        await page.get_by_role("button", name="Update", exact=True).click()
        await page.get_by_role("button", name=re.compile(rf"1 Room, {search.adults} Guests?")).wait_for(
            timeout=30000
        )
        await page.get_by_role("heading", name="Select a Room and Rate").wait_for(timeout=30000)
        await page.get_by_role("button", name=re.compile(r"STAY DATES")).first.click()
        await self._choose_day(page, search.check_in)
        await self._choose_day(page, search.check_out)
        await page.get_by_role("button", name="Update", exact=True).click()
        await page.get_by_role("heading", name="Select a Room and Rate").wait_for(timeout=30000)
        dates = await page.get_by_role("button", name=re.compile(r"STAY DATES")).first.inner_text()
        for day in (search.check_in, search.check_out):
            if f"{WEEKDAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day:02d}" not in dates:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott page shows unexpected stay dates")
        # The form label updates before the asynchronously rendered room cards.
        for _ in range(40):
            href = (
                await page.locator('a[href*="roomPoolCode="]').first.get_attribute("href")
                if await page.locator('a[href*="roomPoolCode="]').count()
                else None
            )
            if href:
                identity = _product_identity(href)
                if identity[3:5] == [search.check_in.isoformat(), search.check_out.isoformat()]:
                    return
            await page.wait_for_timeout(500)
        if await page.locator('a[href*="roomPoolCode="]').count():
            raise ProviderError(ErrorCode.TIMEOUT, "Marriott room results did not update for selected dates")
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED,
            "Marriott room results are missing; no availability has not been confirmed",
        )

    async def _collect_rates(self, page: Page, search: RateRequest) -> list[RateData]:
        if not await page.locator('a[href*="roomPoolCode="]').count():
            raise ProviderError(
                ErrorCode.PROVIDER_CHANGED,
                "Marriott room results are missing; no availability has not been confirmed",
            )
        await page.get_by_role("checkbox", name="Show with taxes and fees").check()
        rooms = page.locator("div.drop-shadow").filter(has=page.locator('a[href*="roomPoolCode="]'))
        result = []
        for index in range(await rooms.count()):
            room = rooms.nth(index)
            button = room.get_by_role("button", name="View Rates")
            if await button.count():
                await button.click()
            await room.locator(".rate-card-content").first.wait_for(timeout=15000)
            result.extend(parse_room_page(await room.inner_html(), search))
        if not result:
            raise ProviderError(
                ErrorCode.PROVIDER_CHANGED,
                "Marriott page contains no verifiable public tax-inclusive quotes",
            )
        return result

    async def search_hotels(self, query: dict) -> list[HotelData]:
        code = query.get("provider_hotel_id")
        name = query.get("hotel_name")
        if not isinstance(code, str) or not isinstance(name, str):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Discovery requires hotel code and name")
        try:
            page = await self._open_hotel(code, name)
            return [parse_hotel_page(await page.content(), code)]
        except PlaywrightTimeout as exc:
            if self._page:
                await self._check_access(self._page, None)
            raise ProviderError(ErrorCode.TIMEOUT, "Marriott booking page timed out") from exc
        except PlaywrightError as exc:
            raise ProviderError(ErrorCode.NETWORK_ERROR, "Marriott browser navigation failed") from exc

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        try:
            page = await self._open_hotel(hotel_id)
            return parse_hotel_page(await page.content(), hotel_id)
        except PlaywrightTimeout as exc:
            if self._page:
                await self._check_access(self._page, None)
            raise ProviderError(ErrorCode.TIMEOUT, "Marriott booking page timed out") from exc
        except PlaywrightError as exc:
            raise ProviderError(ErrorCode.NETWORK_ERROR, "Marriott browser navigation failed") from exc

    async def search_rates(self, search: RateRequest) -> list[RateData]:
        try:
            page = await self._open_hotel(search.provider_hotel_id)
            await self._set_search(page, search)
            return await self._collect_rates(page, search)
        except PlaywrightTimeout as exc:
            if self._page:
                await self._check_access(self._page, None)
            raise ProviderError(ErrorCode.TIMEOUT, "Marriott booking page timed out") from exc
        except PlaywrightError as exc:
            raise ProviderError(ErrorCode.NETWORK_ERROR, "Marriott browser navigation failed") from exc

    async def get_calendar_rates(self, search: RateRequest) -> list[RateData]:
        return await self.search_rates(search)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        previous_name = self._hotel_name
        self._hotel_name = "New York Marriott Marquis"
        try:
            rates = await self.search_rates(
                RateRequest(provider_hotel_id="NYCMQ", check_in=day, check_out=day + timedelta(days=1))
            )
        finally:
            self._hotel_name = previous_name
        return HealthResult(
            status="ONLINE" if rates else "DEGRADED",
            message="Public room quotes" if rates else "No public room quotes",
        )

    async def close(self) -> None:
        if self._page:
            await self._page.close()
            self._page = None
