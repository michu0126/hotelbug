"""One-shot public booking-page probe; never persists rates or sends alerts."""

import argparse
import asyncio
import sys
from datetime import date, timedelta

from playwright.async_api import Error as PlaywrightError

from app.core.errors import ProviderError
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.schemas.domain import RateRequest


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", default="NYCMQ")
    parser.add_argument("--hotel-name", default="New York Marriott Marquis")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=10))
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument("--channel", help="Local browser channel, for example chrome; omit in Docker")
    parser.add_argument("--headed", action="store_true", help="Show a fresh local test browser window")
    parser.add_argument("--proxy", help="HTTP or SOCKS5 proxy URL for this one-shot test")
    args = parser.parse_args()
    provider = MarriottBrowserProvider(
        hotel_name=args.hotel_name,
        browser_channel=args.channel,
        headless=not args.headed,
        proxy_server=args.proxy,
    )
    try:
        rates = await provider.search_rates(
            RateRequest(
                provider_hotel_id=args.code,
                check_in=args.date,
                check_out=args.date + timedelta(days=1),
                adults=args.adults,
            )
        )
        for rate in rates:
            print(
                rate.provider_hotel_id,
                rate.check_in,
                rate.room_code,
                rate.rate_code,
                rate.total_price,
                rate.currency,
            )
        print(f"verified_public_nonmember_quotes={len(rates)}")
        return 0 if rates else 1
    except ProviderError as exc:
        print("provider_error=", exc.code)
        print("provider_message=", str(exc))
        page = provider._page
        if page:
            print("page_url=", page.url.split("?")[0])
            try:
                print("page_title=", await page.title())
                body = (await page.locator("body").inner_text()).lower()
                print("access_denied_marker=", "access denied" in body)
                print("human_challenge_marker=", "verify you are human" in body)
                print("booking_heading_present=", "select a room and rate" in body)
            except PlaywrightError:
                print("page_closed_before_diagnostics=", True)
        return 2
    finally:
        try:
            await provider.close()
        finally:
            await close_browser_sessions()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
