"""Hyatt official rendered room cards and selected public rate-plan dialogs."""

import re
from datetime import date, timedelta

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider
from app.providers.hyatt_catalog import ROOT as CATALOG_ROOT
from app.providers.hyatt_catalog import (
    combine_regions,
    directory_url,
    parse_region,
    property_identity,
    region_names,
)
from app.providers.hyatt_page import (
    hotel_code,
    parse_hotel,
    parse_selected_rate,
    quote_currency,
    rooms_url,
    verify_stay,
)
from app.schemas.domain import CatalogPageData, HealthResult, HotelData, RateData, RateRequest

FRAME = '[role="dialog"] li.slide.selected > .room-rates-frame-container'


class HyattBrowserProvider(AccorBrowserProvider):
    session_provider = "hyatt"

    def __init__(self, *args, headless: bool = False, **kwargs):
        super().__init__(*args, headless=headless, **kwargs)

    def configure_hotel(self, name: str):
        self.expected_hotel_name = name

    def configure_hotel_url(self, url: str):
        # Validate identity without opening the URL or reading browser state.
        property_identity(url)
        self.official_url = url

    async def _check_access(self, page, status=None, retry_after=None):
        if status in (401, 403):
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, f"Hyatt returned HTTP {status}")
        if status == 429:
            raise ProviderError(ErrorCode.RATE_LIMITED, "Hyatt returned HTTP 429", retry_after=retry_after)
        if status is not None and status >= 400:
            raise ProviderError(ErrorCode.HTTP_ERROR, f"Hyatt returned HTTP {status}")
        title = (await page.title()).lower()
        if "access denied" in title:
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Hyatt access denied page")

    async def _navigate(self, url: str):
        page = await self._get_page()
        response = await page.goto(url, wait_until="commit", timeout=45000)
        retry = None
        if response is not None and response.status == 429:
            value = await response.header_value("retry-after")
            if value and value.isdecimal():
                retry = int(value)
        await self._check_access(page, response.status if response else None, retry)
        return page

    async def _verify_native_currency(self, page):
        # hotelCurrencyCode names the native hotel currency. Do not apply it to
        # a converted display if an old dedicated profile remembers a choice.
        if await page.locator("#currency-select").input_value() != "":
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Hyatt converted display currency is not supported"
            )

    async def discover_catalog(self, query: dict) -> CatalogPageData:
        url = query.get("official_url")
        if not isinstance(url, str):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt catalog URL missing")
        url = directory_url(url)
        try:
            page = await self._navigate(url + "/")
            await page.locator("main button.dream-list-accordion__trigger").first.wait_for(
                state="visible", timeout=45000
            )
            if directory_url(page.url) != CATALOG_ROOT:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt catalog redirected unexpectedly")
            names = region_names(await page.content())
            observations = {}
            for name in names:
                button = page.get_by_role("button", name=name, exact=True)
                if await button.get_attribute("aria-expanded") != "true":
                    await button.click()
                region = page.get_by_role("region", name=name, exact=True)
                await region.wait_for(state="visible", timeout=15000)
                await region.locator(".dream-list-accordion__property-list-item a[href]").first.wait_for(
                    state="visible", timeout=15000
                )
                await self._check_access(page)
                if directory_url(page.url) != url:
                    raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt catalog source changed")
                observations[name] = parse_region(await page.content(), name)
            return combine_regions(url, observations)
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Hyatt worldwide list failed",
            ) from exc

    async def search_hotels(self, query: dict) -> list[HotelData]:
        url = query.get("official_url")
        if not isinstance(url, str):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Hyatt discovery requires an official hotel URL")
        self.configure_hotel_url(url)
        try:
            page = await self._navigate(url)
            await page.locator("h1").first.wait_for(state="visible", timeout=30000)
            await self._check_access(page)
            hotel = parse_hotel(await page.content(), page.url)
            if hotel.provider_hotel_id != property_identity(url)[0]:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt hotel redirected to another property")
            return [hotel]
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                "Hyatt official hotel page failed",
            ) from exc

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        url = getattr(self, "official_url", None)
        if not url or property_identity(url)[0].lower() != hotel_code(hotel_id):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt task lacks a matching official hotel URL")
        return (await self.search_hotels({"official_url": url}))[0]

    async def search_rates(self, request: RateRequest) -> list[RateData]:
        if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Hyatt page adapter supports one room, one night")
        url = getattr(self, "official_url", None)
        if url and property_identity(url)[0].lower() != hotel_code(request.provider_hotel_id):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt configured property and task do not match")
        stage = "booking page"
        try:
            page = await self._navigate(rooms_url(request))
            await page.locator('[data-locator="hotel-name"] h1').wait_for(state="visible", timeout=45000)
            await page.locator('[data-module="room-card"]').first.wait_for(state="visible", timeout=45000)
            await self._check_access(page)
            await self._verify_native_currency(page)
            hotel_name = verify_stay(
                await page.content(), page.url, request, getattr(self, "expected_hotel_name", None)
            )
            tabs = page.get_by_role("tablist", name="Room Type Selection").get_by_role("tab")
            labels = await tabs.all_text_contents()
            if not labels or len(labels) != len(set(labels)):
                raise ProviderError(
                    ErrorCode.PROVIDER_CHANGED, "Hyatt room categories are missing or ambiguous"
                )
            results = []
            seen_rooms = set()
            for label in labels:
                stage = "room category"
                expected_count = re.fullmatch(r"(?:客房|套房)\s*\((\d+)\)", label.strip())
                if expected_count is None:
                    raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt room category label changed")
                await tabs.filter(has_text=re.compile("^" + re.escape(label) + "$")).click()
                panel = page.locator('[role="tabpanel"][aria-hidden="false"]')
                await panel.wait_for(state="visible", timeout=15000)
                await page.wait_for_function(
                    """expected => document.querySelector('[role="tabpanel"][aria-hidden="false"]')
                        ?.querySelectorAll('[data-module="room-card"]').length === expected""",
                    arg=int(expected_count[1]),
                    timeout=15000,
                )
                rooms = await panel.locator('[data-module="room-card"]').evaluate_all(
                    "es => es.map(e => ({code:e.getAttribute('data-room-type-code'),"
                    "name:e.querySelector('[data-locator=room-title]')?.textContent.trim(),"
                    "price:e.querySelector('[data-locator=cash-rate] .rate-values-prices')?.textContent.trim()}))"
                )
                if len(rooms) != int(expected_count[1]):
                    raise ProviderError(
                        ErrorCode.INVALID_RESPONSE, "Hyatt room category has not finished updating"
                    )
                for room in rooms:
                    code, name, price = room["code"], room["name"], room["price"]
                    if (
                        not isinstance(code, str)
                        or not re.fullmatch(r"[A-Z0-9]+", code)
                        or not name
                        or not price
                        or code in seen_rooms
                    ):
                        raise ProviderError(
                            ErrorCode.INVALID_RESPONSE,
                            "Hyatt room identity or starting member price is missing",
                        )
                    seen_rooms.add(code)
                    stage = "rate dialog"
                    await panel.locator(f'button[id="{code}-more-rates"]').click()
                    # Hidden carousel frames repeat the currently loaded room's
                    # plans. A modal existing is not evidence of the new room.
                    await page.wait_for_function(
                        """expected => {
                            const f = document.querySelector('[role="dialog"] li.slide.selected > .room-rates-frame-container');
                            return f?.getAttribute('aria-label') === expected.name &&
                                f.querySelector('.room_rates_modal__rates--static [data-locator="cash"]')?.textContent.trim() === expected.price;
                        }""",
                        arg={"name": name, "price": price},
                        timeout=15000,
                    )
                    frame = page.locator(FRAME)
                    currency = quote_currency(await page.content(), request, code, name)
                    plans = await frame.locator('input[name="rate"]').evaluate_all(
                        "es => es.map(e => e.value)"
                    )
                    if (
                        not plans
                        or len(plans) != len(set(plans))
                        or any(not re.fullmatch(r"[A-Z0-9]+", p) for p in plans)
                    ):
                        raise ProviderError(
                            ErrorCode.INVALID_RESPONSE, "Hyatt plan identities missing or duplicated"
                        )
                    for plan in plans:
                        stage = "selected plan"
                        # Select a plan only. Never click the form's submit,
                        # sign-in, join, or payment controls.
                        await (
                            frame.locator('[role="radio"]')
                            .filter(has=page.locator(f'input[value="{plan}"]'))
                            .click()
                        )
                        await page.wait_for_function(
                            """code => {
                                const f = document.querySelector('[role="dialog"] li.slide.selected > .room-rates-frame-container');
                                const r = f?.querySelector('[role="radio"][aria-checked="true"]');
                                return r?.querySelector('input')?.value === code &&
                                    r.querySelector('[aria-hidden="false"].selected_rate_description') &&
                                    r.querySelector('[data-locator="cash"]')?.textContent.trim() ===
                                    f.querySelector('.room_rates_modal__rates--static [data-locator="cash"]')?.textContent.trim();
                            }""",
                            arg=plan,
                            timeout=15000,
                        )
                        html = await page.content()
                        await self._verify_native_currency(page)
                        verify_stay(html, page.url, request, hotel_name)
                        results.append(
                            parse_selected_rate(
                                html,
                                request,
                                room_code=code,
                                room_name=name,
                                plan_code=plan,
                                currency=currency,
                                hotel_name=hotel_name,
                            )
                        )
                    await frame.get_by_role("button", name="Close", exact=True).click()
                    await frame.wait_for(state="hidden", timeout=15000)
            if not results:
                raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt verified room plans are absent")
            return results
        except BrowserError as exc:
            raise ProviderError(
                ErrorCode.TIMEOUT if isinstance(exc, BrowserTimeout) else ErrorCode.NETWORK_ERROR,
                f"Hyatt booking failed at {stage}",
            ) from exc

    async def get_calendar_rates(self, request: RateRequest) -> list[RateData]:
        return await self.search_rates(request)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="SHACP", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(
            status="ONLINE" if rates else "DEGRADED", message="Hyatt rendered selected rate plans"
        )
