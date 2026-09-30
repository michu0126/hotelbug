"""Accor public website form and rendered public, tax-inclusive room prices."""

import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.base import HotelProvider
from app.providers.browser_session import new_provider_page
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://all.accor.com"
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def hotel_code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{4}", value):
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "Accor hotel code must be four alphanumeric characters"
        )
    return value.upper()


def name_key(value: str) -> str:
    return hashlib.sha256(" ".join(value.lower().split()).encode()).hexdigest()[:24]


def parse_public_rates(html: str, request: RateRequest, page_url: str) -> list[RateData]:
    code = hotel_code(request.provider_hotel_id)
    url = urlparse(page_url)
    if url.hostname != "all.accor.com" or url.path.rstrip("/") != f"/booking/en/accor/hotel/{code}":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor booking hotel does not match request")
    if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor page adapter supports one room, one night")
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    for day in (request.check_in, request.check_out):
        if f"{MONTHS[day.month - 1]} {day.day:02d}, {day.year}" not in text:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor displayed dates do not match request")
    currencies = []
    for button in soup.select("button"):
        match = re.fullmatch(r"([A-Z]{3})\s*\([^)]*\)", button.get_text(" ", strip=True))
        if match:
            currencies.append(match.group(1))
    if len(set(currencies)) != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor selected currency is missing or ambiguous")
    digest = hashlib.sha256(html.encode()).hexdigest()
    rates = {}
    for label in soup.select("label.hotel-accommodation-offers-content__label"):
        public = next(
            (
                p
                for p in label.select(".offer-price")
                if p.get_text(" ", strip=True).startswith("Public rate")
            ),
            None,
        )
        if public is None:
            continue
        amount = public.select_one(".price-display-amount")
        title = label.select_one(".ads-radio__label")
        details = label.select_one(".stay-details")
        if amount is None or title is None or details is None:
            continue
        terms = details.get_text(" ", strip=True)
        if "Taxes and fees included." not in terms or "1 night" not in terms:
            continue
        if not re.search(rf"\b{request.adults} adults?\b", terms):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor guest count does not match request")
        money = re.fullmatch(
            r"[^\d]*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)\s*(?:[A-Z]{3})?", amount.get_text(" ", strip=True)
        )
        if not money:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Accor public price format changed")
        parent = label.parent
        heading = None
        while parent and parent.name != "body":
            headings = parent.select(".hotel-accommodations-offers__item-title")
            if len(headings) == 1:
                heading = headings[0]
                break
            if len(headings) > 1:
                break
            parent = parent.parent
        if heading is None:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Accor room identity missing")
        room_name, rate_name = heading.get_text(" ", strip=True), title.get_text(" ", strip=True)
        policy = label.get_text(" ", strip=True)
        rate = RateData(
            **request.model_dump(exclude={"provider_hotel_id"}),
            provider="accor",
            provider_hotel_id=code,
            room_type=room_name,
            room_code="name:" + name_key(room_name),
            rate_name=rate_name + " · Public rate",
            rate_code="public-name:" + name_key(rate_name),
            total_price=Decimal(money.group(1).replace(",", "")),
            currency=currencies[0],
            member_rate=False,
            availability=True,
            price_basis="stay_total",
            refundable=False
            if "Non-refundable" in policy
            else True
            if "Cancel free of charge" in policy
            else None,
            breakfast_included=True if "Breakfast included" in policy else None,
            source_url=ROOT + url.path,
            raw_response_hash=digest,
        )
        if rate.total_price <= 0:
            continue
        rates[rate.offer_key()] = rate
    if not rates:
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED, "No verified Accor public tax-inclusive offers on page"
        )
    return list(rates.values())


class AccorBrowserProvider(HotelProvider):
    session_provider = "accor"

    def __init__(
        self,
        proxy_server: str | None = None,
        browser_channel: str | None = None,
        session_dir: str | None = None,
    ):
        self.proxy_server = proxy_server
        self.browser_channel = browser_channel
        self.session_dir = session_dir
        self._page = None

    async def _get_page(self):
        if self._page is None:
            self._page = await new_provider_page(
                self.session_provider,
                proxy_server=self.proxy_server,
                browser_channel=self.browser_channel,
                session_dir=self.session_dir,
            )
            self._page.set_default_timeout(30000)

            # Consent can appear after the form becomes usable, especially on the
            # second task in a persistent session. Handle it before blocked actions.
            async def dismiss_consent(locator):
                await locator.click()

            await self._page.add_locator_handler(
                self._page.get_by_text("Continue without Accepting", exact=False).first,
                dismiss_consent,
            )
        return self._page

    async def _open(self, code: str):
        page = await self._get_page()
        response = await page.goto(
            ROOT + f"/hotel/{hotel_code(code)}/index.en.shtml", wait_until="domcontentloaded", timeout=45000
        )
        if response and response.status in (403, 429):
            raise ProviderError(
                ErrorCode.BLOCKED_BY_ANTIBOT if response.status == 403 else ErrorCode.RATE_LIMITED,
                "Accor access rejected",
            )
        if response and response.status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"Accor HTTP {response.status}")
        await page.locator("#booking-date-in.hasDatepicker").wait_for(state="visible")
        return page

    async def search_hotels(self, query: dict) -> list[HotelData]:
        if not query.get("provider_hotel_id"):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor details require a catalog hotel code")
        return [await self.get_hotel_details(query["provider_hotel_id"])]

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        try:
            page = await self._open(hotel_id)
            name = (await page.locator("h1").first.inner_text()).strip()
            name = re.sub(r"\s+\d(?:\.\d)?\s+stars?$", "", name, flags=re.IGNORECASE)
            return HotelData(
                provider="accor",
                provider_hotel_id=hotel_code(hotel_id),
                hotel_name=name,
                official_url=ROOT + f"/hotel/{hotel_code(hotel_id)}/index.en.shtml",
            )
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Accor hotel page failed",
            ) from exc

    async def search_rates(self, request: RateRequest) -> list[RateData]:
        if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Accor page adapter supports one room, one night")
        stage = "hotel page"
        try:
            page = await self._open(request.provider_hotel_id)
            stage = "stay dates"
            await page.locator('[name="search.dateIn"]').fill(request.check_in.strftime("%d/%m/%Y"))
            await page.locator('[name="search.dateIn"]').press("Tab")
            await page.locator('[name="search.dateOut"]').fill(request.check_out.strftime("%d/%m/%Y"))
            await page.locator('[name="search.dateOut"]').press("Tab")
            if request.adults != 1:
                stage = "guest count"
                await page.locator("button").filter(has_text="1 room, 1 adult").click()
                for _ in range(request.adults - 1):
                    await page.get_by_role("button", name="Add an adult", exact=True).click()
                await page.locator('[name="search.dateIn"]').click()
                await page.locator('[name="search.dateIn"]').press("Escape")
            form = page.locator("form").filter(has=page.locator('[name="search.dateIn"]'))
            stage = "search submission"
            await form.get_by_role("button", name="See rates", exact=True).click()
            stage = "booking navigation"
            await page.wait_for_url("**/booking/**")
            stage = "public offers"
            await page.locator(
                "label.hotel-accommodation-offers-content__label .offer-price--alternative"
            ).first.wait_for(state="visible", timeout=45000)
            return parse_public_rates(await page.content(), request, page.url)
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                f"Accor booking page failed at {stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="0338", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(status="ONLINE" if rates else "DEGRADED", message="Accor rendered public offers")

    async def close(self):
        if self._page:
            await self._page.close()
            self._page = None
