"""Inspect public room offers and tax toggle; never selects or purchases a room."""

import argparse
import asyncio
import json

from playwright.async_api import async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    args = parser.parse_args()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", proxy={"server": args.proxy} if args.proxy else None
        )
        page = await browser.new_page(locale="en-US")
        try:
            await page.goto(
                "https://www.ghadiscovery.com/booking/select_room?hotelId=10624&clearBookingParams=1&startDate=2026-10-07&endDate=2026-10-08&room1Adults=2&room1Children=0&from=hotel",
                wait_until="domcontentloaded",
            )
            await page.get_by_role("button", name="VIEW RATES", exact=True).first.wait_for(timeout=30000)
            print(
                "inputs=",
                await page.locator("input").evaluate_all("els=>els.map(e=>e.outerHTML)"),
                flush=True,
            )
            await page.locator("label").filter(has=page.get_by_role("checkbox")).click()
            print(
                "room_ancestors=",
                json.dumps(
                    await page.get_by_role("button", name="VIEW RATES", exact=True).first.evaluate(
                        'e=>{let p=e,a=[]; for(let i=0;i<10 && p;i++,p=p.parentElement){a.push({cls:p.className,text:p.innerText.slice(0,400),titles:[...p.querySelectorAll("h5,h6")].map(h=>h.outerHTML)});}return a}'
                    )
                ),
                flush=True,
            )
            await page.get_by_role("button", name="VIEW RATES", exact=True).first.click()
            await page.wait_for_timeout(2000)
            print(
                "buttons=",
                await page.locator("button").evaluate_all(
                    'els=>els.map(e=>({text:e.innerText,aria:e.getAttribute("aria-label"),cls:e.className}))'
                ),
                flush=True,
            )
            print(
                "rate_cards=",
                json.dumps(
                    await page.get_by_role("button", name="SELECT", exact=True).evaluate_all(
                        "els=>els.map(e=>e.parentElement.parentElement.parentElement.outerHTML.slice(0,18000))"
                    )
                ),
                flush=True,
            )
            print(
                (await page.locator("body").inner_text())[:14000]
                .encode("ascii", "backslashreplace")
                .decode(),
                flush=True,
            )
            print(
                "headings=",
                json.dumps(
                    await page.locator("h1,h2,h3,h4").evaluate_all(
                        "els=>els.map(e=>({text:e.innerText,html:e.parentElement.outerHTML.slice(0,16000)}))"
                    )
                ),
                flush=True,
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
