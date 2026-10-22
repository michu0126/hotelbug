"""Normal public rooms-page navigation and clicks; observe its own price responses.

No independent HTTP client, GraphQL replay, injected fetch, copied authentication,
browser-profile reuse or alternate transport after an access denial.
"""

import asyncio
import math
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from adapters.base import BrandBlocked, RateLimited, TransientFailure, official_url
from adapters.hilton_rates import parse_public_rates
from adapters.hilton_samples import (
    OBSERVED_HOTEL_ALIASES,
    ROOM_SEARCH_OPERATION,
    ROOM_SEARCH_PATH,
    SampleValidationError,
    validate_search_context,
)
from adapters.hilton_urls import RATES_PATH, ROOMS_PATH, rooms_url
from adapters.page_policy import is_official_request, visible_challenge
from configuration import BrowserSettings, RequestPolicy
from models import utcnow

# Seen in supplied public booking assets (room-card and summary components).
ROOM_TILE = '[data-testid="roomCardTile"], [data-testid="accessibleRoomCard"]'
MORE_RATES = (
    '[data-testid="moreRatesButton"], [data-testid="accessibleMoreRatesButton"], '
    'button[data-e2e="moreRatesButton"]'
)
RETURN_TO_ROOMS = '[data-testid="roomSelectedLabel"]'


