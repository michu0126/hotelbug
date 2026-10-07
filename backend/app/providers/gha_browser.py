"""GHA public booking-page adapter. No private API or booking submission."""

import asyncio
import hashlib
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from math import ceil
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider, name_key
from app.providers.gha_catalog import hotel_candidate_url
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.ghadiscovery.com"
MONEY = re.compile(r"^([A-Z]{3})\s+((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:\s*/\s*night)?$")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()
NO_AVAILABILITY = "Selection not available for these dates."


def hotel_code(code: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,9}", code):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA requires the numeric public booking hotelId")
    return code


def booking_url(search: RateRequest) -> str:
    return (
        ROOT
        + "/booking/select_room?"
        + urlencode(
            {
                "hotelId": hotel_code(search.provider_hotel_id),
                "clearBookingParams": 1,
                "startDate": search.check_in.isoformat(),
                "endDate": search.check_out.isoformat(),
                "room1Adults": search.adults,
                "room1Children": 0,
                "from": "hotel",
            }
        )
    )


def verify_stay(html: str, url: str, search: RateRequest) -> None:
    parsed = urlparse(url)
    expected = parse_qs(urlparse(booking_url(search)).query)
    actual = parse_qs(parsed.query)
    if (
        parsed.hostname != "www.ghadiscovery.com"
        or parsed.path != "/booking/select_room"
        or any(
            actual.get(key) != expected[key]
            for key in ("hotelId", "startDate", "endDate", "room1Adults", "room1Children")
        )
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA booking hotel or stay URL mismatch")
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True)

    def label(day):
        return f"{WEEKDAYS[day.weekday()]}, {day.day:02d} {MONTHS[day.month - 1]}"

    start = label(search.check_in)
    if search.check_in.year != search.check_out.year:
        start += f" {search.check_in.year}"
    dates = f"{start} - {label(search.check_out)} {search.check_out.year}"
    if dates not in text or not re.search(rf"ROOM 1:\s*{search.adults} ADULTS?\b", text):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA displayed dates or occupancy mismatch")


def public_room(html: str, *, allow_member_identity: bool = False) -> tuple[str, str, Decimal]:
    """Read room/currency identity; the card's FROM amount is never stored as a rate."""
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h5.px-5")
    label = soup.find("span", string="NON-MEMBER RATES")
    if label is None:
        # Some Capella room cards have only FROM, even beside room cards with
        # separate member/non-member columns. Eligibility is checked on named
        # plans after opening View Rates, not inferred from this summary.
        label = soup.find("span", string="FROM")
    if label is None and allow_member_identity:
        # Actual Amalfi extra-bed cards advertise only a member summary. Its
        # amount is not a public quote; named plans still enforce eligibility.
        label = soup.find("span", string="MEMBER RATES FROM")
    price = label.parent.find("h5") if label else None
    money = MONEY.fullmatch(price.get_text(" ", strip=True)) if price else None
    if not heading or not money or "Including taxes and fees" not in soup.get_text(" ", strip=True):
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED, "GHA room identity or inclusive summary price missing"
        )
    return heading.get_text(" ", strip=True), money[1], Decimal(money[2].replace(",", ""))


def no_availability(html: str) -> bool:
    return NO_AVAILABILITY in BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def public_detail_location(html: str) -> dict[str, str | None] | None:
    """Actual public hotel header address/city-country slots, not menu labels."""
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.select('h1[class*="HotelPage_hotelName__"]')
    if len(headings) != 1:
        return None
    heading = headings[0]
    name = heading.get_text(" ", strip=True)
    if not name:
        return None
    result = {"hotel_name": name, "address": None, "city": None, "country": None}
    block = heading.find_next_sibling("div")
    if not block or not {"flex", "flex-col"}.issubset(block.get("class", [])):
        return result
    slots = block.find_all("div", recursive=False)
    # The observed desktop header has street, city-country and Tel./View Map
    # slots. If this changes, retain unknown fields rather than infer a location.
    if len(slots) != 3 or "View Map" not in slots[2].get_text(" ", strip=True):
        return result
    address = " ".join(slots[0].get_text(" ", strip=True).split())
    result["address"] = address or None
    label = " ".join(slots[1].get_text(" ", strip=True).split())
    parts = [part.strip() for part in label.split(",")]
    # Actual Nice header: "06 000, Nice, France". Remove only the
    # observed leading numeric postal slot, not an arbitrary region label.
    if len(parts) == 3 and re.fullmatch(r"[0-9]{4,10}", parts[0].replace(" ", "")):
        parts = parts[1:]
    if len(parts) == 2:
        city, country = parts
        if city and country and not any(character.isdigit() for character in country):
            result.update(city=city, country=country)
    return result


