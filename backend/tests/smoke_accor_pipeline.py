"""Live official-page discovery -> Worker -> isolated database, without notifications."""

import argparse
import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.core.logging import redact
from app.crawler.worker import process_one
from app.models.tables import Base, CrawlJob, Hotel, PriceHistory, ProviderStatus
from app.providers.browser_session import close_browser_sessions
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.calendar import get_calendar
from app.services.jobs import enqueue
from app.services.queue import Queue


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--provider", choices=("accor", "gha"), default="accor")
    parser.add_argument("--official-url", help="GHA sitemap detail URL for discovery instead of a known ID")
    parser.add_argument("--code", help="Accor or GHA hotel code for a single live pipeline test")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 10, 7))
    parser.add_argument(
        "--expect-empty", action="store_true", help="Require a successful no-availability job"
    )
    args = parser.parse_args()
    os.environ.update(ACCOR_ENABLED="true", ACCOR_RATE_LIMIT_SECONDS="1", BROWSER_CHANNEL="chrome")
    os.environ[args.provider.upper() + "_ENABLED"] = "true"
    os.environ[args.provider.upper() + "_RATE_LIMIT_SECONDS"] = "1"
    provider_code = args.code or ("0338" if args.provider == "accor" else "10624")
    if args.proxy:
        os.environ["BROWSER_PROXY_URL"] = args.proxy
    get_settings.cache_clear()
    settings = get_settings()
    if args.diagnostics:
        original_factory = FACTORIES[args.provider]

        def traced_factory():
            provider = original_factory()
            original_search = provider.search_rates

            async def traced_search(request):
                try:
                    return await original_search(request)
                except ProviderError as exc:
                    print("provider_failure=", str(exc), flush=True)
                    if exc.__cause__:
                        print("cause=", redact(str(exc.__cause__)), flush=True)
                    page = provider._page
                    if page and not page.is_closed():
                        print("failure_url=", redact(page.url), flush=True)
                        excerpt = redact((await page.locator("body").inner_text())[:5000])
                        print("failure_body=", ascii(excerpt), flush=True)
                    raise

            provider.search_rates = traced_search
            return provider

        FACTORIES[args.provider] = traced_factory
    with TemporaryDirectory(prefix="hotelbug-live-test-") as temp:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(temp) / "test.db"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        redis = FakeRedis(decode_responses=True)
        queue = Queue(redis)
        try:
            async with sessions() as session, session.begin():
                job = await enqueue(
                    session,
                    JobInput(
                        provider=args.provider,
                        kind=JobKind.DISCOVER_HOTELS,
                        payload={"official_url": args.official_url}
                        if args.official_url
                        else {"provider_hotel_id": provider_code},
                    ),
                )
                discovery_id = job.id
            await queue.put(discovery_id, 50)
            await process_one(sessions, queue, settings)
            async with sessions() as session, session.begin():
                hotel = await session.scalar(select(Hotel))
                discovery = await session.get(CrawlJob, discovery_id)
                print("discovery=", discovery.status, discovery.error_type, flush=True)
                if hotel is None:
                    raise SystemExit(2)
                day = args.date
                job = await enqueue(
                    session,
                    JobInput(
                        provider=args.provider,
                        kind=JobKind.FETCH_RATE,
                        hotel_id=hotel.id,
                        check_in=day,
                        check_out=day + timedelta(days=1),
                    ),
                )
                rate_id = job.id
            await asyncio.sleep(1.1)
            await queue.put(rate_id, 50)
            await process_one(sessions, queue, settings)
            async with sessions() as session:
                job = await session.get(CrawlJob, rate_id)
                state = await session.get(ProviderStatus, args.provider)
                count = await session.scalar(select(func.count()).select_from(PriceHistory))
                calendar = await get_calendar(session, hotel, day.strftime("%Y-%m"))
                day_status = next(
                    item["status"] for item in calendar["days"] if item["date"] == day.isoformat()
                )
                print(
                    json.dumps(
                        {
                            "hotel": hotel.hotel_name,
                            "job_status": job.status,
                            "error": job.error_type,
                            "error_message": job.error_message,
                            "persisted_quotes": count,
                            "provider_status": state.status,
                            "calendar_status": day_status,
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                expected_status = "UNAVAILABLE" if args.expect_empty else "AVAILABLE"
                if (
                    job.status != "SUCCEEDED"
                    or (count == 0) != args.expect_empty
                    or day_status != expected_status
                ):
                    raise SystemExit(2)
        finally:
            await close_browser_sessions()
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
