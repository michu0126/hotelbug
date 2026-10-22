"""Drive the ordinary China-site calendar; prices are only observed, never replayed.

Attach to the user's dedicated, headed Chrome profile. Never close that browser,
change its cookies, inject fetch(), or navigate directly to the booking endpoint.
Selectors below were inspected on the XIYBP public hotel overview, 2026-10-20.
"""

import asyncio
import contextlib
import re
from dataclasses import replace
from datetime import date
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott import parse_capture
from adapters.marriott_browser import retry_after
from adapters.page_policy import is_official_request, visible_challenge
from passive_capture import debugger_info, is_target, process_capture

HOST = "www.marriott.com.cn"
HOMEPAGE = f"https://{HOST}/"
DATE_INPUT = "#datesInput"
CALENDARS = ".drp-calendar:visible"
MONTH_HEADING = "th.month"
DAY_CELL = "td.available:not(.off):not(.disabled)"
NEXT_MONTH = "th.next.available"
PREVIOUS_MONTH = "th.prev.available"
APPLY = "确认"
GUESTS = "Select number of guests dropdown"
VIEW_RATES = "查看房价"
PROPERTY_CARD = ".property-card-container:not(:has(.property-card-container))"
SOLD_OUT_ALERT = "您选择的酒店没有空房"
MAX_MONTH_MOVES = 13
CHINESE_MONTHS = ("一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二")


class DateNotSelectable(ValueError):
    """An ordinary calendar does not yet allow this date; not a zero-price quote."""


def validate_hotel_url(url, hotel_id):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != HOST
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[A-Z0-9]{5}", hotel_id)
        or not re.fullmatch(
            rf"/hotels/(?:travel/)?{hotel_id.lower()}-[a-z0-9-]+(?:/overview/?)?/?", parsed.path
        )
    ):
        raise ValueError("Use this hotel's public Marriott China overview URL, not a booking URL")
    return url


def calendar_month(text):
    match = re.fullmatch(r"\s*([一二三四五六七八九十]+)月\s+(\d{4})\s*", text)
    if not match or match[1] not in CHINESE_MONTHS:
        raise ValueError("Unrecognized calendar month")
    return int(match[2]), CHINESE_MONTHS.index(match[1]) + 1


def matches_request(payload, task):
    """No customer IDs or tokens leave this in-memory comparison."""
    try:
        options = payload["variables"]["search"]["options"]
        return (
            payload.get("operationName") == "PhoenixBookDTTSearchProductsByProperty"
            and payload["variables"]["search"]["propertyId"] == task.hotel_id
            and options["startDate"] == str(task.checkin)
            and options["endDate"] == str(task.checkout)
            and type(options["quantity"]) is int
            and options["quantity"] == 1
            and type(options["numberInParty"]) is int
            and options["numberInParty"] == task.adults
            and options["childAges"] == []
        )
    except (KeyError, TypeError):
        return False


