"""One dated Hyatt query in a project-owned browser; no DB or Telegram writes."""

import argparse
import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

from app.core.errors import ProviderError
from app.providers.browser_session import close_browser_sessions
from app.providers.hyatt_browser import HyattBrowserProvider
from app.providers.hyatt_catalog import property_identity
from app.schemas.domain import RateRequest


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-url", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--channel", default="chrome", help="Browser channel (default: chrome)")
    parser.add_argument(
        "--proxy",
        help=(
            "Explicit browser proxy; omitted means default browser/network routing. "
            "This diagnostic does not load the application's saved proxy settings."
        ),
    )
    args = parser.parse_args()
    if not date.today() <= args.date <= date.today() + timedelta(days=364):
        parser.error("Stay must be within the next 365 days")
    code, _ = property_identity(args.official_url)
    provider = HyattBrowserProvider(
        proxy_server=args.proxy or None,
        browser_channel=args.channel,
        session_dir=args.session_dir,
    )
    provider.configure_hotel_url(args.official_url)
    request = RateRequest(provider_hotel_id=code, check_in=args.date, check_out=args.date + timedelta(days=1))
    report = {
        "request": request.model_dump(mode="json"),
        # Identify the tested route without storing proxy URLs or credentials.
        "browser_channel": args.channel,
        "proxy_configured": bool(args.proxy),
        "quote_status": "RUNNING",
        "cleanup_status": "PENDING",
    }
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
    try:
        rates = await provider.search_rates(request)
        report.update(quote_status="SUCCEEDED", rates=[r.model_dump(mode="json") for r in rates])
        print(
            json.dumps(
                {
                    "quote_status": "SUCCEEDED",
                    "rooms": len({r.room_code for r in rates}),
                    "rates": len(rates),
                    "public_rates": sum(r.member_rate is False for r in rates),
                }
            ),
            flush=True,
        )
    except BaseException as exc:
        report.update(
            quote_status="FAILED",
            error_type=exc.code.value if isinstance(exc, ProviderError) else type(exc).__name__,
            message=str(exc),
        )
        print(
            json.dumps({k: report[k] for k in ("quote_status", "error_type", "message")}, ensure_ascii=True),
            flush=True,
        )
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