def parse_public_offer(html: str, room: tuple[str, str, Decimal], search: RateRequest) -> RateData | None:
    """Read only named non-member plans; never turn a room-card summary into a quote."""
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.select("h5")
    if not headings:
        return None
    name = headings[0].get_text(" ", strip=True)
    upper_name = name.upper()
    if "DISCOVERY" in upper_name or ("MEMBER" in upper_name and "NON-MEMBER" not in upper_name):
        return None
    prices = [h for h in headings if "/" in h.get_text() and "text-gold" not in h.get("class", [])]
    for price in prices:
        money = MONEY.fullmatch(price.get_text(" ", strip=True))
        if not money or "Including taxes and fees" not in price.parent.get_text(" ", strip=True):
            continue
        amount = Decimal(money[2].replace(",", ""))
        # The room-card "from" price is only a summary. A bookable named
        # non-member plan may differ in either direction, including a bug price.
        if amount <= 0 or money[1] != room[1]:
            continue
        return RateData(
            **search.model_dump(),
            provider="gha",
            room_type=room[0],
            room_code="name:" + name_key(room[0]),
            rate_name=name,
            rate_code="public-name:" + name_key(name),
            total_price=amount,
            currency=money[1],
            member_rate=False,
            availability=True,
            price_basis="nightly",
            source_url=booking_url(search),
            raw_response_hash=hashlib.sha256(html.encode()).hexdigest(),
        )
    return None


