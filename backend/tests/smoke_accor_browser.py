import argparse
import asyncio
import json
from datetime import date, timedelta

from app.core.errors import ProviderError
from app.core.logging import redact
from app.providers.accor_browser import AccorBrowserProvider
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--code", default="0338")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 7))
    args = parser.parse_args()
    provider = AccorBrowserProvider(proxy_server=args.proxy, browser_channel=args.channel)
    try:
        rates = await provider.search_rates(
            RateRequest(
                provider_hotel_id=args.code, check_in=args.date, check_out=args.date + timedelta(days=1)
            )
        )
        print(json.dumps([r.model_dump(mode="json") for r in rates], ensure_ascii=True), flush=True)
        print("verified_public_quotes=", len(rates), flush=True)
    except ProviderError as exc:
        print("provider_error=", exc.code, str(exc), flush=True)
        if exc.__cause__:
            print("cause=", redact(str(exc.__cause__)), flush=True)
        raise SystemExit(2) from None
    finally:
        await provider.close()


if __name__ == "__main__":
    asyncio.run(main())
