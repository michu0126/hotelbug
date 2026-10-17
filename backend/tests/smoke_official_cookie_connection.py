"""Real Chromium Cookie apply/update/removal smoke, without any hotel requests."""

import asyncio
import json
from tempfile import TemporaryDirectory

from app.providers.browser_session import close_browser_sessions, new_provider_page
from app.services.official_connections import ORIGINS, save_connection


async def main():
    with TemporaryDirectory(prefix="hotelbug-cookie-smoke-") as directory:
        try:
            save_connection(directory, "marriott", ORIGINS["marriott"], "HotelbugOfflineSmoke=fixture-one")
            page = await new_provider_page("marriott", session_dir=directory)
            context = page.context
            cookies = await context.cookies([ORIGINS["marriott"]])
            assert any(c["name"] == "HotelbugOfflineSmoke" and c["value"] == "fixture-one" for c in cookies)
            await page.close()
            save_connection(directory, "marriott", ORIGINS["marriott"], "HotelbugOfflineSmoke=fixture-two")
            page = await new_provider_page("marriott", session_dir=directory)
            assert page.context is context
            cookies = await context.cookies([ORIGINS["marriott"]])
            assert any(c["name"] == "HotelbugOfflineSmoke" and c["value"] == "fixture-two" for c in cookies)
            await page.close()
            save_connection(directory, "marriott", ORIGINS["marriott"], enabled=False)
            page = await new_provider_page("marriott", session_dir=directory)
            cookies = await context.cookies([ORIGINS["marriott"]])
            assert not any(c["name"] == "HotelbugOfflineSmoke" for c in cookies)
            await page.close()
            print(
                json.dumps(
                    {
                        "real_chromium_apply": True,
                        "same_profile_update": True,
                        "imported_cookie_removal": True,
                        "hotel_requests": 0,
                    }
                ),
                flush=True,
            )
        finally:
            await close_browser_sessions()


if __name__ == "__main__":
    asyncio.run(main())