class GHABrowserProvider(AccorBrowserProvider):
    session_provider = "gha"

    async def _check_public_response(self, response):
        if response is None or response.status < 400:
            return
        status = response.status
        retry_after = None
        if status == 429:
            value = (await response.header_value("retry-after") or "").strip()
            if value.isdecimal() and len(value) <= 9:
                retry_after = int(value)
            else:
                try:
                    until = parsedate_to_datetime(value)
                    if until.tzinfo is not None:
                        retry_after = max(0, ceil((until - datetime.now(timezone.utc)).total_seconds()))
                except (ValueError, TypeError, OverflowError):
                    pass
        code = (
            ErrorCode.BLOCKED_BY_ANTIBOT
            if status in (401, 403)
            else ErrorCode.RATE_LIMITED
            if status == 429
            else ErrorCode.HTTP_ERROR
        )
        raise ProviderError(code, f"GHA HTTP {status}", retry_after=retry_after)

    async def _expand_rate_pages(self, page):
        """Follow all displayed pagination controls; mobile duplicates are not progress."""
        more = page.locator(".tid-viewMoreRates:visible")
        try:
            async with asyncio.timeout(45):
                while await more.count():
                    before = await page.locator(".tid-selectBtn:visible").count()
                    await more.click()
                    await page.wait_for_function(
                        """before => Array.from(document.querySelectorAll('.tid-selectBtn'))
                            .filter(element => !!element.getClientRects().length).length > before""",
                        arg=before,
                        timeout=10000,
                    )
        except TimeoutError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT, "GHA additional rate plans did not finish loading"
            ) from exc

    # Reuse only browser lifecycle; all provider operations below are GHA-specific.
    async def _open_booking(self, search: RateRequest):
        if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
            raise ProviderError(
                ErrorCode.NOT_IMPLEMENTED, "GHA browser quotes require one room and one night"
            )
        page = await self._get_page()
        response = await page.goto(booking_url(search), wait_until="domcontentloaded", timeout=45000)
        await self._check_public_response(response)
        await page.locator(".tid-editStay").wait_for(state="attached")
        verify_stay(await page.content(), page.url, search)
        return page

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        day = date.today() + timedelta(days=10)
        try:
            page = await self._open_booking(
                RateRequest(provider_hotel_id=hotel_id, check_in=day, check_out=day + timedelta(days=1))
            )
            soup = BeautifulSoup(await page.content(), "html.parser")
            heading = soup.select_one("a[href] > h3")
            if not heading:
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "GHA hotel heading missing")
            link = urljoin(ROOT, heading.parent["href"])
            if urlparse(link).hostname != "www.ghadiscovery.com":
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA hotel identity link off-site")
            return HotelData(
                provider="gha",
                provider_hotel_id=hotel_code(hotel_id),
                hotel_name=heading.get_text(" ", strip=True),
                official_url=link,
            )
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "GHA detail page failed",
            ) from exc

    async def search_hotels(self, query: dict) -> list[HotelData]:
        if query.get("official_url"):
            url = urlparse(query["official_url"])
            if hotel_candidate_url(url.geturl()) != url.geturl().rstrip("/"):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA catalog URL invalid")
            try:
                page = await self._get_page()
                response = await page.goto(url.geturl(), wait_until="domcontentloaded", timeout=45000)
                await self._check_public_response(response)
                link = page.locator('a[href*="/booking/select_room?"]').first
                await link.wait_for(state="attached", timeout=15000)
                target = urlparse(urljoin(ROOT, await link.get_attribute("href")))
                if target.hostname != "www.ghadiscovery.com":
                    raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA booking link off-site")
                code = parse_qs(target.query).get("hotelId", [""])[0]
                location = public_detail_location(await page.content())
                observed_url = urlparse(page.url)
                details = await self.get_hotel_details(hotel_code(code))
                verified_url = urlparse(str(details.official_url))
                if (
                    location
                    and " ".join(location["hotel_name"].casefold().split())
                    == " ".join(details.hotel_name.casefold().split())
                    and observed_url.hostname == verified_url.hostname == "www.ghadiscovery.com"
                    and observed_url.path.rstrip("/") == verified_url.path.rstrip("/")
                ):
                    details = details.model_copy(
                        update={
                            key: value
                            for key, value in location.items()
                            if key != "hotel_name" and value is not None
                        }
                    )
                return [details]
            except BrowserError as exc:
                raise ProviderError(
                    ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                    "GHA catalog detail failed",
                ) from exc
        return [await self.get_hotel_details(str(query.get("provider_hotel_id", "")))]

    async def search_rates(self, search: RateRequest) -> list[RateData]:
        try:
            page = await self._open_booking(search)
            await page.wait_for_function(
                "() => document.querySelector('.tid-viewRates') || "
                "document.body.innerText.includes('Selection not available for these dates.')",
                timeout=30000,
            )
            if not await page.locator(".tid-viewRates").count():
                if no_availability(await page.content()):
                    return []
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "GHA room availability not readable")
            checkbox = page.get_by_role("checkbox")
            if not await checkbox.is_checked():
                await page.locator("label").filter(has=checkbox).click()
            await page.get_by_text("Including taxes and fees", exact=True).first.wait_for()
            # Sold-out room cards can still render a disabled View Rates button.
            # Do not let one unavailable room abort quotes from available rooms.
            buttons = page.locator(".tid-viewRates:not([disabled])")
            if not await buttons.count():
                return []
            result = {}
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                html = await button.evaluate(
                    'e=>{let p=e; while(p){if(p.querySelector("h5.px-5")) return p.outerHTML;p=p.parentElement;}return ""}'
                )
                room = public_room(html, allow_member_identity=True)
                await button.click()
                await page.locator(".tid-selectBtn:visible").first.wait_for()
                await self._expand_rate_pages(page)
                cards = await page.locator(".tid-selectBtn:visible").evaluate_all(
                    "els=>els.map(e=>e.parentElement.parentElement.parentElement.outerHTML)"
                )
                verify_stay(await page.content(), page.url, search)
                for card in cards:
                    rate = parse_public_offer(card, room, search)
                    if rate:
                        result[rate.offer_key()] = rate
                await page.locator(".react-responsive-modal-closeButton:visible").click()
            if not result:
                raise ProviderError(
                    ErrorCode.PROVIDER_CHANGED, "No named GHA non-member tax-inclusive offers verified"
                )
            return list(result.values())
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "GHA room page failed",
            ) from exc

    async def get_calendar_rates(self, search: RateRequest) -> list[RateData]:
        return await self.search_rates(search)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="10624", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(status="ONLINE" if rates else "DEGRADED", message="GHA public-page quotes")
