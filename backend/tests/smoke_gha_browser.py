"""Read-only live GHA browser quote smoke test."""

import argparse
import asyncio
from datetime import date, timedelta

from app.core.errors import ProviderError
from app.providers.gha_browser import GHABrowserProvider
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--code", default="10624")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=8))
    parser.add_argument("--all", action="store_true", help="Print every verified room offer")
    args = parser.parse_args()
    provider = GHABrowserProvider(proxy_server=args.proxy, browser_channel="chrome")
    try:
        hotel = await provider.get_hotel_details(args.code)
        print(hotel.model_dump_json(), flush=True)
        rates = await provider.search_rates(
            RateRequest(
                provider_hotel_id=args.code, check_in=args.date, check_out=args.date + timedelta(days=1)
            )
        )
        for rate in rates if args.all else rates[:5]:
            print(rate.model_dump_json(), flush=True)
        print("verified_public_quotes=", len(rates), flush=True)
    except ProviderError as exc:
        print("provider_error=", exc.code.value, str(exc), flush=True)
        if provider._page:
            page = provider._page
            print("page_url=", page.url.split("?")[0], flush=True)
            print("all_rooms=", await page.locator(".tid-viewRates").count(), flush=True)
            print(
                "available_rooms=",
                await page.locator(".tid-viewRates:not([disabled])").count(),
                flush=True,
            )
            print(
                "visible_plan_cards=",
                await page.locator(".tid-selectBtn:visible").count(),
                flush=True,
            )
            print("page_excerpt=", (await page.locator("body").inner_text())[:1800], flush=True)
        raise
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