def retry_after_seconds(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            parsed = parsedate_to_datetime(value)
            result = (parsed - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 3600
    return max(1, result) if math.isfinite(result) else 3600


class HiltonBrowserAdapter:
    def __init__(self, browser_settings=None, request_policy=None, *, clock=utcnow, configure_context=None):
        self.settings = browser_settings or BrowserSettings()
        self.policy = request_policy or RequestPolicy()
        self.clock = clock
        self._configure_context = configure_context  # Offline route fixtures only, not a CLI setting.
        self._playwright = self._browser = None
        self._denial = None
        self._lock = asyncio.Lock()

    async def _start(self):
        if self._browser and self._browser.is_connected():
            return
        await self.close()
        self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.launch(
                headless=self.settings.headless,
                channel=self.settings.channel,
                proxy={"server": self.settings.proxy} if self.settings.proxy else None,
            )
        except BrowserError:
            await self.close()
            raise TransientFailure("Hilton collection browser could not start") from None

    async def _check_page(self, page):
        if self._denial:
            raise self._denial
        if "Hilton Page Reference Code" in await page.title() or await visible_challenge(page):
            raise BrandBlocked("Hilton visible access denial or challenge")

    @staticmethod
    def _request_matches(variables, hotel, stay, selected):
        return isinstance(variables, dict) and (
            isinstance(variables.get("ctyhocn"), str)
            and OBSERVED_HOTEL_ALIASES.get(variables.get("ctyhocn"), variables.get("ctyhocn"))
            == hotel.hotel_id
            and variables.get("arrivalDate") == stay.check_in.isoformat()
            and variables.get("departureDate") == stay.check_out.isoformat()
            and type(variables.get("numAdults")) is int
            and variables["numAdults"] == stay.adults
            and type(variables.get("numRooms")) is int
            and variables["numRooms"] == stay.rooms
            and type(variables.get("numChildren")) is int
            and variables["numChildren"] == 0
            and variables.get("selectedRoomTypeCode") == selected
        )

    async def _search_response(self, page, hotel, stay, selected, action):
        if self._denial:
            raise self._denial
        future = asyncio.get_running_loop().create_future()
        tasks = set()

        async def read_response(response, payload):
            try:
                if response.status >= 500:
                    raise TransientFailure("Hilton price page returned a server error")
                if response.status != 200:
                    raise SampleValidationError("Hilton price response did not succeed")
                data = await response.json()
                validate_search_context(payload["variables"], data, expected_hotel_id=hotel.hotel_id)
                if not future.done():
                    future.set_result((payload["variables"], data))
            except (BrowserError, ValueError, TypeError, KeyError, TransientFailure) as exc:
                if not future.done():
                    future.set_exception(
                        exc
                        if isinstance(exc, (SampleValidationError, TransientFailure))
                        else TransientFailure("Hilton price response could not be read")
                    )

        def on_response(response):
            if future.done():
                return
            if is_official_request("hilton", response.request):
                if response.status == 403:
                    future.set_exception(BrandBlocked("Hilton returned HTTP 403"))
                    return
                if response.status == 429:
                    future.set_exception(
                        RateLimited(retry_after_seconds(response.headers.get("retry-after")))
                    )
                    return
            parsed = urlsplit(response.url)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "www.hilton.com"
                or parsed.path != ROOM_SEARCH_PATH
                or response.request.method != "POST"
            ):
                return
            try:
                payload = response.request.post_data_json
            except (BrowserError, ValueError):
                return
            if (
                not isinstance(payload, dict)
                or payload.get("operationName") != ROOM_SEARCH_OPERATION
                or not self._request_matches(payload.get("variables"), hotel, stay, selected)
            ):
                return
            task = asyncio.create_task(read_response(response, payload))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        async def watch_challenge():
            while not future.done():
                try:
                    await self._check_page(page)
                except BrandBlocked as exc:
                    if not future.done():
                        future.set_exception(exc)
                    return
                except BrowserError:
                    if page.is_closed():
                        if not future.done():
                            future.set_exception(TransientFailure("Hilton collection page closed"))
                        return
                await asyncio.sleep(0.25)

        page.on("response", on_response)
        watcher = asyncio.create_task(watch_challenge())
        action_task = asyncio.create_task(action())
        try:
            _, captured = await asyncio.wait_for(
                asyncio.gather(action_task, future), self.settings.navigation_timeout_ms / 1000
            )
            await self._check_page(page)
            return captured
        finally:
            page.remove_listener("response", on_response)
            watcher.cancel()
            if not action_task.done():
                action_task.cancel()
            for task in list(tasks):
                task.cancel()
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # Consume a denial even if navigation failed first.
            await asyncio.gather(watcher, action_task, *list(tasks), return_exceptions=True)

    async def fetch_rates(self, hotel, stay, *, confirmation=False):
        if hotel.brand != "hilton" or stay.rooms != 1 or (stay.check_out - stay.check_in).days != 1:
            raise ValueError("Hilton verified page flow requires one room and one night")
        official_url("hilton", hotel.official_url)
        async with self._lock:
            await self._start()
            try:
                context = await self._browser.new_context()
            except BrowserError:
                await self.close()
                raise TransientFailure("Hilton collection context could not start") from None
            self._denial = None

            def remember_denial(response):
                if is_official_request("hilton", response.request) and not self._denial:
                    if response.status == 403:
                        self._denial = BrandBlocked("Hilton returned HTTP 403")
                    elif response.status == 429:
                        self._denial = RateLimited(retry_after_seconds(response.headers.get("retry-after")))

            context.on("response", remember_denial)
            try:
                if self._configure_context:
                    await self._configure_context(context)
                page = await context.new_page()
                timeout = self.settings.navigation_timeout_ms
                page.set_default_timeout(timeout)
                source = rooms_url(hotel.hotel_id, stay)
                variables, data = await self._search_response(
                    page,
                    hotel,
                    stay,
                    None,
                    lambda: page.goto(source, wait_until="domcontentloaded", timeout=timeout),
                )
                dimensions = validate_search_context(variables, data, expected_hotel_id=hotel.hotel_id)
                room_types = dimensions[7]
                codes = [room.get("roomTypeCode") for room in room_types]
                if (
                    not codes
                    or any(
                        not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", code)
                        for code in codes
                    )
                    or len(set(codes)) != len(codes)
                    or len(codes) > self.settings.max_rooms_per_search
                ):
                    raise SampleValidationError("Room identities are invalid or exceed the configured bound")
                result = []
                for position, code in enumerate(codes):
                    if position:
                        await page.locator(RETURN_TO_ROOMS).first.click()
                        await page.wait_for_url(lambda url: urlsplit(url).path == ROOMS_PATH, timeout=timeout)
                    tile = page.locator(ROOM_TILE).and_(page.locator(f'[data-roomtypecode="{code}"]'))
                    await tile.wait_for(state="visible", timeout=timeout)
                    if await tile.count() != 1:
                        raise SampleValidationError("Visible room identity is not unique")
                    await asyncio.sleep(
                        random.uniform(self.policy.delay_min_seconds, self.policy.delay_max_seconds)
                    )
                    response_variables, response_data = await self._search_response(
                        page, hotel, stay, code, lambda tile=tile: tile.locator(MORE_RATES).first.click()
                    )
                    if urlsplit(page.url).netloc != "www.hilton.com" or urlsplit(page.url).path != RATES_PATH:
                        raise SampleValidationError(
                            "Room selection did not open the observed official rates page"
                        )
                    parsed = parse_public_rates(
                        response_variables,
                        response_data,
                        hotel,
                        stay,
                        observed_at=self.clock(),
                        source_url=source,
                    )
                    result.extend(parsed.quotes)
                await self._check_page(page)
                return result
            except (BrowserError, asyncio.TimeoutError):
                if "page" in locals() and not page.is_closed():
                    try:
                        await self._check_page(page)
                    except BrowserError:
                        pass
                raise TransientFailure("Hilton ordinary page operation timed out or failed") from None
            finally:
                try:
                    await context.close()  # Fresh independent context for the next/confirmation fetch.
                except BrowserError:
                    pass  # A disconnected browser must not replace an already observed 403/429.
                finally:
                    self._denial = None

    async def close(self):
        try:
            if self._browser:
                try:
                    await self._browser.close()
                except BrowserError:
                    pass
        finally:
            self._browser = None
            if self._playwright:
                await self._playwright.stop()
                self._playwright = None
