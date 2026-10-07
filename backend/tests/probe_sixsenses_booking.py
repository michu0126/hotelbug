"""Continue one observed public hotel into BOOK NOW; no API/booking/price writes."""

import argparse
import asyncio
import json
import re
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as BrowserTimeout
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models.tables import AppSetting, ProviderStatus, utcnow
from app.providers.browser_session import close_browser_sessions, new_provider_page

STAGE = "audit:six-senses-booking-controls"


def public_resource_url(value):
    parsed = urlparse(value)
    return {"hostname": parsed.hostname, "path": parsed.path}


def public_failure_type(value):
    # Chromium network error identifiers contain no URL or user data. Do not
    # persist arbitrary failure text, which can include credentials or queries.
    if isinstance(value, str) and re.fullmatch(r"net::ERR_[A-Z_]+", value):
        return value
    return "UNKNOWN"


def install_script_diagnostics(page, events):
    def failed(request):
        if request.resource_type == "script":
            events.append(
                {
                    "event": "SCRIPT_FAILED",
                    **public_resource_url(request.url),
                    "failure_type": public_failure_type(request.failure),
                }
            )

    def response_received(response):
        if response.request.resource_type == "script" and response.status >= 400:
            events.append(
                {"event": "SCRIPT_HTTP_ERROR", "status": response.status, **public_resource_url(response.url)}
            )

    def page_error(error):
        # Public script errors can contain URL queries. Keep only the error's
        # first line, remove URLs altogether, and never read request headers.
        lines = str(error).splitlines()
        message = re.sub(r"https?://\S+", "[URL]", lines[0] if lines else type(error).__name__)[:220]
        events.append({"event": "PAGE_SCRIPT_ERROR", "message": message})

    page.on("requestfailed", failed)
    page.on("response", response_received)
    page.on("pageerror", page_error)


async def public_controls(page):
    return await page.locator("h1,h2,button,input,select,a,iframe").evaluate_all(
        """es => es.filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
            .filter(e => e.tagName !== 'A' || /(^|[^a-z])(book|reserve|reservation|reservations|availability|available)([^a-z]|$)/i.test(e.innerText))
            .map(e => ({tag: e.tagName, text: (e.innerText || '').trim().slice(0, 180),
                id: e.id, class: e.className, type: e.getAttribute('type'), name: e.getAttribute('name'),
                placeholder: e.getAttribute('placeholder'), aria_label: e.getAttribute('aria-label'),
                frame_host: e.tagName === 'IFRAME' && e.src ? new URL(e.src).hostname : null,
                frame_path: e.tagName === 'IFRAME' && e.src ? new URL(e.src).pathname : null}))"""
    )


