"""One normal NextPage click to verify the published directory continuation."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_catalog import directory_url


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-url", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    source = directory_url(args.official_url)
    report = {"source": source, "status": "RUNNING", "database_writes": 0, "price_requests": 0}
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream)
    provider = MarriottBrowserProvider(browser_channel="chrome", session_dir=args.session_dir)
    try:
        async with asyncio.timeout(90):
            page = await provider._get_page()
            response = await page.goto(source, wait_until="commit", timeout=45000)
            await provider._check_access(page, response.status if response else None)
            following = page.locator('a[aria-label="NextPage"]:not(.disabled)')
            await following.wait_for(timeout=30000)
            report["before"] = {
                "url": page.url,
                "range": await page.locator(".search-summary-container").inner_text(),
            }
            async with page.expect_response(
                lambda r: (
                    urlparse(r.url).hostname == "www.marriott.com.cn"
                    and urlparse(r.url).path == "/mi/query/phoenixShopSearchPropertiesByLocation"
                ),
                timeout=30000,
            ) as event:
                await following.click()
            response = await event.value
            await provider._check_access(page, response.status)
            payload = await asyncio.wait_for(response.json(), timeout=15)
            data = payload["data"]["search"]["properties"]["searchByLocation"]
            offset = data["pageInfo"]["currentOffset"]
            await page.wait_for_function(
                "first => document.querySelector('.search-summary-container')?.textContent.trim().startsWith(first + ' -')",
                arg=offset + 1,
                timeout=15000,
            )
            report.update(
                status="SUCCEEDED",
                after={
                    "url": page.url,
                    "range": await page.locator(".search-summary-container").inner_text(),
                },
                request=response.request.post_data_json,
                page_info=data["pageInfo"],
                total=data["total"],
                hotel_ids=[edge["node"]["id"] for edge in data["edges"]],
            )
    except BaseException as exc:
        report.update(status="FAILED", error_type=type(exc).__name__)
        raise
    finally:
        try:
            await provider.close()
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        finally:
            args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        k: report.get(k)
                        for k in ("status", "error_type", "before", "after", "page_info", "cleanup_status")
                    }
                ),
                flush=True,
            )


if __name__ == "__main__":
    asyncio.run(main())
