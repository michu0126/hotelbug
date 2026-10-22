"""Dated hints from the ordinary Marriott CN monthly cash-price calendar.

The public calendar can contain member/pre-tax minima. These observations are
not room offers, availability results, or independently verified alerts. The
controls and date-cell structure were observed on 2026-10-14 (PBHMD).
"""

import asyncio
import calendar
import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott import hotel_code
from app.schemas.calendar_hints import CalendarHintBatch, CalendarHintDay, CalendarHintRequest

ORIGIN = "https://www.marriott.com.cn"
PATH = "/search/availabilityCalendar.mi"
MONTHS = dict(zip("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), range(1, 13), strict=True))
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()
GUESTS = re.compile(r"^1 间客房,\s*(\d+) 宾客$")
TOTAL = re.compile(r"(?:客房费用总额\s*)?1\s*晚客房总费用\s*\(([A-Z]{3})\)")
AMOUNT = re.compile(
    r"(?P<prefix>[A-Z]{3}|[$€£¥₹])?\s*(?P<number>(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)(?:\.\d{1,2})?)"
)

# Only visible public controls and their current native checked state. No
# framework stores, request bodies, browser cookies, or account data are read.
SNAPSHOT_SCRIPT = r"""() => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const text = e => (e.innerText || e.textContent || '').trim();
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const rewards = document.querySelector('input[aria-label="useRewardsPoints"]');
  return {
    buttons: all('button,[role=button]').map(e => ({
      text:text(e), label:e.getAttribute('aria-label') || '',
      disabled:e.disabled === true || e.getAttribute('aria-disabled') === 'true'
    })),
    cells: all('[role=gridcell]').map(e => ({
      label:e.getAttribute('aria-label'),
      disabled:e.getAttribute('aria-disabled') === 'true',
      outside:e.classList.contains('DayPicker-Day--outside'),
      price_text:[...e.querySelectorAll('.price-section')].filter(visible).map(text).join(' ')
    })),
    rewards_selected:rewards ? rewards.checked : null,
    hotels:all('h3.hotel-name').map(e => ({
      name:text(e), href:e.closest('a')?.getAttribute('href') || ''
    })),
    busy:all('[aria-busy=true],[role=progressbar]').length > 0
  };
}"""


@dataclass(frozen=True)
class CalendarMonth:
    first_day: date
    currency: str
    days: tuple[CalendarHintDay, ...]


def cell_date(label: str) -> date:
    match = re.fullmatch(r"([A-Za-z]{3}) ([A-Za-z]{3}) (\d{1,2}) (\d{4})", label or "")
    if not match or match[2] not in MONTHS:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott calendar date label is missing")
    try:
        result = date(int(match[4]), MONTHS[match[2]], int(match[3]))
    except ValueError as exc:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar date is invalid") from exc
    if match[1] != WEEKDAYS[result.weekday()]:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar weekday and date disagree")
    return result


def parse_month(snapshot: dict, request: CalendarHintRequest) -> CalendarMonth:
    """Validate the complete current-month grid, not the stale search summary."""
    labels = [button.get("text", "").strip() for button in snapshot.get("buttons", [])]
    # The calendar observed on 2026-10-15 has no rewards checkbox at all.
    # Its explicit one-night ISO currency total establishes cash; a present
    # selected rewards control still contradicts that evidence.
    if snapshot.get("rewards_selected") is not False and snapshot.get("rewards_selected") is not None:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott cash calendar rewards state is not off")
    if labels.count("常规优惠房价") != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar is not a regular-rate search")
    dates = [label for label in labels if label.startswith("日期")]
    if len(dates) != 1 or not re.match(r"^日期\s*\(1\s*晚\)(?:\s|$)", dates[0]):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar is not a one-night search")
    occupancies = [label for label in labels if GUESTS.fullmatch(label)]
    if occupancies != [f"1 间客房, {request.adults} 宾客"] or request.rooms != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar occupancy does not match")
    totals = [TOTAL.fullmatch(label) for label in labels if "晚客房总费用" in label]
    if len(totals) != 1 or totals[0] is None:
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED, "Marriott calendar explicit total currency is missing"
        )
    currency = totals[0][1]
    hotels = snapshot.get("hotels", [])
    if (
        len(hotels) != 1
        or not hotels[0].get("name", "").strip()
        or not hotels[0].get("href", "").startswith(f"/hotels/travel/{request.provider_hotel_id.lower()}-")
    ):
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "Marriott calendar rendered hotel identity does not match"
        )
    cells = [cell for cell in snapshot.get("cells", []) if not cell.get("outside")]
    dated_cells = [(cell_date(cell.get("label", "")), cell) for cell in cells]
    months = {day.replace(day=1) for day, _ in dated_cells}
    if len(months) != 1:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott current calendar month is ambiguous")
    first_day = next(iter(months))
    expected = {
        date(first_day.year, first_day.month, day)
        for day in range(1, calendar.monthrange(first_day.year, first_day.month)[1] + 1)
    }
    if len(dated_cells) != len(expected) or {day for day, _ in dated_cells} != expected:
        raise ProviderError(
            ErrorCode.PROVIDER_CHANGED, "Marriott calendar month has missing or duplicate days"
        )
    hints = []
    for day, cell in sorted(dated_cells, key=lambda pair: pair[0]):
        if cell.get("disabled") is True:
            continue  # Disabled does not prove sold out; no availability is emitted.
        match = AMOUNT.fullmatch(str(cell.get("price_text", "")).strip())
        if not match or (match["prefix"] and len(match["prefix"]) == 3 and match["prefix"] != currency):
            raise ProviderError(
                ErrorCode.PROVIDER_CHANGED, "Marriott calendar amount is missing or ambiguous"
            )
        amount = Decimal(match["number"].replace(",", ""))
        if amount <= 0:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar amount must be positive")
        hints.append(CalendarHintDay(check_in=day, displayed_price=amount))
    if not hints:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott month has no confirmed cash-price hints")
    return CalendarMonth(first_day=first_day, currency=currency, days=tuple(hints))