async def read_booking_controls(page, url, settle=False):
    response = await page.goto(url, wait_until="commit", timeout=45000)
    if response and response.status in (401, 403, 429):
        return {"outcome": "ACCESS_REJECTED", "status": response.status}
    await page.locator("h1").first.wait_for(state="visible", timeout=30000)
    title = (await page.title()).casefold()
    if any(word in title for word in ("access denied", "just a moment")):
        return {"outcome": "ACCESS_REJECTED"}
    if settle:
        await page.wait_for_load_state("domcontentloaded", timeout=30000)
        # This exact consent button was visible in the first real observation.
        consent = page.locator("#truste-consent-button")
        if await consent.is_visible():
            await consent.click()
    links = page.locator("a").filter(has_text=re.compile(r"^\s*BOOK NOW\s*$", re.I))
    visible = [link for link in await links.all() if await link.is_visible()]
    initial = await public_controls(page)
    if len(visible) != 1:
        return {"outcome": "BOOK_NOW_NOT_UNIQUE", "visible_ctas": len(visible), "controls": initial}
    target = page
    clicked = False
    try:
        try:
            async with page.expect_popup(timeout=5000) as popup:
                await visible[0].click()
                clicked = True
            target = await popup.value
            await target.wait_for_load_state("domcontentloaded", timeout=30000)
        except BrowserTimeout:
            # The actual BOOK NOW can open an inline dialog instead of a tab.
            # This is not permission to click a second time.
            if not clicked:
                raise
        parsed = urlparse(target.url)
        if parsed.scheme != "https" or parsed.hostname not in {
            "www.sixsenses.com",
            "sixsenses.com",
            "be.synxis.com",
        }:
            return {"outcome": "DESTINATION_UNVERIFIED", "hostname": parsed.hostname}
        final_title = (await target.title()).casefold()
        if any(word in final_title for word in ("access denied", "just a moment")):
            return {"outcome": "ACCESS_REJECTED"}
        if settle:
            try:
                await target.wait_for_function(
                    """() => [...document.querySelectorAll('input,iframe')].some(e =>
                        !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden' &&
                        (/date|arrival|depart|check.?in|check.?out/i.test([e.id, e.name, e.getAttribute('placeholder')].join(' ')) ||
                        e.tagName === 'IFRAME' && /synxis/.test(e.src)))""",
                    timeout=15000,
                )
            except BrowserTimeout:
                return {"outcome": "BOOKING_FORM_NOT_VISIBLE", "controls": await public_controls(target)}
        return {
            "outcome": "BOOKING_CONTROLS_READ",
            "public_url": parsed._replace(query="", fragment="").geturl(),
            "controls": await public_controls(target),
        }
    finally:
        if target is not page:
            await target.close()


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--diagnose-scripts",
        action="store_true",
        help="One failed-form follow-up: public script load/errors only, no API capture",
    )
    parser.add_argument(
        "--settle-controls",
        action="store_true",
        help="Next observed phase: DOM-ready and the actual cookie-consent button before BOOK NOW",
    )
    parser.add_argument(
        "--retry-fixed-title-read",
        action="store_true",
        help="One recorded TypeError title-read code-fix attempt only",
    )
    args = parser.parse_args()
    database = args.state_dir.resolve() / "catalog-audit.sqlite"
    assert database.is_file(), "Missing isolated directory audit database"
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    assert not (args.diagnose_scripts and args.retry_fixed_title_read), "Choose one diagnostic phase"
    stage = (
        STAGE + ":resource-diagnostics"
        if args.diagnose_scripts
        else STAGE + ":loaded-form"
        if args.settle_controls
        else STAGE
    )
    try:
        async with sessions() as db, db.begin():
            marker = await db.get(AppSetting, "audit:directory-only")
            assert marker and marker.value == {"provider": "ihg"}, "Not the isolated IHG audit database"
            existing = await db.get(AppSetting, stage)
            fixed = await db.get(AppSetting, STAGE + ":fixed-title")
            state = await db.get(ProviderStatus, "ihg")
            now = utcnow()
            if args.diagnose_scripts:
                previous = await db.get(AppSetting, STAGE + ":loaded-form")
                assert previous and previous.value.get("outcome") == "BOOKING_FORM_NOT_VISIBLE", (
                    "Only diagnose the recorded failed form"
                )
            if args.settle_controls:
                previous = await db.get(AppSetting, STAGE)
                assert previous and previous.value.get("outcome") == "BOOKING_CONTROLS_READ", (
                    "Requires the initial observed public controls"
                )
            if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                print(json.dumps({"outcome": "ALREADY_OBSERVED_OR_PROVIDER_PAUSED"}), flush=True)
                return
            if existing:
                if not (
                    args.retry_fixed_title_read
                    and not fixed
                    and existing.value.get("outcome") == "BOOKING_NAVIGATION_FAILED"
                    and existing.value.get("error_type") == "TypeError"
                ):
                    print(json.dumps({"outcome": "ALREADY_OBSERVED_OR_PROVIDER_PAUSED"}), flush=True)
                    return
                db.add(
                    AppSetting(
                        key=STAGE + ":fixed-title", value={"original": existing.value, "at": now.isoformat()}
                    )
                )
            sources = (
                await db.scalars(
                    select(AppSetting).where(AppSetting.key.startswith("audit:six-senses-public-link:"))
                )
            ).all()
            source = next(
                (item.value for item in sources if item.value.get("outcome") == "PUBLIC_PAGE_READ"), None
            )
            assert source, "No successfully observed official hotel link"
            url = source["public_url"]
            parsed = urlparse(url)
            assert parsed.scheme == "https" and parsed.hostname == "www.sixsenses.com" and not parsed.query
            start = {"outcome": "STARTED", "hotel_name": source["hotel_name"], "at": now.isoformat()}
            if existing:
                existing.value = start
            else:
                db.add(AppSetting(key=stage, value=start))
        settings = Settings(_env_file=None)
        page = await new_provider_page(
            "ihg",
            proxy_server=args.proxy,
            browser_channel="chrome",
            headless=False,
            session_dir=settings.browser_session_dir,
        )
        events = []
        if args.diagnose_scripts:
            install_script_diagnostics(page, events)
        try:
            report = await read_booking_controls(
                page, url, settle=args.settle_controls or args.diagnose_scripts
            )
        except Exception as error:
            report = {"outcome": "BOOKING_NAVIGATION_FAILED", "error_type": type(error).__name__}
        report["hotel_name"] = source["hotel_name"]
        if args.diagnose_scripts:
            report["script_diagnostics"] = events
        if report["outcome"] == "ACCESS_REJECTED":
            report["blocked_until"] = (utcnow() + timedelta(hours=6)).isoformat()
        async with sessions() as db, db.begin():
            (await db.get(AppSetting, stage)).value = report
        print(json.dumps(report, ensure_ascii=True), flush=True)
    finally:
        await close_browser_sessions()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
