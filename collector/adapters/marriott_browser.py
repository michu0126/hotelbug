"""Persistent project Chrome: homepage -> hotel -> dates -> View Rates.

Only normal page actions; no direct booking-entry navigation, injected fetch,
API replay, copied user cookies or alternative transport after a denial.
"""

import asyncio
import math
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott import RATES_OPERATION, RATES_PATH, ROOT, MarriottSampleError, _stay, parse_prices
from adapters.page_policy import is_official_request, visible_challenge
from configuration import BrowserSettings
from models import utcnow

HOMEPAGE = ROOT + "/"
DEFAULT_PROFILE = Path(__file__).resolve().parents[1] / ".profiles" / "marriott"
HOTEL_NAMES = {"NYCMQ": "New York Marriott Marquis"}
DESTINATION = "Destination"
SUGGESTIONS = '[id^="downshift-0-item-"]'
FIND_HOTELS = "Find Hotels"
HOTEL_CARD = ".property-card-container"
DATES = re.compile(r"STAY DATES|Check.?in|^Dates\b", re.IGNORECASE)
GUESTS = re.compile(r"ROOMS & GUESTS|1 Room, \d+ Guests?", re.IGNORECASE)
VIEW_RATES = re.compile(r"^(?:View Rates|Check Availability)$", re.IGNORECASE)
DATE_APPLY = re.compile(r"^(?:Done|Update|Apply|Save Dates)$", re.IGNORECASE)
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAX_CALENDAR_MONTHS = 13


def day_label(day):
    return f"{WEEKDAYS[day.weekday()]} {MONTHS[day.month - 1]} {day.day:02d} {day.year}"


def retry_after(value):
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return 3600
    return max(1, seconds) if math.isfinite(seconds) else 3600


