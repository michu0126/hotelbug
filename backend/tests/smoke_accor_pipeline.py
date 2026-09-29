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
from app.crawler.worker import process_one
from app.models.tables import Base, CrawlJob, Hotel, PriceHistory, ProviderStatus
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.queue import Queue


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    args = parser.parse_args()
    os.environ.update(ACCOR_ENABLED="true", ACCOR_RATE_LIMIT_SECONDS="1", BROWSER_CHANNEL="chrome")
    if args.proxy:
        os.environ["BROWSER_PROXY_URL"] = args.proxy
    get_settings.cache_clear()
    settings = get_settings()
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
                        provider="accor", kind=JobKind.DISCOVER_HOTELS, payload={"provider_hotel_id": "0338"}
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
                day = date(2026, 10, 7)
                job = await enqueue(
                    session,
                    JobInput(
                        provider="accor",
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
                state = await session.get(ProviderStatus, "accor")
                count = await session.scalar(select(func.count()).select_from(PriceHistory))
                print(
                    json.dumps(
                        {
                            "hotel": hotel.hotel_name,
                            "job_status": job.status,
                            "error": job.error_type,
                            "persisted_quotes": count,
                            "provider_status": state.status,
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                if job.status != "SUCCEEDED" or count < 1:
                    raise SystemExit(2)
        finally:
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
