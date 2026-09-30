"""Read-only public GHA modal probe for one hotel and stay date."""

import argparse
import asyncio
from datetime import date, timedelta

from app.providers.gha_browser import GHABrowserProvider, public_room
from app.schemas.domain import RateRequest


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="5149")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=8))
    parser.add_argument("--proxy")
    args = parser.parse_args()
    provider = GHABrowserProvider(proxy_server=args.proxy, browser_channel="chrome")
    request = RateRequest(
        provider_hotel_id=args.code, check_in=args.date, check_out=args.date + timedelta(days=1)
    )
    try:
        page = await provider._open_booking(request)
        checkbox = page.get_by_role("checkbox")
        if not await checkbox.is_checked():
            await page.locator("label").filter(has=checkbox).click()
        button = page.locator(".tid-viewRates:not([disabled])").first
        html = await button.evaluate(
            'e=>{let p=e;while(p){if(p.querySelector("h5.px-5"))return p.outerHTML;p=p.parentElement;}return ""}'
        )
        print("room=", public_room(html), flush=True)
        await button.click()
        await page.locator(".tid-selectBtn:visible").first.wait_for()
        print("modal_text=", (await page.locator(".react-responsive-modal-modal").inner_text())[:5000])
        cards = await page.locator(".tid-selectBtn:visible").evaluate_all(
            "els=>els.slice(0,3).map(e=>e.parentElement.parentElement.parentElement.outerHTML.slice(0,6000))"
        )
        for index, card in enumerate(cards):
            print(f"card_{index}=", card, flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
