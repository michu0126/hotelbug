"""Inspect Hilton's public homepage search; never submits a reservation."""

import argparse
import asyncio
import json
import re
from datetime import date

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--query", default="London")
    parser.add_argument("--property-url")
    parser.add_argument("--location-url")
    parser.add_argument("--check-in", default="2026-10-07")
    parser.add_argument("--check-out", default="2026-10-08")
    parser.add_argument("--open-rates", action="store_true")
    parser.add_argument("--accept-cookies", action="store_true")
    args = parser.parse_args()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", proxy={"server": args.proxy} if args.proxy else None
        )
        page = await browser.new_page(locale="en-US")
        try:
            response = await page.goto(
                args.location_url or args.property_url or "https://www.hilton.com/en/",
                wait_until="domcontentloaded",
                timeout=45000,
            )
            print("homepage_status=", response.status if response else None, flush=True)
            reject = page.get_by_role(
                "button",
                name="allow cookies" if args.accept_cookies else "dismiss cookie message",
                exact=True,
            )
            try:
                await reject.wait_for(state="visible", timeout=10000)
                await reject.click()
                if args.accept_cookies:
                    await page.reload(wait_until="domcontentloaded", timeout=45000)
            except BrowserTimeout:
                pass
            if args.location_url:
                check_in = date.fromisoformat(args.check_in)
                check_out = date.fromisoformat(args.check_out)
                await page.get_by_role("button", name=re.compile("Check-in.*Check-out", re.I)).click()

                def day_name(value: date) -> re.Pattern:
                    return re.compile(
                        rf"choose {value.strftime('%A')}, {value.strftime('%B')} {value.day}, {value.year}",
                        re.I,
                    )

                async def choose_day(value: date) -> None:
                    day = page.get_by_role("button", name=day_name(value))
                    for _ in range(13):
                        if await day.count():
                            await day.first.click()
                            return
                        await page.get_by_role("button", name=re.compile("next month", re.I)).last.click()
                    raise RuntimeError(f"Hilton calendar did not expose {value.isoformat()}")

                await choose_day(check_in)
                await choose_day(check_out)
                await page.get_by_role("button", name="Done", exact=True).click()
                traffic = []
                page.on(
                    "response",
                    lambda item: (
                        traffic.append((item.request.resource_type, item.status, item.url.split("?", 1)[0]))
                        if item.request.resource_type in {"document", "xhr", "fetch"}
                        else None
                    ),
                )
                await page.get_by_role("button", name="Update", exact=True).click()
                await page.wait_for_timeout(10000)
                print("update_traffic=", list(dict.fromkeys(traffic)), flush=True)
                print("results_url=", page.url, flush=True)
                print(
                    "selected_dates=",
                    await page.get_by_role(
                        "button", name=re.compile("Check-in.*Check-out", re.I)
                    ).inner_text(),
                    flush=True,
                )
                card = page.locator('[data-testid="hotel-card-ABEKZHX"]')
                print("hotel_card=", (await card.inner_text())[:2000], flush=True)
                print("hotel_card_html=", (await card.inner_html())[:5000], flush=True)
                if args.open_rates:
                    async with page.expect_popup(timeout=30000) as result:
                        await card.get_by_text("Select Dates", exact=True).click()
                    page = await result.value
                    await page.wait_for_load_state("domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(5000)
                    print("rates_url=", page.url, flush=True)
                    print("rates_title=", await page.title(), flush=True)
                    print(
                        "rates_body=",
                        (await page.locator("body").inner_text())[:12000]
                        .encode("ascii", "backslashreplace")
                        .decode(),
                        flush=True,
                    )
            elif args.property_url:
                print(
                    "date_buttons=",
                    await page.locator("button").evaluate_all(
                        'els=>els.filter(e=>e.innerText.includes("SEP")).map(e=>({text:e.innerText,aria:e.getAttribute("aria-label"),html:e.outerHTML.slice(0,500)}))'
                    ),
                    flush=True,
                )
                await page.locator("button").filter(has_text="SEP").first.click()
                print("date_dialog_open=", True, flush=True)
            else:
                search = page.locator('input[name="query"]')
                await search.click()
                await search.fill("")
                await search.press_sequentially(args.query, delay=100)
                try:
                    await page.get_by_role("option").first.wait_for(timeout=20000)
                except BrowserTimeout:
                    print(
                        "search_dom=",
                        await search.evaluate(
                            'e=>({value:e.value,live:e.closest("form").querySelector("[aria-live]")?.textContent})'
                        ),
                        flush=True,
                    )
                print("suggestions=", await page.get_by_role("option").all_text_contents(), flush=True)
                if await page.get_by_role("option").count():
                    await page.get_by_role("option").first.click()
                await page.get_by_role("button", name="Check-in", exact=False).first.click()
            if not args.location_url:
                await page.get_by_role(
                    "button", name=re.compile("CHOOSE WEDNESDAY, OCTOBER 7, 2026 AS YOUR CHECK-IN", re.I)
                ).click()
                await page.get_by_role(
                    "button", name=re.compile("CHOOSE THURSDAY, OCTOBER 8, 2026 AS YOUR CHECK-OUT", re.I)
                ).click()
                await page.get_by_role("button", name="Done", exact=True).click()
                try:
                    async with page.expect_popup(timeout=20000) as result:
                        await page.get_by_role(
                            "button", name=re.compile("Check prices|Find a hotel", re.I)
                        ).click()
                    page = await result.value
                    await page.wait_for_load_state("domcontentloaded")
                    print("results_url=", page.url, flush=True)
                except BrowserTimeout:
                    print("no_search_popup", flush=True)
            print(
                json.dumps(
                    await page.locator("button, input").evaluate_all(
                        "els=>els.map(e=>({tag:e.tagName,text:e.innerText,aria:e.getAttribute('aria-label'),name:e.getAttribute('name')}))"
                    )
                ),
                flush=True,
            )
            print(
                (await page.locator("body").inner_text())[:8000].encode("ascii", "backslashreplace").decode(),
                flush=True,
            )
        except BrowserTimeout:
            print("timeout_page=", await page.title(), flush=True)
            print(
                (await page.locator("body").inner_text())[:2500].encode("ascii", "backslashreplace").decode(),
                flush=True,
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
