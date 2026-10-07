"""Hilton official rooms -> public rate grid, with verified rendered stay identity."""

import re
from datetime import date, timedelta
from urllib.parse import urlencode, urlparse

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider
from app.providers.hilton_page import (
    MONTHS,
    parse_public_rates,
    stay_label_expectations,
    verify_displayed_stay,
    verify_rooms_url,
)
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.hilton.com"
ROOM_BUTTONS = '[data-testid="moreRatesButton"], [data-testid="accessibleMoreRatesButton"]'
PROPERTY_PATH = re.compile(r"/en/hotels/([a-z0-9]{7})-[a-z0-9-]+/?")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def hotel_code(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9]{7}", value):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton requires an official seven-character code")
    return value.upper()


def rooms_url(request: RateRequest) -> str:
    return (
        ROOT
        + "/zh-hant/book/reservation/rooms/?"
        + urlencode(
            {
                "ctyhocn": hotel_code(request.provider_hotel_id),
                "arrivalDate": request.check_in.isoformat(),
                "departureDate": request.check_out.isoformat(),
                "room1NumAdults": request.adults,
            }
        )
    )


class HiltonBrowserProvider(AccorBrowserProvider):
    session_provider = "hilton"

    def __init__(self, *args, headless: bool = False, **kwargs):
        super().__init__(*args, headless=headless, **kwargs)
        self.hotel_name = None
        self._journey_pages = []
        self._booking_stage = None

    def configure_hotel(self, name: str):
        self.hotel_name = name.strip()

    async def _install_cookie_handler(self, page):
        async def dismiss_cookie(locator):
            await locator.click()

        await page.add_locator_handler(
            page.get_by_role("button", name="dismiss cookie message", exact=True), dismiss_cookie
        )

    async def _get_page(self):
        created = self._page is None
        page = await super()._get_page()
        if created:
            await self._install_cookie_handler(page)
        return page

    async def _choose_day(self, page, value: date, phase: str):
        label = re.compile(
            rf"^Choose {WEEKDAYS[value.weekday()]}, {MONTHS[value.month - 1]} {value.day}, "
            rf"{value.year} as your Check-{phase} date\.$",
            re.I,
        )
        day = page.get_by_role("button", name=label)
        # The official calendar shows two months. A year's dates require at
        # most twelve forward steps, not a guessed URL/date payload.
        for _ in range(13):
            if await day.count():
                await day.click()
                return
            await page.get_by_role("button", name="Next Month", exact=True).click()
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton date not exposed by calendar")

    async def _set_adults(self, page, adults: int):
        await page.get_by_role("button", name=re.compile("Modify/Change Rooms & Guests")).click()
        dialog = page.get_by_role("dialog")
        add = dialog.get_by_role("button", name="Add adult to room 1", exact=True)
        await add.wait_for(state="visible", timeout=30000)

        # Count is rendered between the normal plus/minus buttons. Do not
        # assume the homepage always defaults to one adult in a saved session.
        async def count():
            value = await add.evaluate(
                "e => e.parentElement.querySelector('[aria-live=polite]')?.textContent.trim()"
            )
            match = re.fullmatch(r"([1-8]) adults?", value or "")
            if not match:
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hilton adult count is not readable")
            return int(match[1])

        current = await count()
        while current != adults:
            button = (
                add
                if current < adults
                else dialog.get_by_role("button", name="Remove adult from room 1", exact=True)
            )
            await button.click()
            next_count = await count()
            if next_count != current + (1 if current < adults else -1):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton adult count did not update")
            current = next_count
        await dialog.get_by_role("button", name="Done", exact=True).click()

    async def _open_via_home(self, page, request: RateRequest):
        self._booking_stage = "home navigation"
        response = await page.goto(ROOT + "/en/", wait_until="commit", timeout=45000)
        await self._check_access(page, response.status if response else None)
        search = page.locator("#location-input")
        try:
            self._booking_stage = "hotel search input"
            await search.wait_for(state="visible", timeout=45000)
            await search.fill(self.hotel_name)
            self._booking_stage = "hotel suggestion"
            exact_hotel = page.get_by_role("option").filter(
                has_text=re.compile(r"^" + re.escape(self.hotel_name) + r"(?:\s|$)")
            )
            await exact_hotel.first.wait_for(state="visible", timeout=45000)
            if await exact_hotel.count() != 1:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton hotel suggestion is ambiguous")
            await exact_hotel.click()
            self._booking_stage = "stay date controls"
            await page.get_by_role("button", name=re.compile("^Check-in.*Check-out")).click()
            await self._choose_day(page, request.check_in, "in")
            await self._choose_day(page, request.check_out, "out")
            await page.get_by_role("dialog").get_by_role("button", name="Done", exact=True).click()
            self._booking_stage = "guest controls"
            await self._set_adults(page, request.adults)
            self._booking_stage = "search submission"
            async with page.expect_popup(timeout=45000) as found:
                await page.get_by_test_id("search-submit-button").click()
            results = await found.value
            self._journey_pages.append(results)
            await self._install_cookie_handler(results)
            await results.wait_for_load_state("domcontentloaded", timeout=45000)
            await self._check_access(results)
            final = urlparse(results.url)
            if final.scheme != "https" or final.hostname != "www.hilton.com" or final.path != "/en/search/":
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton search result URL changed")
            self._booking_stage = "target hotel result"
            card = results.get_by_test_id("hotel-card-" + hotel_code(request.provider_hotel_id))
            await card.wait_for(state="visible", timeout=45000)
            self._booking_stage = "target hotel rates navigation"
            async with results.expect_popup(timeout=45000) as rooms:
                await card.get_by_role("link", name=re.compile("^View Rates")).click()
            room_page = await rooms.value
            self._journey_pages.append(room_page)
            await self._install_cookie_handler(room_page)
            await room_page.wait_for_load_state("domcontentloaded", timeout=45000)
            await self._check_access(room_page)
            verify_rooms_url(room_page.url, request)
            self._page = room_page
            return room_page
        except BrowserTimeout:
            await self._check_access(page)
            raise

    async def _check_access(self, page, status=None):
        if status == 403:
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Hilton returned HTTP 403")
        if status == 429:
            raise ProviderError(ErrorCode.RATE_LIMITED, "Hilton returned HTTP 429")
        if status is not None and status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"Hilton returned HTTP {status}")
        if "Hilton Page Reference Code" in await page.title():
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Hilton Page Reference Code error page")

    async def _open_rooms(self, request: RateRequest):
        page = await self._get_page()
        if self.hotel_name:
            self._journey_pages.append(page)
            page = await self._open_via_home(page, request)
        else:
            # Code-only diagnostic calls retain the verified public deeplink;
            # real catalog-backed rate jobs have the official hotel name.
            response = await page.goto(rooms_url(request), wait_until="domcontentloaded", timeout=45000)
            await self._check_access(page, response.status if response else None)
        try:
            self._booking_stage = "rooms summary"
            await page.get_by_test_id("search-edit-button").wait_for(state="attached", timeout=30000)
            # The summary button can be attached while its hotel/date data is still loading.
            await page.wait_for_function(
                "expected => { const label = document.querySelector('[data-testid=search-edit-button]')?.getAttribute('aria-label') || '';"
                "const name = document.querySelector('[data-testid=hotelName]')?.textContent.trim();"
                "return name && label.includes(name) && expected.some(group => group.every(value => label.includes(value))); }",
                arg=stay_label_expectations(request),
                timeout=30000,
            )
        except BrowserTimeout:
            await self._check_access(page)
            raise
        verify_rooms_url(page.url, request)
        name = verify_displayed_stay(await page.content(), request)
        return page, name

    async def search_hotels(self, query: dict) -> list[HotelData]:
        if query.get("official_url"):
            url = urlparse(query["official_url"])
            match = PROPERTY_PATH.fullmatch(url.path)
            if url.scheme != "https" or url.hostname != "www.hilton.com" or url.query or not match:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton catalog property URL invalid")
            try:
                page = await self._get_page()
                response = await page.goto(url.geturl(), wait_until="domcontentloaded", timeout=45000)
                await self._check_access(page, response.status if response else None)
                await page.locator("h1").first.wait_for(state="visible", timeout=30000)
                await self._check_access(page)
                final = urlparse(page.url)
                final_match = PROPERTY_PATH.fullmatch(final.path)
                if final.hostname != "www.hilton.com" or not final_match or final_match[1] != match[1]:
                    raise ProviderError(
                        ErrorCode.INVALID_RESPONSE, "Hilton property identity changed after navigation"
                    )
                name = (await page.locator("h1").first.inner_text()).strip()
                return [
                    HotelData(
                        provider="hilton",
                        provider_hotel_id=hotel_code(match[1]),
                        hotel_name=name,
                        official_url=page.url,
                    )
                ]
            except BrowserError as exc:
                raise ProviderError(
                    ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                    "Hilton catalog detail page failed",
                ) from exc
        if query.get("provider_hotel_id"):
            return [await self.get_hotel_details(query["provider_hotel_id"])]
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Hilton discovery requires an official property URL or code"
        )

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        day = date.today() + timedelta(days=10)
        request = RateRequest(
            provider_hotel_id=hotel_code(hotel_id), check_in=day, check_out=day + timedelta(days=1)
        )
        try:
            page, name = await self._open_rooms(request)
            return HotelData(
                provider="hilton",
                provider_hotel_id=hotel_code(hotel_id),
                hotel_name=name,
                official_url=page.url,
            )
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Hilton hotel identity page failed",
            ) from exc

    async def search_rates(self, request: RateRequest) -> list[RateData]:
        if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Hilton page adapter supports one room, one night")
        stage = "rooms page"
        self._booking_stage = None
        try:
            page, name = await self._open_rooms(request)
            self._booking_stage = None
            stage = "available room cards"
            await page.locator(ROOM_BUTTONS).first.wait_for(state="visible", timeout=30000)
            verified_url = page.url
            cards = await page.get_by_test_id(
                "roomCardTile"
            ).evaluate_all("""cards => cards
                .filter(card => card.querySelector('[data-testid="moreRatesButton"], [data-testid="accessibleMoreRatesButton"]'))
                .map(card => ({code:card.getAttribute('data-roomtypecode'),
                              name:card.querySelector('[data-testid="roomTypeName"]')?.textContent.trim()}))""")
            if (
                not cards
                or any(
                    not card["name"] or not re.fullmatch(r"[A-Za-z0-9]+", card["code"] or "")
                    for card in cards
                )
                or len({card["code"] for card in cards}) != len(cards)
            ):
                raise ProviderError(
                    ErrorCode.PROVIDER_CHANGED, "Hilton available room identities are not readable"
                )
            rates = []
            for position, card in enumerate(cards):
                stage = "room plan selection"
                tile = page.locator(f'[data-testid="roomCardTile"][data-roomtypecode="{card["code"]}"]')
                await tile.locator(ROOM_BUTTONS).first.click()
                await page.wait_for_url("**/book/reservation/rates/**", timeout=30000)
                stage = "public rate grid"
                await page.locator(
                    '[data-testid="rateTableStandardCell"] [data-testid="ratePrice"] p'
                ).first.wait_for(state="visible", timeout=45000)
                await self._check_access(page)
                currency = await page.locator("#selectCurrencyConverter").input_value()
                rates.extend(
                    parse_public_rates(
                        await page.content(),
                        request,
                        rooms_url=verified_url,
                        currency=currency,
                        hotel_name=name,
                        room_name=card["name"],
                        room_code=card["code"],
                    )
                )
                if position + 1 < len(cards):
                    stage = "return to room cards"
                    await page.get_by_test_id("roomSelectedLabel").first.click()
                    await page.wait_for_url("**/book/reservation/rooms/**", timeout=30000)
                    await page.locator(ROOM_BUTTONS).first.wait_for(state="visible", timeout=30000)
                    verify_rooms_url(page.url, request)
                    verify_displayed_stay(await page.content(), request, name)
            return rates
        except BrowserError as exc:
            if self._page and not self._page.is_closed():
                try:
                    await self._check_access(self._page)
                except BrowserError:
                    pass  # Failed diagnostics must not replace the original network/timeout cause.
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                f"Hilton booking page failed at {self._booking_stage or stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        self.configure_hotel("Conrad London St. James")
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="LONCOCI", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(status="ONLINE" if rates else "DEGRADED", message="Hilton rendered public rates")

    async def close(self):
        try:
            for page in self._journey_pages:
                if page is not self._page and not page.is_closed():
                    await page.close()
        finally:
            self._journey_pages.clear()
            await super().close()