class MarriottUIDriver:
    def __init__(self, *, port=9222, timeout_seconds=90, context=None):
        self.port, self.timeout_seconds = port, timeout_seconds
        self._playwright = self._browser = self._context = self._page = None
        self._provided_context = context
        self._owned = set()
        self._initialized = False
        self.stage = "未开始"

    async def start(self):
        if self._context is not None:
            return
        try:
            if self._provided_context is not None:
                # NAS Worker supplies its persistent, privately configured session.
                self._context = self._provided_context
            else:
                info = debugger_info(self.port)
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(info["webSocketDebuggerUrl"])
                if len(self._browser.contexts) != 1:
                    raise ValueError("Expected one dedicated persistent Chrome context")
                self._context = self._browser.contexts[0]
            # Never take over the user's search/results tabs.
            self._page = await self._context.new_page()
            self._owned.add(self._page)
            self._page.on("popup", self._popup)
            self._page.set_default_timeout(15000)
        except BaseException:
            await self.close()
            raise

    def _popup(self, page):
        self._owned.add(page)
        self._page = page
        page.set_default_timeout(15000)
        page.on("popup", self._popup)

    async def close(self):
        # stop() disconnects the automation transport; it does NOT close Chrome.
        if self._playwright:
            await self._playwright.stop()
        self._playwright = self._browser = self._context = self._page = None
        self._owned.clear()
        self._initialized = False

    async def _choose_day(self, day):
        day = date.fromisoformat(str(day))
        page = self._page
        for _ in range(MAX_MONTH_MOVES + 1):
            calendars = page.locator(CALENDARS)
            months = []
            for i in range(await calendars.count()):
                calendar = calendars.nth(i)
                month = calendar_month(await calendar.locator(MONTH_HEADING).inner_text())
                months.append(month)
                if month == (day.year, day.month):
                    cell = calendar.locator(DAY_CELL).filter(has_text=re.compile(rf"^{day.day}$"))
                    if await cell.count() != 1:
                        raise DateNotSelectable("Requested date is not selectable in the official calendar")
                    await cell.click()
                    return
            if not months:
                raise ValueError("Official date calendar is not visible")
            direction = PREVIOUS_MONTH if (day.year, day.month) < min(months) else NEXT_MONTH
            arrow = calendars.locator(direction).filter(visible=True)
            if await arrow.count() != 1:
                raise DateNotSelectable("Date is outside the official booking window")
            before = [await c.inner_text() for c in await calendars.locator(MONTH_HEADING).all()]
            await arrow.click()
            # Only await the ordinary DOM update; never change calendar JS state.
            for _ in range(20):
                after = await page.locator(CALENDARS).locator(MONTH_HEADING).all_inner_texts()
                if after != before:
                    break
                await asyncio.sleep(0.05)
        raise ValueError("Requested month was not reached")

    async def _set_counter(self, kind, desired):
        page = self._page
        plus = page.get_by_role("button", name=f"Increase number of {kind}", exact=True)
        minus = page.get_by_role("button", name=f"Decrease number of {kind}", exact=True)
        # This parent contains the rendered numeric count between two buttons.
        text = await plus.locator("..").inner_text()
        numbers = re.findall(r"\d+", text)
        if len(numbers) != 1:
            raise ValueError("Guest count was not explicit")
        current = int(numbers[0])
        if not 0 <= current <= 8:
            raise ValueError("Unexpected occupancy")
        for _ in range(abs(current - desired)):
            await (plus if current < desired else minus).click()
        if re.findall(r"\d+", await plus.locator("..").inner_text()) != [str(desired)]:
            raise ValueError("Guest control did not accept the requested occupancy")

    async def _actions(self, task, arm, guard):
        page = self._page
        if not self._initialized:
            self.stage = "打开万豪中国站首页"
            await page.goto(HOMEPAGE, wait_until="domcontentloaded")
            guard()
            if await visible_challenge(page):
                raise BrandBlocked("Official Marriott homepage shows a verification challenge")
            self._initialized = True
        self.stage = "打开酒店介绍页"
        await page.goto(task.hotel_url, wait_until="domcontentloaded")
        guard()
        if await visible_challenge(page):
            raise BrandBlocked("Official Marriott page shows a verification challenge")
        self.stage = "选择入住及退房日期"
        await page.locator(DATE_INPUT).click()
        await self._choose_day(task.checkin)
        await self._choose_day(task.checkout)
        await page.get_by_role("button", name=APPLY, exact=True).filter(visible=True).click()
        self.stage = "确认客房及人数"
        await page.get_by_role("button", name=GUESTS, exact=True).click()
        await self._set_counter("rooms", 1)
        await self._set_counter("Adults", task.adults)
        await self._set_counter("Children", 0)
        await page.get_by_role("button", name=APPLY, exact=True).filter(visible=True).click()
        self.stage = "点击查看房价"
        guard()
        arm()
        # The first visible View Rates is the header's all-room-types search,
        # not an individual room card's booking button.
        await page.get_by_role("button", name=VIEW_RATES, exact=True).filter(visible=True).first.click()
        self.stage = "等待官网房价响应"

    async def _sold_out(self, task):
        """A sold-out redirect is an availability result, never a zero price."""
        page = self._page
        parsed = urlsplit(page.url)
        options = parse_qs(parsed.query)
        expected = {
            "propertyCode": task.hotel_id,
            "fromDate": str(task.checkin),
            "toDate": str(task.checkout),
            "numberOfRooms": "1",
            "numberOfAdults": str(task.adults),
            "childrenCount": "0",
            "unsuccessfulSell": "true",
        }
        if (
            parsed.netloc != HOST
            or parsed.path != "/search/findHotels.mi"
            or any(options.get(key) != [value] for key, value in expected.items())
        ):
            return False
        alert = page.get_by_role("alert").filter(has_text=SOLD_OUT_ALERT)
        card = (
            page.locator(PROPERTY_CARD)
            .filter(visible=True)
            .filter(has=page.locator(f'a[href*="propertyCode={task.hotel_id}&"]'))
        )
        return (
            await alert.count() > 0
            and await alert.first.is_visible()
            and await card.count() == 1
            and "售罄" in await card.inner_text()
        )

    async def query(self, task, *, include_records=False):
        validate_hotel_url(task.hotel_url, task.hotel_id)
        if type(task.adults) is not int or not 1 <= task.adults <= 8:
            raise ValueError("Only one room, 1–8 adults and no children are supported")
        await self.start()
        future = asyncio.get_running_loop().create_future()
        background = set()
        armed = False
        denial = None

        def arm():
            nonlocal armed
            armed = True

        def guard():
            if denial:
                raise denial

        async def read(response):
            try:
                if response.status >= 500:
                    raise TransientFailure("Official price response returned a server error")
                if response.status != 200:
                    raise ValueError("Official price response was not successful")
                doc = process_capture(response.url, response.request.post_data, await response.text())
                records = parse_capture(doc)  # Reject incomplete pages, mismatched dates, ambiguous currency.
                result = {
                    "status": "ok",
                    "hotel_id": task.hotel_id,
                    "checkin": str(task.checkin),
                    "checkout": str(task.checkout),
                    "adults": task.adults,
                    "offers": len(records),
                    "source_site": doc["source_site"],
                    "response_hash": records[0].response_hash if records else None,
                }
                if include_records:
                    # These records came from this owned page's matching live
                    # response, not from re-reading a saved sample or replaying it.
                    result["records"] = [replace(record, source_kind="LIVE") for record in records]
                if not future.done():
                    future.set_result(result)
            except (BrowserError, ValueError, TypeError, KeyError, TransientFailure) as error:
                if not future.done():
                    future.set_exception(
                        error if isinstance(error, (ValueError, TransientFailure)) else TransientFailure()
                    )

        def on_response(response):
            nonlocal denial
            if not is_official_request("marriott", response.request):
                return
            try:
                if response.request.frame.page not in self._owned:
                    return
            except BrowserError:
                return
            if response.status in (403, 429):
                denial = (
                    BrandBlocked("Official Marriott page returned HTTP 403")
                    if response.status == 403
                    else RateLimited(retry_after(response.headers.get("retry-after")))
                )
                if not future.done():
                    future.set_exception(denial)
            if future.done() or not armed or not is_target(response.url, response.request.method):
                return
            try:
                if not matches_request(response.request.post_data_json, task):
                    return
            except (BrowserError, ValueError):
                return
            pending = asyncio.create_task(read(response))
            background.add(pending)
            pending.add_done_callback(background.discard)

        async def watch():
            while not future.done():
                try:
                    if await visible_challenge(self._page):
                        raise BrandBlocked("Official Marriott page shows a verification challenge")
                    if armed and await self._sold_out(task) and not future.done():
                        future.set_result(
                            {
                                "status": "no_availability",
                                "hotel_id": task.hotel_id,
                                "checkin": str(task.checkin),
                                "checkout": str(task.checkout),
                                "adults": task.adults,
                                "offers": 0,
                                "source_site": HOST,
                                "evidence": "official_sold_out_redirect_and_matching_hotel_card",
                            }
                        )
                        return
                except BrandBlocked as error:
                    future.set_exception(error)
                    return
                except BrowserError:
                    if self._page.is_closed():
                        future.set_exception(TransientFailure("Automatic query tab was closed"))
                        return
                await asyncio.sleep(0.25)

        self._context.on("response", on_response)
        action = asyncio.create_task(self._actions(task, arm, guard))
        watcher = asyncio.create_task(watch())
        try:
            _, result = await asyncio.wait_for(asyncio.gather(action, future), self.timeout_seconds)
            if denial:
                raise denial
            self.stage = "已收到匹配房价"
            # The site opens a new results tab. Retain its latest result while
            # closing only older tabs created by this driver, never user tabs.
            for old in tuple(self._owned):
                if old != self._page:
                    with contextlib.suppress(BrowserError):
                        await old.close()
                    self._owned.discard(old)
            return result
        except (BrowserError, asyncio.TimeoutError):
            if denial:
                raise denial
            raise TransientFailure(f"Official query did not finish at stage: {self.stage}") from None
        finally:
            self._context.remove_listener("response", on_response)
            for pending in (action, watcher, *background):
                pending.cancel()
            await asyncio.gather(action, watcher, *background, return_exceptions=True)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                with contextlib.suppress(Exception):
                    future.exception()
