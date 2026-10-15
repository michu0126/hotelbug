"""Inspect one public calendar from an automatically discovered audit hotel.

No room search, bookings, authentication, database writes or notifications.
Only rendered public controls are recorded; browser state is not exported.
"""

import argparse
import asyncio
import json
import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.tables import AppSetting, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_catalog import ROOT
from app.services.ihg_catalogs import selected_directory_root

MONTHS = dict(zip("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), range(1, 13), strict=True))
GUESTS = re.compile(r"^1 间客房,\s*(\d+) 宾客$")
SNAPSHOT_SCRIPT = r"""() => {
  const visible = e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden';
  const text = e => e.innerText?.trim() || e.textContent?.trim() || '';
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const label = e => e.getAttribute('aria-label') ||
    (e.getAttribute('aria-labelledby') || '').split(/\s+/).map(id => document.getElementById(id))
      .filter(Boolean).map(text).join(' ') || [...(e.labels || [])].map(text).join(' ');
  return {
    body_text: document.body.innerText,
    headings: all('h1,h2,h3').map(text),
    buttons: all('button,[role=button]').map(e => ({
      text:text(e), label:label(e), role:e.getAttribute('role') || 'button',
      disabled:e.disabled === true || e.getAttribute('aria-disabled') === 'true'
    })),
    cells: all('[role=gridcell]').map(e => ({
      label:e.getAttribute('aria-label'), text:text(e),
      disabled:e.getAttribute('aria-disabled') === 'true',
      outside:e.classList.contains('DayPicker-Day--outside'),
      price_text:[...e.querySelectorAll('.price-section')].filter(visible).map(text).join(' '),
      button_label:e.querySelector('[role=button]')?.getAttribute('aria-label') || ''
    })),
    labels: all('label').map(text),
    search: all('form,[role=search]').map(text),
    month_captions: all('.DayPicker-Caption').map(text),
    currency_controls: all('select,[role=combobox]').map(e => ({
      label:label(e), text:e.tagName === 'SELECT' ? [...e.selectedOptions].map(text).join(' ') : text(e)
    })),
    busy: all('[aria-busy=true],[role=progressbar]').length > 0
  };
}"""


def cell_date(label):
    match = re.fullmatch(r"[A-Za-z]{3} ([A-Za-z]{3}) (\d{1,2}) (\d{4})", label or "")
    if not match or match[1] not in MONTHS:
        raise ValueError("Calendar cell has no explicit public date label")
    return date(int(match[3]), MONTHS[match[1]], int(match[2]))


def calendar_summary(snapshot):
    """Keep raw public amount labels as hints, without guessing currency/tax basis."""
    cells = [cell for cell in snapshot["cells"] if not cell.get("outside")]
    months = {cell_date(cell["label"]).strftime("%Y-%m") for cell in cells}
    if len(months) != 1:
        raise ValueError("Calendar must show one unambiguous current month")
    buttons = snapshot["buttons"]
    date_labels = [button["text"] for button in buttons if button["text"].startswith("日期")]
    occupancy = [button["text"] for button in buttons if GUESTS.fullmatch(button["text"])]
    currency = [
        value
        for value in [
            *snapshot.get("labels", []),
            *(button.get("label", "") + " " + button["text"] for button in buttons),
            *(control.get("label", "") + " " + control["text"] for control in snapshot["currency_controls"]),
        ]
        if re.search(r"货币|貨幣|币种|Currency|USD|CNY|美元|人民币", value, re.I)
    ]
    hints = [
        {
            "check_in": cell_date(cell["label"]).isoformat(),
            "displayed_amount": cell["price_text"],
            "public_label": cell.get("button_label", ""),
        }
        for cell in cells
        if cell.get("price_text") and not cell.get("disabled")
    ]
    return {
        "calendar_month": next(iter(months)),
        "date_labels": date_labels,
        "occupancy_labels": occupancy,
        "currency_labels": list(dict.fromkeys(currency)),
        "priced_hint_days": len(hints),
        "hints": hints,
        "price_kind": "unverified_calendar_hint",
    }


class PageActivity:
    """Observe completion/status only, never export requests or browser state."""

    def __init__(self):
        self.pending = set()
        self.started = 0
        self.last_change = time.monotonic()
        self.rejected_status = None

    @staticmethod
    def relevant(request):
        return urlparse(request.url).hostname == "www.marriott.com.cn" and request.resource_type in {
            "document",
            "xhr",
            "fetch",
        }

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
        page.on("request", self.request)
        page.on("requestfinished", self.finished)
        page.on("requestfailed", self.finished)
        page.on("response", self.response)


async def settled_calendar(provider, page, activity, *, expected_month=None, after_request=None):
    """Wait for dated, populated cells plus completed normal page requests.

    A changed month heading alone can precede the replacement price cells. The
    fingerprint includes all visible dates/prices, occupancy and date labels.
    """
    deadline = time.monotonic() + 60
    fingerprint = None
    unchanged_since = time.monotonic()
    while time.monotonic() < deadline:
        await provider._check_access(page, activity.rejected_status)
        parsed = urlparse(page.url)
        if parsed.hostname != "www.marriott.com.cn" or parsed.path != "/search/availabilityCalendar.mi":
            raise ValueError("Diagnostic must remain on the official public calendar")
        snapshot = await page.evaluate(SNAPSHOT_SCRIPT)
        try:
            summary = calendar_summary(snapshot)
        except ValueError:
            summary = None
        current = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
        if current != fingerprint:
            fingerprint, unchanged_since = current, time.monotonic()
        cells_ready = all(
            cell.get("disabled") or cell.get("price_text")
            for cell in snapshot["cells"]
            if not cell.get("outside")
        )
        if (
            summary
            and summary["occupancy_labels"] == ["1 间客房, 2 宾客"]
            and any(re.search(r"日期\s*\(1\s*晚\)", label) for label in summary["date_labels"])
            and (expected_month is None or summary["calendar_month"] == expected_month)
            and summary["priced_hint_days"] > 0
            and cells_ready
            and not snapshot["busy"]
            and not activity.pending
            and (after_request is None or activity.started > after_request)
            and time.monotonic() - max(unchanged_since, activity.last_change) >= 1.5
        ):
            return {"summary": summary, "rendered": snapshot}
        await asyncio.sleep(0.25)
    raise TimeoutError("Two-adult calendar month and visible price hints did not settle")


async def set_two_adults(page, activity):
    guests = page.get_by_role("button", name=GUESTS)
    label = await guests.inner_text()
    current = int(GUESTS.fullmatch(label)[1])
    if current == 2:
        return None
    await guests.click()
    action = "添加 成人" if current < 2 else "删除 成人"
    for _ in range(abs(current - 2)):
        await page.get_by_role("button", name=re.compile(action + "$")).click()
    before = activity.started
    await page.get_by_role("button", name="更新搜索", exact=True).click()
    await page.get_by_role("button", name="1 间客房, 2 宾客", exact=True).wait_for()
    return before


async def select_audit_hotel(sessions):
    async with sessions() as db:
        marker = await db.get(AppSetting, "audit:directory-only")
        catalog = await db.get(AppSetting, "catalog:marriott")
        if not marker or marker.value != {"provider": "marriott"}:
            raise ValueError("Expected isolated Marriott audit marker")
        if not catalog or selected_directory_root(catalog.value, "marriott") != ROOT:
            raise ValueError("Expected accepted CN directory source")
        state = await db.get(ProviderStatus, "marriott")
        now = utcnow()
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            raise ValueError("Marriott provider remains paused until " + state.blocked_until.isoformat())
        hotel = await db.scalar(
            select(Hotel)
            .where(
                Hotel.provider == "marriott",
                Hotel.active.is_(True),
                Hotel.official_url.like("https://www.marriott.com.cn/%"),
                ~select(PriceHistory.id).where(PriceHistory.hotel_id == Hotel.id).exists(),
            )
            .order_by(Hotel.created_at, Hotel.id)
            .limit(1)
        )
        if hotel is None:
            raise ValueError("No unobserved discovered Marriott hotel")
        return {"code": hotel.provider_hotel_id, "name": hotel.hotel_name, "official_url": hotel.official_url}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--max-months", type=int, choices=(1, 2, 3), default=2)
    args = parser.parse_args()
    if not args.database.is_file() or args.report.exists():
        parser.error("Use an existing isolated catalog database and a new report")
    engine = create_async_engine("sqlite+aiosqlite:///" + args.database.resolve().as_posix())
    sessions = async_sessionmaker(engine)
    provider = MarriottBrowserProvider(browser_channel="chrome", session_dir=args.session_dir)
    report = {
        "status": "RUNNING",
        "database_writes": 0,
        "room_quote_requests": 0,
        "conditions": {"rooms": 1, "adults": 2, "nights": 1},
        "months": [],
    }
    try:
        report["hotel"] = await select_audit_hotel(sessions)
        print(json.dumps({"stage": "public_calendar", "hotel": report["hotel"]}), flush=True)
        provider.configure_hotel_url(report["hotel"]["official_url"])
        async with asyncio.timeout(300):
            page = await provider._get_page()
            activity = PageActivity()
            activity.attach(page)
            page = await provider._open_hotel(report["hotel"]["code"])
            await page.get_by_role("gridcell").first.wait_for(state="visible", timeout=30000)
            after_request = await set_two_adults(page, activity)
            capture = await settled_calendar(provider, page, activity, after_request=after_request)
            for index in range(args.max_months):
                report["months"].append(capture)
                print(
                    json.dumps({"stage": "calendar_hints", **capture["summary"]}, ensure_ascii=False),
                    flush=True,
                )
                if index + 1 == args.max_months:
                    break
                year, month = map(int, capture["summary"]["calendar_month"].split("-"))
                expected = f"{year + (month == 12):04d}-{month % 12 + 1:02d}"
                following = page.get_by_role("button", name="Next Month", exact=True)
                if not await following.is_enabled():
                    raise ValueError("Next Month is unavailable; cannot complete requested diagnostic")
                await following.click()
                capture = await settled_calendar(provider, page, activity, expected_month=expected)
            report["status"] = "SUCCEEDED"
    except BaseException as exc:
        report.update(status="FAILED", error_type=type(exc).__name__)
        raise
    finally:
        try:
            await provider.close()
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        except BaseException:
            report.update(status="FAILED", cleanup_status="FAILED")
            raise
        finally:
            await engine.dispose()
            with args.report.open("x", encoding="utf-8") as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
            print(
                json.dumps({k: report.get(k) for k in ("status", "error_type", "cleanup_status")}), flush=True
            )


if __name__ == "__main__":
    asyncio.run(main())
