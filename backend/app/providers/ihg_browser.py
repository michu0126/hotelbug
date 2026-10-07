"""IHG official hotel page -> date controls -> inline/expanded public room prices."""

import re
from datetime import date, timedelta

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider
from app.providers.ihg_catalog import (
    ROOT,
    directory_render_pending,
    directory_url,
    parse_catalog,
    verify_directory_source,
)
from app.providers.ihg_page import (
    NO_ROOMS,
    WINDOW_CLOSED,
    booking_window_closed,
    hotel_search_unavailable,
    parse_hotel,
    parse_room,
    property_identity,
    verify_booking,
    verify_displayed_stay,
)
from app.schemas.domain import CatalogPageData, HealthResult, HotelData, RateData, RateRequest

SAMPLE_URL = "https://www.ihg.com/hotelindigo/hotels/us/en/london/lonls/hoteldetail"


class IHGBrowserProvider(AccorBrowserProvider):
    session_provider = "ihg"

    def __init__(self, *args, headless: bool = False, **kwargs):
        # The verified IHG flow uses an ordinary browser. The Docker Worker
        # supplies its own virtual display; no user browser/profile is attached.
        super().__init__(*args, headless=headless, **kwargs)

    def configure_hotel_url(self, official_url: str):
        property_identity(official_url)
        self.official_url = official_url

    async def _check_access(self, page, status=None):
        if status == 403:
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "IHG returned HTTP 403")
        if status == 429:
            raise ProviderError(ErrorCode.RATE_LIMITED, "IHG returned HTTP 429")
        if status is not None and status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"IHG returned HTTP {status}")
        if "access denied" in (await page.title()).lower():
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "IHG Access Denied page")

    async def _open_property(self, official_url):
        expected = property_identity(official_url)
        page = await self._get_page()
        # IHG can display a usable booking form while ancillary resources delay
        # DOMContentLoaded. Wait for the actual controls, not the whole-site event.
        response = await page.goto(official_url, wait_until="commit", timeout=45000)
        await self._check_access(page, response.status if response else None)
        await page.locator("h1").first.wait_for(state="visible", timeout=30000)
        await self._check_access(page)
        if property_identity(page.url)[4] != expected[4]:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG hotel changed after navigation")
        return page, parse_hotel(await page.content(), page.url)

    async def search_hotels(self, query: dict) -> list[HotelData]:
        url = query.get("official_url")
        if not isinstance(url, str):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "IHG discovery requires official hotel URL")
        try:
            _, hotel = await self._open_property(url)
            return [hotel]
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "IHG hotel detail page failed",
            ) from exc

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        url = getattr(self, "official_url", None)
        if not url or property_identity(url)[4].upper() != hotel_id.upper():
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG official hotel URL required for this code")
        return (await self.search_hotels({"official_url": url}))[0]

    async def discover_catalog(self, query: dict) -> CatalogPageData:
        url = query.get("official_url")
        if not isinstance(url, str):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG catalog URL missing")
        url = directory_url(url)
        try:
            page = await self._get_page()
            response = await page.goto(url, wait_until="commit", timeout=45000)
            await self._check_access(page, response.status if response else None)
            if url == ROOT:
                await (
                    page.locator(".cmp-accordion__title")
                    .filter(has_text="US & Canada")
                    .wait_for(state="visible", timeout=30000)
                )
            else:
                # Small city pages omit the total-count bar entirely. Their
                # public hotel headings are the usable readiness signal.
                await page.wait_for_function(
                    """() => document.querySelector('h4 a[href]') ||
                        document.querySelector('#cmp-card__title-bar-count')?.textContent.trim() === '0'""",
                    timeout=30000,
                )
            await self._check_access(page)
            verify_directory_source(url, page.url)
            html = await page.content()
            if url != ROOT and directory_render_pending(html):
                # The real Alberta page had all 45 hotel links before its
                # public counter became "45". Reading immediately after the
                # first link mislabeled it partial and caused hourly re-reads.
                # Wait briefly for the advertised counter/list to agree; a
                # permanently empty/featured-only counter still stays partial.
                try:
                    await page.wait_for_function(
                        """() => {
                            const label = document.querySelector('[data-render-hotel-count="true"] #cmp-card__title-bar-count')?.textContent.trim().replaceAll(',', '');
                            return !!label && /^\\d+$/.test(label) &&
                                document.querySelectorAll('h4 a[href]').length >= Number(label);
                        }""",
                        timeout=5000,
                    )
                except BrowserTimeout:
                    pass
                await self._check_access(page)
                verify_directory_source(url, page.url)
                html = await page.content()
            return parse_catalog(html, page.url)
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "IHG official directory page failed",
            ) from exc

    async def _choose_day(self, page, day: date):
        cell = page.locator(f'[data-year="{day.year}"][data-month="{day.month}"][data-day="{day.day}"]')
        for _ in range(14):
            if await cell.count():
                await cell.first.click()
                return
            await (
                page.get_by_role("dialog", name="Choose Date", exact=True)
                .get_by_role("button", name="Next", exact=True)
                .click()
            )
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG date outside displayed calendar")

    async def _verify_stay(self, page, request, name):
        verify_booking(page.url, request)
        dates = await page.locator("input.calendar-input").evaluate_all("es => es.map(e => e.value)")
        guests = await page.locator("#room-and-guest").input_value()
        verify_displayed_stay(await page.content(), request, name, dates, guests)

    async def search_rates(self, request: RateRequest) -> list[RateData]:
        if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "IHG page adapter supports one room, one night")
        url = getattr(self, "official_url", None)
        if not url or property_identity(url)[4].upper() != request.provider_hotel_id.upper():
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG task lacks matching official property URL")
        stage = "hotel page"
        try:
            page, hotel = await self._open_property(url)
            stage = "stay dates"
            await page.locator("input.calendar-input").first.click()
            await self._choose_day(page, request.check_in)
            await self._choose_day(page, request.check_out)
            # Selecting checkout can leave the calendar panel open. Use the
            # normal outside click before opening the guest picker.
            await page.locator("h1").first.click()
            stage = "guest count"
            adults = page.get_by_test_id("adults-count-input")
            if not await adults.is_visible():
                try:
                    # The outer modal trigger intentionally covers the readonly
                    # input. Click its real public trigger, not the covered input.
                    await page.get_by_test_id("roomAndGuestModal").click(timeout=5000)
                except BrowserTimeout:
                    # An animated picker can appear during a click retry and
                    # intercept the remaining action. Its visible input is the
                    # authoritative success state; never click it closed again.
                    if not await adults.is_visible():
                        raise
            await adults.fill(str(request.adults))
            await adults.press("Tab")
            await page.locator("h1").first.click()
            stage = "selected stay confirmation"
            await page.wait_for_function(
                """expected => {
                    const dates = [...document.querySelectorAll('input.calendar-input')].map(e => e.value);
                    const guests = document.querySelector('#room-and-guest')?.value;
                    return JSON.stringify(dates) === JSON.stringify(expected.dates) && guests === expected.guests;
                }""",
                arg={
                    "dates": [
                        f"{day.month:02d}/{day.day:02d}/{day.year}"
                        for day in (request.check_in, request.check_out)
                    ],
                    "guests": f"1 Room, {request.adults} Guest" + ("s" if request.adults != 1 else ""),
                },
                timeout=15000,
            )
            stage = "search submission"
            # Brand headers can expose a same-name button that only scrolls to
            # this form. Submit the actual date/occupancy search component.
            await page.get_by_test_id("consolidate-search-submit-button").click()
            await page.wait_for_url(
                re.compile(
                    r"^https://www\.ihg\.com/[^?#]*/en/find-hotels/(?:select-roomrate|hotel-search)\?"
                ),
                wait_until="commit",
                timeout=45000,
            )
            stage = "room results"
            await page.wait_for_function(
                """expected => {
                    const card = [...document.querySelectorAll('app-hotel-card-list-view[data-testid="hotel-card"]')]
                        .find(e => e.id === expected.hotel);
                    return [...document.querySelectorAll('[data-testid="roomNameTestId"]')]
                        .some(e => e.getClientRects().length) ||
                        (document.body?.innerText || '').includes(expected.windowClosed) ||
                        (!!card?.getClientRects().length && card.innerText.includes(expected.noRooms));
                }""",
                arg={
                    "windowClosed": WINDOW_CLOSED,
                    "noRooms": NO_ROOMS,
                    "hotel": request.provider_hotel_id.upper(),
                },
                timeout=45000,
            )
            await self._check_access(page)
            if page.url.split("?", 1)[0].endswith("/en/find-hotels/hotel-search"):
                card = page.locator(
                    f'app-hotel-card-list-view[data-testid="hotel-card"][id="{request.provider_hotel_id.upper()}"]'
                )
                if await card.count() != 1 or not await card.is_visible():
                    raise ProviderError(
                        ErrorCode.INVALID_RESPONSE, "IHG target unavailable hotel card not visible"
                    )
                if not await card.get_by_text(NO_ROOMS, exact=True).is_visible():
                    raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG target no-room message not visible")
                if hotel_search_unavailable(
                    await page.content(),
                    page.url,
                    request,
                    hotel.hotel_name,
                    await page.locator("input.calendar-input").evaluate_all("es => es.map(e => e.value)"),
                    await page.locator("#room-and-guest").input_value(),
                ):
                    return []
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG target no-room state not confirmed")
            if booking_window_closed(
                await page.content(),
                request,
                hotel.hotel_name,
                await page.locator("input.calendar-input").evaluate_all("es => es.map(e => e.value)"),
                await page.locator("#room-and-guest").input_value(),
            ):
                raise ProviderError(
                    ErrorCode.BOOKING_WINDOW_CLOSED, "IHG has not opened booking for these stay dates"
                )
            await self._verify_stay(page, request, hotel.hotel_name)
            cards = page.locator('app-room-rate-item[id^="ROOM_CODE"]')
            ids = await cards.evaluate_all("es => es.map(e => e.id)")
            if (
                not ids
                or len(set(ids)) != len(ids)
                or any(not re.fullmatch(r"ROOM_CODE[A-Z0-9]+", i) for i in ids)
            ):
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG room identities missing or duplicated")
            result = []
            for identity in ids:
                stage = "expand room plans"
                room = page.locator(f"#{identity}")
                expand = room.get_by_role("button", name=re.compile(r"^View prices for "))
                # Some hotels show their actual named plan immediately and
                # have no expand button. Never click Select to inspect prices.
                if await expand.count():
                    await expand.click()
                else:
                    await room.get_by_test_id("rateCard").first.wait_for(state="visible", timeout=15000)
                stage = "public member discount"
                # The switch's accessible name changes after toggling; use its stable DOM identity.
                toggle = room.locator('input[role="switch"]')
                await toggle.wait_for(state="visible", timeout=15000)
                await toggle.set_checked(False)
                stage = "public rate cards"
                await room.get_by_test_id("rateCard").first.wait_for(state="visible", timeout=15000)
                # Switching off the discount triggers an asynchronous rate-card
                # update. A visible card alone can still contain the old member price.
                await page.wait_for_function(
                    """identity => {
                        const room = document.getElementById(identity);
                        const toggle = room?.querySelector('input[role="switch"]');
                        const cards = [...(room?.querySelectorAll('[data-testid="rateCard"]') || [])];
                        return toggle?.getAttribute('aria-checked') === 'false' && cards.length > 0 &&
                            cards.every(card => !card.innerText.includes('IHG One Rewards Discount'));
                    }""",
                    arg=identity,
                    timeout=15000,
                )
                await self._verify_stay(page, request, hotel.hotel_name)
                result.extend(
                    parse_room(await room.inner_html(), request, page.url, identity.removeprefix("ROOM_CODE"))
                )
            return result
        except BrowserError as exc:
            if self._page and not self._page.is_closed():
                try:
                    await self._check_access(self._page)
                except BrowserError:
                    pass
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                f"IHG booking page failed at {stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        self.configure_hotel_url(SAMPLE_URL)
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="LONLS", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(
            status="ONLINE" if rates else "DEGRADED", message="IHG rendered public room prices"
        )
