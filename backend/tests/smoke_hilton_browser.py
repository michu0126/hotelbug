"""Live official Hilton room/public-rate test; never submits a reservation."""

import argparse
import asyncio
from datetime import date, timedelta

from app.core.errors import ProviderError
from app.core.logging import redact
from app.providers.browser_session import close_browser_sessions
from app.providers.hilton_browser import HiltonBrowserProvider
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--session-dir")
    parser.add_argument("--code", default="LONCOCI")
    parser.add_argument(
        "--hotel-name", help="Use the official homepage search instead of a code-only deeplink"
    )
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=10))
    args = parser.parse_args()
    provider = HiltonBrowserProvider(
        proxy_server=args.proxy, browser_channel=args.channel, session_dir=args.session_dir
    )
    if args.hotel_name:
        provider.configure_hotel(args.hotel_name)
    try:
        rates = await provider.search_rates(
            RateRequest(
                provider_hotel_id=args.code, check_in=args.date, check_out=args.date + timedelta(days=1)
            )
        )
        for rate in rates[:8]:
            print(rate.model_dump_json(), flush=True)
        print("verified_public_quotes=", len(rates), flush=True)
    except ProviderError as exc:
        print("provider_error=", exc.code.value, str(exc), flush=True)
        if exc.__cause__:
            print("cause=", redact(str(exc.__cause__)), flush=True)
        page = provider._page
        if page and not page.is_closed():
            print("page_title=", ascii(await page.title()), flush=True)
            print("page_url=", redact(page.url), flush=True)
        raise SystemExit(2) from None
    finally:
        await provider.close()
        await close_browser_sessions()


if __name__ == "__main__":
    asyncio.run(main())
