"""Hilton official rooms -> public rate grid, with verified rendered stay identity."""

import re
from datetime import date, timedelta
from urllib.parse import urlencode, urlparse

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider
from app.providers.hilton_page import parse_public_rates, verify_displayed_stay, verify_rooms_url
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.hilton.com"
ROOM_BUTTONS = '[data-testid="moreRatesButton"], [data-testid="accessibleMoreRatesButton"]'
PROPERTY_PATH = re.compile(r"/en/hotels/([a-z0-9]{7})-[a-z0-9-]+/?")


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

    async def _get_page(self):
        created = self._page is None
        page = await super()._get_page()
        if created:

            async def dismiss_cookie(locator):
                await locator.click()

            await page.add_locator_handler(
                page.get_by_role("button", name="dismiss cookie message", exact=True), dismiss_cookie
            )
        return page

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
        response = await page.goto(rooms_url(request), wait_until="domcontentloaded", timeout=45000)
        await self._check_access(page, response.status if response else None)
        try:
            await page.get_by_test_id("search-edit-button").wait_for(state="attached", timeout=30000)
            # The summary button can be attached while its hotel/date data is still loading.
            await page.wait_for_function(
                "expected => { const label = document.querySelector('[data-testid=search-edit-button]')?.getAttribute('aria-label') || '';"
                "const name = document.querySelector('[data-testid=hotelName]')?.textContent.trim();"
                "return name && label.includes(name) && expected.every(value => label.includes(value)); }",
                arg=[f"{day.year}年{day.month}月{day.day}日" for day in (request.check_in, request.check_out)]
                + [f"1 間客房，{request.adults} 位成人"],
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
        try:
            page, name = await self._open_rooms(request)
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
                currency = await page.get_by_role("combobox", name="選擇貨幣", exact=True).input_value()
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
                f"Hilton booking page failed at {stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="LONCOCI", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(status="ONLINE" if rates else "DEGRADED", message="Hilton rendered public rates")