class MarriottLivePage:
    def __init__(self, settings=None, *, adults=2, configure_context=None, profile_dir=None, hotel_name=None):
        self.settings = settings or BrowserSettings()
        self.adults = adults
        self.profile_dir = Path(profile_dir or DEFAULT_PROFILE).resolve()
        self.hotel_name = hotel_name
        self._configure_context = configure_context
        self._playwright = self._browser = None
        self._context = self._page = self._profile_lock = None
        self.diagnostics = {}

    async def _start(self):
        if self._context is not None:
            return
        await self.close()
        # Keep one owner even when two commands target different SQLite files.
        from main import RunLock

        lock = RunLock(self.profile_dir.parent / (self.profile_dir.name + ".lock"))
        lock.__enter__()
        self._profile_lock = lock
        try:
            self._playwright = await async_playwright().start()
            self._context = await self._playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                headless=self.settings.headless,
                channel=self.settings.channel,
                proxy={"server": self.settings.proxy} if self.settings.proxy else None,
                locale="en-US",
            )
            self._browser = self._context.browser
        except BaseException:
            await self.close()
            raise

    def _stage(self, name):
        self.diagnostics["stage"] = name
        self.diagnostics.setdefault("steps", []).append({"stage": name, "at": utcnow().isoformat()})

    async def _choose_day(self, day):
        page = self._page
        cell = page.get_by_role("gridcell", name=day_label(day), exact=True)
        for month in range(MAX_CALENDAR_MONTHS):
            if await cell.count() and await cell.first.is_visible():
                await cell.first.click()
                return
            if month == MAX_CALENDAR_MONTHS - 1:
                break
            await page.get_by_role("button", name="Next Month", exact=True).click()
            # Wait for normal DOM mutation; never alter calendar state with JS.
            await page.wait_for_timeout(350)
        raise MarriottSampleError("Requested date is outside the visible hotel calendar")

    async def _homepage_hotel_dates(self, code, ci, co, name, before_submit):
        page = self._page
        self._stage("HOMEPAGE")
        await page.goto(HOMEPAGE, wait_until="domcontentloaded", timeout=self.settings.navigation_timeout_ms)
        self._stage("SEARCH_HOTEL")
        await page.get_by_role("textbox", name=DESTINATION, exact=True).fill(name)
        await page.locator(SUGGESTIONS).filter(has_text=name).first.click()
        await page.get_by_role("button", name=FIND_HOTELS, exact=True).click()
        card = page.locator(HOTEL_CARD).filter(has=page.get_by_role("button", name=name, exact=True))
        await card.wait_for(state="visible")
        self._stage("OPEN_HOTEL")
        selector = f'a[href*="/hotels/"][href*="/{code.lower()}-"]'
        link = card.locator(selector).filter(visible=True)
        if not await link.count():
            await card.get_by_role("button", name=name, exact=True).click()
            link = page.locator(selector).filter(visible=True)
        await link.first.wait_for(state="visible")
        href = urlsplit(urljoin(page.url, await link.first.get_attribute("href")))
        if href.scheme != "https" or href.netloc != "www.marriott.com" or href.query or href.fragment:
            raise MarriottSampleError("Hotel page link is not the expected official overview")
        if await link.first.get_attribute("target") == "_blank":
            async with self._context.expect_page() as popup:
                await link.first.click()
            self._page = page = await popup.value
            page.set_default_timeout(self.settings.navigation_timeout_ms)
        else:
            await link.first.click()
        await page.wait_for_url(
            lambda url: (
                urlsplit(url).netloc == "www.marriott.com" and f"/{code.lower()}-" in urlsplit(url).path
            )
        )
        self._stage("SELECT_DATES")
        await page.get_by_role("button", name=DATES).first.click()
        await self._choose_day(ci)
        await self._choose_day(co)
        await page.get_by_role("button", name=DATE_APPLY).filter(visible=True).first.click()
        guests = page.get_by_role("button", name=GUESTS).first
        label = await guests.inner_text()
        match = re.search(r"1 Room,\s*(\d+) Guests?", label, re.IGNORECASE)
        if not match:
            raise MarriottSampleError("Hotel page guest count is not explicit")
        current = int(match[1])
        if current != self.adults:
            await guests.click()
            control = "Add Adults" if current < self.adults else "Remove Adults"
            for _ in range(abs(current - self.adults)):
                await page.get_by_role("button", name=control, exact=True).click()
            await page.get_by_role("button", name="Update", exact=True).click()
        self._stage("VIEW_RATES")
        before_submit()
        button = page.get_by_role("button", name=VIEW_RATES).or_(page.get_by_role("link", name=VIEW_RATES))
        await button.filter(visible=True).first.click()

    async def fetch_prices(self, hotel_id, checkin, checkout):
        code, ci, co = _stay(hotel_id, checkin, checkout)
        name = self.hotel_name or HOTEL_NAMES.get(code)
        if not name:
            raise MarriottSampleError("Homepage search needs the official hotel name")
        self.diagnostics = {
            "stage": "BROWSER_START",
            "responses": [],
            "request_failures": [],
            "profile_dir": str(self.profile_dir),
            "persistent_profile": True,
            "homepage": HOMEPAGE,
            "hotel_name": name,
        }
        try:
            await self._start()
        except BrowserError:
            await self.close()
            raise TransientFailure("Marriott persistent Chrome could not start") from None
        context = self._context
        page = None
        tasks, action_task, watcher = set(), None, None
        future = asyncio.get_running_loop().create_future()
        denial = None
        accept_prices = False

        def before_submit():
            nonlocal accept_prices
            accept_prices = True

        async def read_prices(response, options):
            try:
                if response.status >= 500:
                    raise TransientFailure("Marriott price response returned a server error")
                if response.status != 200:
                    raise MarriottSampleError("Marriott price response did not succeed")
                data = await response.json()
                records = parse_prices(
                    data,
                    code,
                    ci,
                    co,
                    adults=options["numberInParty"],
                    captured_at=utcnow(),
                    source_kind="LIVE",
                )
                if not future.done():
                    future.set_result(records)
            except (BrowserError, ValueError, TypeError, KeyError, TransientFailure) as exc:
                if not future.done():
                    future.set_exception(
                        exc
                        if isinstance(exc, (MarriottSampleError, TransientFailure))
                        else TransientFailure("Marriott price response could not be read")
                    )

        def on_response(response):
            nonlocal denial
            if not is_official_request("marriott", response.request):
                return
            parsed = urlsplit(response.url)
            event = {
                "host": parsed.hostname,
                "path": parsed.path,
                "status": response.status,
                "resource_type": response.request.resource_type,
            }
            self.diagnostics["responses"].append(event)
            if response.status in (403, 429):
                denial = denial or (
                    BrandBlocked("Marriott returned HTTP 403")
                    if response.status == 403
                    else RateLimited(retry_after(response.headers.get("retry-after")))
                )
                if not future.done():
                    future.set_exception(denial)
                return
            if (
                future.done()
                or not accept_prices
                or parsed.scheme != "https"
                or parsed.netloc != "www.marriott.com"
                or parsed.path != RATES_PATH
                or response.request.method != "POST"
            ):
                return
            try:
                payload = response.request.post_data_json
                search = payload["variables"]["search"]
                options = search["options"]
                if payload.get("operationName") != RATES_OPERATION:
                    return
                event["operation"] = RATES_OPERATION
                event["requested_hotel"] = search.get("propertyId")
                event["requested_dates"] = [options.get("startDate"), options.get("endDate")]
                if (search["propertyId"], options["startDate"], options["endDate"]) != (
                    code,
                    str(ci),
                    str(co),
                ):
                    return
                if (
                    type(options["quantity"]) is not int
                    or options["quantity"] != 1
                    or type(options["numberInParty"]) is not int
                    or options["numberInParty"] != self.adults
                    or options["childAges"] != []
                ):
                    raise MarriottSampleError("Live page occupancy differs from the requested stay")
            except MarriottSampleError as exc:
                if not future.done():
                    future.set_exception(exc)
                return
            except (BrowserError, TypeError, KeyError, ValueError):
                return
            self.diagnostics["stage"] = "PRICE_RESPONSE"
            task = asyncio.create_task(read_prices(response, options))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        def on_failure(request):
            if is_official_request("marriott", request):
                parsed = urlsplit(request.url)
                self.diagnostics["request_failures"].append(
                    {"host": parsed.hostname, "path": parsed.path, "error": request.failure}
                )

        async def check_access():
            if denial:
                raise denial
            if await visible_challenge(self._page):
                raise BrandBlocked("Marriott visible access denial or challenge")

        async def watch():
            while not future.done():
                try:
                    await check_access()
                except (BrandBlocked, RateLimited) as exc:
                    if not future.done():
                        future.set_exception(exc)
                    return
                except BrowserError:
                    if self._page.is_closed():
                        if not future.done():
                            future.set_exception(TransientFailure("Marriott collection page closed"))
                        return
                await asyncio.sleep(0.25)

        context.on("response", on_response)
        context.on("requestfailed", on_failure)
        try:
            if self._configure_context:
                await self._configure_context(context)
            self._page = page = context.pages[0] if context.pages else await context.new_page()
            timeout = self.settings.navigation_timeout_ms
            page.set_default_timeout(timeout)
            watcher = asyncio.create_task(watch())
            action_task = asyncio.create_task(self._homepage_hotel_dates(code, ci, co, name, before_submit))
            _, records = await asyncio.wait_for(asyncio.gather(action_task, future), timeout / 1000 * 6)
            await check_access()
            self.diagnostics["stage"] = "COMPLETE"
            return records
        except (BrowserError, asyncio.TimeoutError):
            if denial:
                raise denial
            if page is not None and not page.is_closed():
                try:
                    await check_access()
                except BrowserError:
                    pass
            raise TransientFailure("Marriott public page did not produce a verified price response") from None
        finally:
            # Stop the form workflow before any diagnostic inspection can await.
            for task in [action_task, watcher, *tasks]:
                if task is not None and not task.done():
                    task.cancel()
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()
            await asyncio.gather(
                *[t for t in [action_task, watcher, *tasks] if t is not None], return_exceptions=True
            )
            page = self._page
            if page is not None and not page.is_closed():
                parsed = urlsplit(page.url)
                self.diagnostics["page"] = {"host": parsed.hostname, "path": parsed.path}
                if denial:
                    self.diagnostics["title"] = "SKIPPED_AFTER_DENIAL"
                else:
                    try:
                        self.diagnostics["title"] = (await asyncio.wait_for(page.title(), 2))[:180]
                    except (BrowserError, asyncio.TimeoutError):
                        self.diagnostics["title"] = "UNAVAILABLE"
            try:
                await self.close()
            except BrowserError:
                pass
            # A delayed navigation response can arrive during title inspection
            # or context teardown, after the initial timeout handler ran. It must
            # still reach BrandRunner as a denial, never as an ordinary timeout.
            if denial:
                raise denial

    async def close(self):
        try:
            if self._context:
                try:
                    await self._context.close()
                except BrowserError:
                    pass
        finally:
            self._browser = None
            self._context = self._page = None
            try:
                if self._playwright:
                    await self._playwright.stop()
            finally:
                self._playwright = None
                if self._profile_lock is not None:
                    self._profile_lock.__exit__(None, None, None)
                    self._profile_lock = None
