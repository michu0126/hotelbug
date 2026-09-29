"""Inspect Hilton's public homepage search; never submits a reservation."""

import argparse
import asyncio
import json
import re

from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--query", default="London")
    args = parser.parse_args()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", proxy={"server": args.proxy} if args.proxy else None
        )
        page = await browser.new_page(locale="en-US")
        try:
            response = await page.goto(
                "https://www.hilton.com/en/", wait_until="domcontentloaded", timeout=45000
            )
            print("homepage_status=", response.status if response else None, flush=True)
            reject = page.get_by_role("button", name="dismiss cookie message", exact=True)
            try:
                await reject.wait_for(state="visible", timeout=10000)
                await reject.click()
            except BrowserTimeout:
                pass
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
                        'e=>({value:e.value,expanded:e.getAttribute("aria-expanded"),live:e.closest("form").querySelector("[aria-live]")?.textContent})'
                    ),
                    flush=True,
                )
                print(
                    "body=",
                    (await page.locator("body").inner_text())[:2500]
                    .encode("ascii", "backslashreplace")
                    .decode(),
                    flush=True,
                )
            print("suggestions=", await page.get_by_role("option").all_text_contents(), flush=True)
            if await page.get_by_role("option").count():
                await page.get_by_role("option").first.click()
            await page.get_by_role("button", name="Check-in", exact=False).first.click()
            await page.get_by_role(
                "button", name=re.compile("CHOOSE WEDNESDAY, OCTOBER 7, 2026 AS YOUR CHECK-IN", re.I)
            ).click()
            await page.get_by_role(
                "button", name=re.compile("CHOOSE THURSDAY, OCTOBER 8, 2026 AS YOUR CHECK-OUT", re.I)
            ).click()
            await page.get_by_role("button", name="Done", exact=True).click()
            try:
                async with page.expect_popup(timeout=20000) as result:
                    await page.get_by_role("button", name=re.compile("Find a hotel")).click()
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
