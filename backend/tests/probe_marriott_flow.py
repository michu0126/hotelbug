"""Read-only Marriott homepage-to-rates browser probe, without saved sessions."""

import argparse
import asyncio

from playwright.async_api import async_playwright


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=not args.headed)
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        try:
            home = await page.goto("https://www.marriott.com/default.mi", wait_until="domcontentloaded")
            print("home_status=", home.status if home else None)
            print("home_title=", await page.title())
            await page.get_by_role("textbox", name="Destination").fill("New York Marriott Marquis")
            suggestion = page.locator('[id^="downshift-0-item-"]').filter(
                has_text="New York Marriott Marquis"
            )
            await suggestion.first.click(timeout=15000)
            await page.get_by_role("button", name="Find Hotels").click()
            await page.get_by_role("button", name="New York Marriott Marquis", exact=True).wait_for(
                timeout=30000
            )
            print("search_title=", await page.title())
            card = page.locator(".property-card-container").filter(
                has=page.get_by_role("button", name="New York Marriott Marquis", exact=True)
            )
            await card.get_by_role("link", name="View Rates").click()
            await page.get_by_role("heading", name="Select a Room and Rate").wait_for(timeout=30000)
            print("rate_page_title=", await page.title())
            print("rate_page_ready=", True)
        except Exception as exc:
            print("current_url=", page.url.split("?")[0])
            print("current_title=", await page.title())
            print("failure_type=", type(exc).__name__)
            raise
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
