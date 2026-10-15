"""One official Chinese Marriott UI quote through the real Worker adapter.

Uses a project-owned ordinary browser; no user profile, database or Telegram.
"""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from app.core.errors import ProviderError
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-url", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", required=True, type=Path)
    args = parser.parse_args()
    if not date.today() <= args.date <= date.today() + timedelta(days=364):
        parser.error("Stay must be within the next 365 days")
    provider = MarriottBrowserProvider(browser_channel="chrome", session_dir=args.session_dir)
    provider.configure_hotel_url(args.official_url)
    if provider._root != "https://www.marriott.com.cn":
        parser.error("This diagnostic requires the Chinese official property URL")
    request = RateRequest(
        provider_hotel_id=provider._official_code, check_in=args.date, check_out=args.date + timedelta(days=1)
    )
    report = {
        "request": request.model_dump(mode="json"),
        "quote_status": "RUNNING",
        "cleanup_status": "PENDING",
    }
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
    try:
        rates = await provider.search_rates(request)
        report.update(quote_status="SUCCEEDED", rates=[r.model_dump(mode="json") for r in rates])
        print(json.dumps(report, ensure_ascii=True), flush=True)
    except BaseException as exc:
        report.update(
            quote_status="FAILED",
            error_type=exc.code.value if isinstance(exc, ProviderError) else type(exc).__name__,
        )
        print(json.dumps({"error_type": report["error_type"]}, ensure_ascii=True), flush=True)
        raise
    finally:
        try:
            try:
                await provider.close()
            finally:
                await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        except BaseException as exc:
            report.update(cleanup_status="FAILED", cleanup_error_type=type(exc).__name__)
            raise
        finally:
            report["status"] = (
                "SUCCEEDED" if report["quote_status"] == report["cleanup_status"] == "SUCCEEDED" else "FAILED"
            )
            args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
            print("database_writes=0 telegram_calls=0", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
