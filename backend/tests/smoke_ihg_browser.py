"""One read-only IHG official-page quote diagnostic, no database or Telegram."""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from app.core.errors import ProviderError
from app.providers.browser_session import close_browser_sessions
from app.providers.ihg_browser import IHGBrowserProvider
from app.providers.ihg_page import property_identity
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-url", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--session-dir")
    parser.add_argument("--proxy")
    parser.add_argument(
        "--report", type=Path, help="New local JSON evidence file; existing files are refused"
    )
    args = parser.parse_args()
    if not date.today() <= args.date <= date.today() + timedelta(days=364):
        parser.error("Stay must be within the next 365 days")
    code = property_identity(args.official_url)[4].upper()
    provider = IHGBrowserProvider(
        browser_channel="chrome", session_dir=args.session_dir, proxy_server=args.proxy
    )
    provider.configure_hotel_url(args.official_url)
    request = RateRequest(provider_hotel_id=code, check_in=args.date, check_out=args.date + timedelta(days=1))
    report = {
        "provider": "ihg",
        "request": request.model_dump(mode="json"),
        "status": "RUNNING",
        "quote_status": "RUNNING",
        "cleanup_status": "PENDING",
    }
    if args.report:
        # Create exclusively: never overwrite evidence from a previous run.
        with args.report.open("x", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=True, indent=2)
    try:
        rates = await provider.search_rates(request)
        report.update(quote_status="SUCCEEDED", rates=[rate.model_dump(mode="json") for rate in rates])
        for rate in rates:
            print(json.dumps(rate.model_dump(mode="json"), ensure_ascii=True), flush=True)
        print(f"verified_public_quotes={len(rates)} database_writes=0 telegram_calls=0", flush=True)
    except ProviderError as exc:
        report.update(quote_status="FAILED", error_type=exc.code.value, error_message=str(exc))
        print(
            json.dumps({"provider_error": exc.code.value, "message": str(exc)}, ensure_ascii=True), flush=True
        )
        page = provider._page
        if page and not page.is_closed():
            # Only documented public stay controls, never cookies, arbitrary
            # inputs or the full query URL (which may contain tracking data).
            print(
                json.dumps(
                    {
                        "dates": await page.locator("input.calendar-input").evaluate_all(
                            r"es => es.map(e => ({value:/^\d{2}\/\d{2}\/\d{4}$/.test(e.value)?e.value:'other',visible:!!e.getClientRects().length}))"
                        ),
                        "hotel": await page.locator(
                            '[data-testid="hotelDetailsInfoHotelName"]'
                        ).all_text_contents(),
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
        raise
    except BaseException as exc:
        report.update(quote_status="FAILED", error_type=type(exc).__name__)
        raise
    finally:
        try:
            try:
                await provider.close()
            finally:
                await close_browser_sessions()
            report.update(cleanup_status="SUCCEEDED")
        except BaseException as exc:
            report.update(cleanup_status="FAILED", cleanup_error_type=type(exc).__name__)
            raise
        finally:
            report["status"] = (
                "SUCCEEDED" if report["quote_status"] == report["cleanup_status"] == "SUCCEEDED" else "FAILED"
            )
            if args.report:
                args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
