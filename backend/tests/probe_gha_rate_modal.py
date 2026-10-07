"""Read-only public GHA modal probe for one hotel and stay date."""

import argparse
import asyncio
import json
from datetime import date, timedelta

from bs4 import BeautifulSoup

from app.core.errors import ProviderError
from app.providers.browser_session import close_browser_sessions
from app.providers.gha_browser import GHABrowserProvider, public_room
from app.schemas.domain import RateRequest


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="5149")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=8))
    parser.add_argument("--proxy")
    parser.add_argument("--session-dir")
    parser.add_argument(
        "--room-only", action="store_true", help="Read public room-card fields, no modal or booking action"
    )
    args = parser.parse_args()
    provider = GHABrowserProvider(
        proxy_server=args.proxy, browser_channel="chrome", session_dir=args.session_dir
    )
    request = RateRequest(
        provider_hotel_id=args.code, check_in=args.date, check_out=args.date + timedelta(days=1)
    )
    try:
        page = await provider._open_booking(request)
        checkbox = page.get_by_role("checkbox")
        if not await checkbox.is_checked():
            await page.locator("label").filter(has=checkbox).click()
        button = page.locator(".tid-viewRates:not([disabled])").first
        await button.wait_for(state="visible", timeout=30000)
        html = await button.evaluate(
            'e=>{let p=e;while(p){if(p.querySelector("h5.px-5"))return p.outerHTML;p=p.parentElement;}return ""}'
        )
        if args.room_only:
            cards = await page.locator(".tid-viewRates:not([disabled])").evaluate_all(
                'es=>es.map(e=>{let p=e;while(p){if(p.querySelector("h5.px-5"))return p.outerHTML;p=p.parentElement;}return ""})'
            )
            for index, card in enumerate(cards):
                try:
                    room = public_room(card)
                    print(
                        json.dumps(
                            {"room_index": index, "public_room_parsed": [room[0], room[1], str(room[2])]},
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                except ProviderError as exc:
                    soup = BeautifulSoup(card, "html.parser")
                    print(
                        json.dumps(
                            {
                                "room_index": index,
                                "error": exc.code.value,
                                "public_room_fields": [
                                    {
                                        "tag": item.name,
                                        "class": item.get("class", []),
                                        "text": item.get_text(" ", strip=True),
                                    }
                                    for item in soup.select("h5,h6,span,button")
                                ],
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                    break
            return
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
        await close_browser_sessions()


if __name__ == "__main__":
    asyncio.run(main())
