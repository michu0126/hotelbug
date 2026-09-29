"""One public Accor hotel form submission; no booking or payment submission."""

import argparse
import asyncio
import json
import re

from playwright.async_api import Error, async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    args = parser.parse_args()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chrome", proxy={"server": args.proxy} if args.proxy else None
        )
        context = await browser.new_context(locale="en-US")
        page = await context.new_page()
        page.set_default_timeout(20000)
        try:
            response = await page.goto(
                "https://all.accor.com/hotel/0338/index.en.shtml", wait_until="domcontentloaded"
            )
            print("hotel_status=", response.status, flush=True)
            await page.locator("#booking-date-in.hasDatepicker").wait_for(state="visible")
            await page.locator('[name="search.dateIn"]').fill("07/10/2026")
            await page.locator('[name="search.dateIn"]').press("Tab")
            await page.locator('[name="search.dateOut"]').fill("08/10/2026")
            await page.locator('[name="search.dateOut"]').press("Tab")
            await page.locator("button").filter(has_text="1 room, 1 adult").click()
            await page.get_by_role("button", name="Add an adult", exact=True).click()
            await page.locator('[name="search.dateIn"]').click()
            await page.locator('[name="search.dateIn"]').press("Escape")
            form = page.locator("form").filter(has=page.locator('[name="search.dateIn"]'))
            await form.get_by_role("button", name="See rates", exact=True).click()
            await page.wait_for_url("**/booking/**")
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Error:
                pass
            decline = page.get_by_text("Continue without Accepting", exact=False)
            if await decline.count():
                await decline.first.click()
            await page.wait_for_timeout(8000)
            for current in context.pages:
                print("page_url=", current.url.split("?")[0], flush=True)
                cards = await current.get_by_text(re.compile("^Public rate")).evaluate_all("""elements => elements.slice(0,1).map(e => {
                    const chain=[]; let n=e; for(let i=0;n&&i<6;i++,n=n.parentElement)chain.push({tag:n.tagName,cls:n.className,html:i===4?n.outerHTML.slice(0,18000):undefined}); return chain;
                })""")
                print("rate_dom=", json.dumps(cards), flush=True)
                print(
                    "headings=",
                    json.dumps(
                        await current.locator("h1,h2,h3,h4").evaluate_all(
                            "els=>els.map(e=>({tag:e.tagName,cls:e.className,text:e.innerText}))"
                        )
                    ),
                    flush=True,
                )
                print(json.dumps(await current.locator("button").all_text_contents()), flush=True)
                print(
                    (await current.locator("body").inner_text())[:16000]
                    .encode("ascii", "backslashreplace")
                    .decode(),
                    flush=True,
                )
        except Error as exc:
            print("browser_error=", str(exc)[:1500], flush=True)
            print(
                (await page.locator("body").inner_text())[:5000].encode("ascii", "backslashreplace").decode(),
                flush=True,
            )
        finally:
            await context.close()
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
