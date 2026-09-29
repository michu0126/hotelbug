"""Read-only live GHA browser quote smoke test."""

import argparse
import asyncio
from datetime import date, timedelta

from app.providers.gha_browser import GHABrowserProvider
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--code", default="10624")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=8))
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
        for rate in rates:
            print(rate.model_dump_json(), flush=True)
        print("verified_public_quotes=", len(rates), flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
