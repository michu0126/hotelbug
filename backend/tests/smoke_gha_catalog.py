"""One-shot official GHA sitemap candidate verification; no database writes."""

import argparse
import asyncio

from app.providers.gha_browser import GHABrowserProvider


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default="https://www.ghadiscovery.com/avani-hotels-resorts/avani-sukhumvit-bangkok-hotel",
    )
    parser.add_argument("--proxy")
    args = parser.parse_args()
    provider = GHABrowserProvider(proxy_server=args.proxy, browser_channel="chrome")
    try:
        hotels = await provider.search_hotels({"official_url": args.url})
        for hotel in hotels:
            print(hotel.model_dump_json(), flush=True)
        print("verified_hotels=", len(hotels), flush=True)
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
