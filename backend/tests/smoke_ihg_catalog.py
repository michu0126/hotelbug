"""Live worldwide root -> automatic leaf -> optional directory/price, isolated DB, no TG."""

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
from app.crawler.worker import process_one
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, PriceHistory
from app.providers.browser_session import close_browser_sessions
from app.schemas.domain import JobInput, JobKind
from app.services.ihg_catalogs import discover_ihg
from app.services.jobs import enqueue
from app.services.queue import Queue
from app.services.watchlists import expand_global


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    parser.add_argument("--directory", help="Optional extra directory; must have been found in the live root")
    parser.add_argument("--with-price", action="store_true")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today() + timedelta(days=10))
    args = parser.parse_args()
    os.environ.update(IHG_ENABLED="true", IHG_RATE_LIMIT_SECONDS="1", BROWSER_CHANNEL="chrome")
    if args.proxy:
        os.environ["BROWSER_PROXY_URL"] = args.proxy
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"catalog_jobs_per_tick": 1, "global_jobs_per_tick": 1})
    with TemporaryDirectory(prefix="hotelbug-ihg-catalog-test-") as temp:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(temp) / "test.db"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        redis = FakeRedis(decode_responses=True)
        queue = Queue(redis)

        async def run_job(job):
            await asyncio.sleep(1.1)
            await queue.put(job.id, job.priority)
            await process_one(sessions, queue, settings)
            async with sessions() as session:
                current = await session.get(CrawlJob, job.id)
                print(
                    json.dumps(
                        {
                            "kind": current.kind,
                            "status": current.status,
                            "error": current.error_type,
                            "message": current.error_message,
                            "url": current.payload.get("official_url"),
                        }
                    ),
                    flush=True,
                )
                if current.status != "SUCCEEDED":
                    raise SystemExit(2)

        try:
            # Both root and first leaf are selected by production discovery logic.
            for _ in range(2):
                assert await discover_ihg(sessions, queue, settings) == 1
                async with sessions() as session:
                    job = await session.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
                await run_job(job)
            if args.directory:
                async with sessions() as session, session.begin():
                    state = await session.get(AppSetting, "catalog:ihg")
                    assert args.directory in state.value["pages"], (
                        "Extra directory not in observed official links"
                    )
                    job = await enqueue(
                        session,
                        JobInput(
                            provider="ihg",
                            kind=JobKind.DISCOVER_CATALOG,
                            payload={"official_url": args.directory},
                        ),
                    )
                await run_job(job)
            async with sessions() as session, session.begin():
                state = await session.get(AppSetting, "catalog:ihg")
                hotel_count = await session.scalar(select(func.count()).select_from(Hotel))
                assert hotel_count > 0
                assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
                assert await expand_global(session, settings) == 1
                print(
                    json.dumps(
                        {
                            "discovered_directories": len(state.value["pages"]),
                            "hotels_persisted": hotel_count,
                            "directory_quotes": 0,
                            "automatic_year_date_job_created": True,
                            "extra_page": state.value["pages"].get(args.directory),
                        }
                    ),
                    flush=True,
                )
                # The generated job proves directory hotels enter the existing
                # yearly scheduler; one chosen hotel is a separate live quote check.
                if args.with_price:
                    hotel = await session.scalar(
                        select(Hotel).where(Hotel.official_url.like("%/huntsville/%"))
                    )
                    if hotel is None:
                        hotel = await session.scalar(select(Hotel).order_by(Hotel.hotel_name))
                    price_job = await enqueue(
                        session,
                        JobInput(
                            provider="ihg",
                            kind=JobKind.FETCH_RATE,
                            hotel_id=hotel.id,
                            check_in=args.date,
                            check_out=args.date + timedelta(days=1),
                        ),
                    )
            if args.with_price:
                await run_job(price_job)
                async with sessions() as session:
                    count = await session.scalar(
                        select(func.count())
                        .select_from(PriceHistory)
                        .where(PriceHistory.job_id == price_job.id)
                    )
                    print(
                        json.dumps(
                            {
                                "hotel": hotel.hotel_name,
                                "check_in": args.date.isoformat(),
                                "live_quotes_persisted": count,
                            }
                        ),
                        flush=True,
                    )
                    assert count > 0
        finally:
            await close_browser_sessions()
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