class PageActivity:
    """Observe ordinary public-page completion and access denials only."""

    def __init__(self):
        self.pending = set()
        self.started = 0
        self.last_change = time.monotonic()
        self.rejected_status = None
        self._listeners = {
            "request": self.request,
            "requestfinished": self.finished,
            "requestfailed": self.finished,
            "response": self.response,
        }

    @staticmethod
    def relevant(request) -> bool:
        parsed = urlparse(request.url)
        return (
            parsed.scheme == "https"
            and parsed.netloc == "www.marriott.com.cn"
            and request.resource_type in {"document", "xhr", "fetch"}
        )

    def request(self, request):
        if self.relevant(request):
            self.pending.add(id(request))
            self.started += 1
            self.last_change = time.monotonic()

    def finished(self, request):
        if id(request) in self.pending:
            self.pending.discard(id(request))
            self.last_change = time.monotonic()

    def response(self, response):
        if self.relevant(response.request) and response.status in (403, 429):
            self.rejected_status = response.status

    def attach(self, page):
        for event, listener in self._listeners.items():
            page.on(event, listener)

    def detach(self, page):
        for event, listener in self._listeners.items():
            page.remove_listener(event, listener)


def verify_calendar_url(url: str, code: str, *, allow_missing_code: bool = False) -> None:
    parsed = urlparse(url)
    codes = parse_qs(parsed.query, keep_blank_values=True).get("propertyCode")
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.marriott.com.cn"
        or parsed.path != PATH
        or (codes != [code] and not (allow_missing_code and codes is None))
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott calendar URL or property does not match")


async def settled_calendar(provider, page, activity, request, *, expected_month=None, after_request=None):
    deadline = time.monotonic() + 60
    fingerprint = None
    unchanged_since = time.monotonic()
    last_error = None
    snapshot = {}
    parsed = None
    while time.monotonic() < deadline:
        await provider._check_access(page, activity.rejected_status)
        # Normal Update Search submits a form and removes propertyCode from
        # this URL. The visible hotel heading/link is checked in parse_month.
        verify_calendar_url(page.url, hotel_code(request.provider_hotel_id), allow_missing_code=True)
        snapshot = await page.evaluate(SNAPSHOT_SCRIPT)
        if snapshot.get("rewards_selected") is True:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott rewards calendar is selected")
        current = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        if current != fingerprint:
            fingerprint, unchanged_since = current, time.monotonic()
        try:
            parsed = parse_month(snapshot, request)
            last_error = None
        except ProviderError as exc:
            parsed, last_error = None, exc
        if (
            parsed
            and (expected_month is None or parsed.first_day == expected_month)
            and not snapshot.get("busy")
            and not activity.pending
            and (after_request is None or activity.started > after_request)
            and time.monotonic() - max(unchanged_since, activity.last_change) >= 1.5
        ):
            return parsed, snapshot
        await asyncio.sleep(0.25)
    # Preserve a stable, populated page's actual validation failure. Previously
    # every parser error was hidden behind TIMEOUT and could be blindly retried.
    loading = calendar_loading_reason(snapshot)
    if (
        last_error
        and not loading
        and not activity.pending
        and time.monotonic() - max(unchanged_since, activity.last_change) >= 1.5
        and (after_request is None or activity.started > after_request)
    ):
        raise ProviderError(
            last_error.code, f"Marriott calendar validation failed: {last_error}"
        ) from last_error
    if not loading:
        if activity.pending:
            loading = "official page requests still pending"
        elif after_request is not None and activity.started <= after_request:
            loading = "guest update request not observed"
        elif parsed and expected_month and parsed.first_day != expected_month:
            loading = "requested month not displayed"
        else:
            loading = "calendar did not become stable"
    raise ProviderError(
        ErrorCode.TIMEOUT, f"Marriott dated cash calendar did not finish updating: {loading}"
    ) from last_error


