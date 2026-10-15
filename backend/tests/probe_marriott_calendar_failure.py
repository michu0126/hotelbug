"""Capture public control/URL evidence for a failed monthly adapter, without writes to DB."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from probe_marriott_cn_calendar import select_audit_hotel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.errors import ErrorCode, ProviderError
from app.providers import marriott_calendar_hints as hints
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.schemas.calendar_hints import CalendarHintRequest, month_window


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--month", required=True)
    args = parser.parse_args()
    if not args.database.is_file() or args.report.exists():
        parser.error("Existing marked audit DB and unique report required")
    engine = create_async_engine("sqlite+aiosqlite:///" + args.database.resolve().as_posix())
    provider = None
    report = {"database_writes": False, "telegram_delivery_called": False}
    try:
        hotel = await select_audit_hotel(async_sessionmaker(engine, expire_on_commit=False))
        report["hotel_code"] = hotel["code"]
        provider = MarriottBrowserProvider(
            hotel_name=hotel["name"],
            session_dir=args.session_dir,
            browser_channel="chrome",
            proxy_server="",
        )
        provider.configure_hotel_url(hotel["official_url"])
        window = month_window(args.month)
        try:
            batch = await provider.get_calendar_hints(
                CalendarHintRequest(
                    provider_hotel_id=hotel["code"],
                    start_date=window[0],
                    end_date=window[1],
                )
            )
            report.update(status="SUCCEEDED", days=len(batch.days))
        except ProviderError as exc:
            report.update(status="FAILED", error_type=exc.code, error_message=str(exc))
            if exc.code not in (ErrorCode.BLOCKED_BY_ANTIBOT, ErrorCode.RATE_LIMITED) and provider._page:
                parsed = urlparse(provider._page.url)
                query = parse_qs(parsed.query, keep_blank_values=True)
                report["public_location"] = {
                    "origin": parsed.scheme + "://" + parsed.netloc,
                    "path": parsed.path,
                    "query": {
                        key: value
                        for key, value in query.items()
                        if key in ("propertyCode", "isRateCalendar", "isSearch", "currency", "showFullPrice")
                    },
                }
                report["controls"] = await provider._page.evaluate(hints.SNAPSHOT_SCRIPT)
    finally:
        if provider:
            await provider.close()
        await close_browser_sessions()
        await engine.dispose()
        args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
        print(json.dumps({key: value for key, value in report.items() if key != "controls"}), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
