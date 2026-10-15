"""One new monthly-hint Worker task in the existing marked local audit DB.

Selects a discovered hotel automatically. No room search, account access,
Telegram delivery, source switch, pause reset, or NAS mutation.
"""

import argparse
import asyncio
import json
from pathlib import Path

from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import (
    AppSetting,
    CalendarHintHistory,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceHistory,
    ProviderStatus,
    StayScan,
    utcnow,
)
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_cn_catalog import ROOT
from app.providers.registry import FACTORIES
from app.schemas.calendar_hints import month_window
from app.schemas.domain import JobInput, JobKind
from app.services.catalog_price_source import SOURCE_MARKER
from app.services.ihg_catalogs import selected_directory_root
from app.services.jobs import enqueue
from app.services.queue import Queue


async def counts(db):
    return {
        model.__tablename__: await db.scalar(select(func.count()).select_from(model))
        for model in (Hotel, CalendarHintHistory, PriceHistory, StayScan, NotificationLog)
    }


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--month", required=True)
    parser.add_argument("--retry-failed-parser", action="store_true")
    args = parser.parse_args()
    if not args.database.is_file() or args.report.exists() or month_window(args.month) is None:
        parser.error("Use a marked existing audit database, new report, and month in the next year")
    settings = Settings(
        _env_file=None,
        browser_channel="chrome",
        browser_session_dir=args.session_dir,
        browser_proxy_url="",
        telegram_bot_token="",
        telegram_chat_id="",
        job_timeout_seconds=300,
        provider_overrides={name: ProviderPolicy(enabled=name == "marriott") for name in FACTORIES},
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + args.database.resolve().as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = FakeRedis(decode_responses=True)
    report = {"status": "RUNNING", "telegram_delivery_called": False, "month": args.month}
    try:
        async with sessions() as db:
            marker = await db.get(AppSetting, "audit:directory-only")
            catalog = await db.get(AppSetting, "catalog:marriott")
            if not marker or marker.value != {"provider": "marriott"}:
                raise ValueError("Missing isolated Marriott audit marker")
            if not catalog or selected_directory_root(catalog.value, "marriott") != ROOT:
                raise ValueError("Audit must retain its accepted CN directory source")
            state = await db.get(ProviderStatus, "marriott")
            now = utcnow()
            if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                raise ValueError("Existing provider pause remains in force")
        # These historical diagnostic DBs predate Alembic; add only the new
        # isolated table after validating their marker. Production uses 0004.
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda connection: CalendarHintHistory.__table__.create(connection, checkfirst=True)
            )
        async with sessions() as db, db.begin():
            report["before"] = await counts(db)
            hotel = await db.scalar(
                select(Hotel)
                .where(
                    Hotel.provider == "marriott",
                    Hotel.active.is_(True),
                    Hotel.official_url.like("https://www.marriott.com.cn/%"),
                    ~select(PriceHistory.id).where(PriceHistory.hotel_id == Hotel.id).exists(),
                )
                .order_by(Hotel.created_at, Hotel.id)
                .limit(1)
            )
            if hotel is None:
                raise ValueError("No discovered hotel without formal price observations")
            prior = await db.scalar(
                select(CrawlJob)
                .where(
                    CrawlJob.hotel_id == hotel.id,
                    CrawlJob.kind == JobKind.FETCH_CALENDAR_HINTS,
                    CrawlJob.payload["month"].as_string() == args.month,
                )
                .order_by(CrawlJob.created_at.desc(), CrawlJob.id.desc())
                .limit(1)
            )
            if prior and not (
                args.retry_failed_parser
                and prior.status == "FAILED"
                and prior.error_type in ("INVALID_RESPONSE", "PARSER_ERROR", "PROVIDER_CHANGED")
            ):
                raise ValueError("This hotel/month already has a hint task; inspect it instead of repeating")
            if prior:
                report["previous_failed_job_id"] = prior.id
            job = await enqueue(
                db,
                JobInput(
                    provider="marriott",
                    hotel_id=hotel.id,
                    kind=JobKind.FETCH_CALENDAR_HINTS,
                    payload={"month": args.month, SOURCE_MARKER: ROOT},
                ),
            )
            job_id = job.id
            report.update(job_id=job_id, hotel_code=hotel.provider_hotel_id, hotel_name=hotel.hotel_name)
        print("stage=monthly_hint_worker", flush=True)
        queue = Queue(redis)
        await queue.put(job_id, 75)
        await process_one(sessions, queue, settings)
        async with sessions() as db:
            job = await db.get(CrawlJob, job_id)
            report["after"] = await counts(db)
            hints = (
                await db.scalars(
                    select(CalendarHintHistory)
                    .where(CalendarHintHistory.job_id == job_id)
                    .order_by(CalendarHintHistory.check_in)
                )
            ).all()
            report.update(
                status=job.status,
                error_type=job.error_type,
                error_message=job.error_message,
                retry_count=job.retry_count,
                priced_hint_days=len(hints),
                hints=[
                    {
                        "check_in": row.check_in.isoformat(),
                        "displayed_price": str(row.displayed_price),
                        "currency": row.currency,
                        "basis": row.basis,
                    }
                    for row in hints
                ],
            )
            assert all(
                report["before"][key] == report["after"][key]
                for key in ("hotels", "price_history", "stay_scans", "notification_logs")
            ), "Hint Worker must not create formal offers, stay coverage, or notifications"
    except BaseException as exc:
        report.update(status="FAILED", diagnostic_error_type=type(exc).__name__)
        raise
    finally:
        try:
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        finally:
            await redis.aclose()
            await engine.dispose()
            args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        key: report.get(key)
                        for key in (
                            "status",
                            "cleanup_status",
                            "hotel_code",
                            "priced_hint_days",
                            "error_type",
                        )
                    }
                ),
                flush=True,
            )
    if report["status"] != "SUCCEEDED":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
