"""GHA public booking-page adapter. No private API or booking submission."""

import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider, name_key
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.ghadiscovery.com"
MONEY = re.compile(r"^([A-Z]{3})\s+((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)(?:\s*/\s*night)?$")
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()


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


def public_room(html: str) -> tuple[str, str, Decimal]:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one("h5.px-5")
    label = soup.find("span", string="NON-MEMBER RATES")
    price = label.parent.find("h5") if label else None
    money = MONEY.fullmatch(price.get_text(" ", strip=True)) if price else None
    if not heading or not money or "Including taxes and fees" not in soup.get_text(" ", strip=True):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "GHA room identity or public inclusive price missing")
    return heading.get_text(" ", strip=True), money[1], Decimal(money[2].replace(",", ""))


def parse_public_offer(html: str, room: tuple[str, str, Decimal], search: RateRequest) -> RateData | None:
    """Only accept a named plan matching the explicitly labelled non-member room quote."""
    soup = BeautifulSoup(html, "html.parser")
    headings = soup.select("h5")
    if not headings:
        return None
    name = headings[0].get_text(" ", strip=True)
    if "DISCOVERY" in name.upper():
        return None
    prices = [h for h in headings if "/" in h.get_text() and "text-gold" not in h.get("class", [])]
    for price in prices:
        money = MONEY.fullmatch(price.get_text(" ", strip=True))
        if not money or "Including taxes and fees" not in price.parent.get_text(" ", strip=True):
            continue
        amount = Decimal(money[2].replace(",", ""))
        if amount <= 0 or (money[1], amount) != room[1:]:
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
    # Reuse only browser lifecycle; all provider operations below are GHA-specific.
    async def _open_booking(self, search: RateRequest):
        if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
            raise ProviderError(
                ErrorCode.NOT_IMPLEMENTED, "GHA browser quotes require one room and one night"
            )
        page = await self._get_page()
        response = await page.goto(booking_url(search), wait_until="domcontentloaded", timeout=45000)
        if response and response.status >= 400:
            code = (
                ErrorCode.BLOCKED_BY_ANTIBOT
                if response.status == 403
                else ErrorCode.RATE_LIMITED
                if response.status == 429
                else ErrorCode.HTTP_ERROR
            )
            raise ProviderError(code, f"GHA HTTP {response.status}")
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
        return [await self.get_hotel_details(str(query.get("provider_hotel_id", "")))]

    async def search_rates(self, search: RateRequest) -> list[RateData]:
        try:
            page = await self._open_booking(search)
            await page.locator(".tid-viewRates").first.wait_for()
            checkbox = page.get_by_role("checkbox")
            if not await checkbox.is_checked():
                await page.locator("label").filter(has=checkbox).click()
            await page.get_by_text("Including taxes and fees", exact=True).first.wait_for()
            buttons = page.locator(".tid-viewRates")
            result = {}
            for index in range(await buttons.count()):
                button = buttons.nth(index)
                html = await button.evaluate(
                    'e=>{let p=e; while(p){if(p.querySelector("h5.px-5")) return p.outerHTML;p=p.parentElement;}return ""}'
                )
                room = public_room(html)
                await button.click()
                await page.locator(".tid-selectBtn:visible").first.wait_for()
                more = page.locator(".tid-viewMoreRates:visible")
                for _ in range(10):
                    if not await more.count():
                        break
                    before = await page.locator(".tid-selectBtn:visible").count()
                    await more.click()
                    await page.wait_for_function(
                        '(n)=>document.querySelectorAll(".tid-selectBtn").length>n*2',
                        arg=before,
                        timeout=5000,
                    )
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