def calendar_loading_reason(snapshot: dict) -> str | None:
    """Recognize observed placeholders, never infer sold-out from an empty grid.

    Return only fixed diagnostic labels; do not persist page text, URL query
    strings or account/session data in the task error.
    """
    if snapshot.get("busy"):
        return "page reports loading"
    hotels = snapshot.get("hotels", [])
    if not hotels or any(
        not hotel.get("name", "").strip() or hotel.get("href", "") in ("", "/hotels/travel/")
        for hotel in hotels
    ):
        return "rendered hotel identity not loaded"
    if not any("晚客房总费用" in button.get("text", "") for button in snapshot.get("buttons", [])):
        return "cash total currency not loaded"
    cells = [cell for cell in snapshot.get("cells", []) if not cell.get("outside")]
    enabled = [cell for cell in cells if cell.get("disabled") is not True]
    if not enabled:
        return "no confirmed priced dates (not a sold-out result)"
    if any(not str(cell.get("price_text", "")).strip() for cell in enabled):
        return "date prices not loaded"
    return None


async def set_adults(page, activity, adults: int):
    guests = page.get_by_role("button", name=GUESTS)
    label = await guests.inner_text()
    match = GUESTS.fullmatch(label)
    if not match:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott calendar guest control is unreadable")
    current = int(match[1])
    if current == adults:
        return None
    await guests.click()
    action = "添加 成人" if current < adults else "删除 成人"
    for _ in range(abs(current - adults)):
        await page.get_by_role("button", name=re.compile(action + "$")).click()
    before = activity.started
    await page.get_by_role("button", name="更新搜索", exact=True).click()
    await page.get_by_role("button", name=f"1 间客房, {adults} 宾客", exact=True).wait_for()
    return before


def next_month(first_day: date) -> date:
    return date(first_day.year + (first_day.month == 12), first_day.month % 12 + 1, 1)


async def collect_calendar_hints(provider, request: CalendarHintRequest) -> CalendarHintBatch:
    """Walk normal month controls; never open a booking or turn hints into rates."""
    if provider._root != ORIGIN:
        raise ProviderError(
            ErrorCode.NOT_IMPLEMENTED, "Calendar hints require the selected Marriott CN source"
        )
    if request.rooms != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Marriott calendar hints currently require one room")
    code = hotel_code(request.provider_hotel_id)
    page = await provider._get_page()
    activity = PageActivity()
    activity.attach(page)
    stage = "hotel identity readiness"
    try:
        opened = await provider._open_hotel(code)
        if opened is not page:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott changed the observed calendar page")
        entry_url = page.url
        verify_calendar_url(entry_url, code)
        stage = "calendar grid readiness"
        await page.get_by_role("gridcell").first.wait_for(state="visible", timeout=30000)
        stage = "guest controls"
        after_request = await set_adults(page, activity, request.adults)
        stage = "dated cash calendar"
        month, snapshot = await settled_calendar(
            provider, page, activity, request, after_request=after_request
        )
        if month.first_day > request.start_date.replace(day=1):
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Marriott requested calendar precedes displayed month"
            )
        currency, hints, snapshots = month.currency, [], []
        for navigation in range(14):  # Initial month plus at most 13 observed forward controls.
            if month.currency != currency:
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "Marriott currency changed between calendar months"
                )
            if next_month(month.first_day) > request.start_date:
                hints.extend(
                    day for day in month.days if request.start_date <= day.check_in < request.end_date
                )
                snapshots.append(snapshot)
            if next_month(month.first_day) >= request.end_date:
                break
            if navigation == 13:
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "Marriott calendar exceeded one-year navigation bound"
                )
            following = page.get_by_role("button", name="Next Month", exact=True)
            if not await following.is_enabled():
                raise ProviderError(
                    ErrorCode.BOOKING_WINDOW_CLOSED, "Marriott next calendar month is unavailable"
                )
            expected = next_month(month.first_day)
            await following.click()
            month, snapshot = await settled_calendar(
                provider, page, activity, request, expected_month=expected
            )
        if not hints:
            raise ProviderError(
                ErrorCode.PROVIDER_CHANGED, "Marriott requested range has no cash-price hints"
            )
        return CalendarHintBatch(
            provider="marriott",
            provider_hotel_id=code,
            start_date=request.start_date,
            end_date=request.end_date,
            adults=request.adults,
            rooms=request.rooms,
            currency=currency,
            basis="CALENDAR_MINIMUM_TAX_UNCONFIRMED",
            # The live page rewrites currency to "default" and later drops
            # propertyCode after the guest form. Keep the actual hotel-specific
            # public entry point used by _open_hotel as the stable history link.
            source_url=f"{ORIGIN}{PATH}?isRateCalendar=true&propertyCode={code}&isSearch=true&currency=",
            captured_at=datetime.now(timezone.utc),
            raw_response_hash=hashlib.sha256(
                json.dumps(snapshots, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            days=hints,
        )
    except PlaywrightError as exc:
        await provider._check_access(page, activity.rejected_status)
        raise ProviderError(
            ErrorCode.TIMEOUT if isinstance(exc, PlaywrightTimeout) else ErrorCode.NETWORK_ERROR,
            f"Marriott public calendar could not be read at {stage}",
        ) from exc
    finally:
        activity.detach(page)
