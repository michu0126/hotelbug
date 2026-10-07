"""Check a dedicated Worker profile locally, without contacting a hotel website."""

import argparse
import asyncio
import json

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import redact
from app.providers.browser_session import close_browser_sessions, new_provider_page
from app.providers.registry import FACTORIES


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=FACTORIES, default="gha")
    parser.add_argument("--proxy")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument("--session-dir", default=get_settings().browser_session_dir)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            {
                "provider": args.provider,
                "channel": args.channel,
                "persistent": bool(args.session_dir),
                "headed": args.headed,
                "proxy_configured": bool(args.proxy),
            },
        ),
        flush=True,
    )
    try:
        page = await new_provider_page(
            args.provider,
            proxy_server=args.proxy,
            browser_channel=args.channel,
            session_dir=args.session_dir,
            headless=not args.headed,
        )
        await page.set_content("<h1>Local dedicated browser startup verified</h1>")
        assert await page.locator("h1").inner_text() == "Local dedicated browser startup verified"
        print("dedicated_profile_startup=PASS; hotel_requests=0", flush=True)
    except ProviderError as exc:
        print("startup_error=", exc.code, redact(str(exc)), flush=True)
        if exc.__cause__:
            print("startup_cause=", redact(str(exc.__cause__)), flush=True)
        raise SystemExit(2) from None
    finally:
        await close_browser_sessions()


if __name__ == "__main__":
    asyncio.run(main())
