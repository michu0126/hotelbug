"""One live Hyatt worldwide directory Worker job, isolated DB/queue, no rate requests/TG."""

import argparse
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, PriceHistory, utcnow
from app.providers.browser_session import close_browser_sessions
from app.providers.hyatt_catalog import ROOT
from app.services.catalog_coverage import directory_coverage
from app.services.ihg_catalogs import discover_hyatt
from app.services.queue import Queue
from app.services.watchlists import expand_global


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    args = parser.parse_args()
    settings = Settings(
        provider_overrides={"hyatt": ProviderPolicy(enabled=True)},
        browser_channel="chrome",
        browser_proxy_url=args.proxy or "",
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
    )
    with TemporaryDirectory(prefix="hotelbug-hyatt-catalog-") as temp:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(temp) / "test.db"))
        redis = FakeRedis(decode_responses=True)
        queue = Queue(redis)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            assert await discover_hyatt(sessions, queue, settings) == 1
            async with sessions() as db:
                job = await db.scalar(select(CrawlJob))
            await queue.put(job.id, job.priority)
            await process_one(sessions, queue, settings)
            async with sessions() as db, db.begin():
                current = await db.get(CrawlJob, job.id)
                print(
                    json.dumps(
                        {
                            "kind": current.kind,
                            "status": current.status,
                            "error": current.error_type,
                            "message": current.error_message,
                        }
                    ),
                    flush=True,
                )
                if current.status != "SUCCEEDED":
                    raise SystemExit(2)
                state = (await db.get(AppSetting, "catalog:hyatt")).value
                count = await db.scalar(select(func.count()).select_from(Hotel))
                assert count > 0 and await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
                generated = await expand_global(db, settings)
                print(
                    json.dumps(
                        {
                            "hotels_persisted": count,
                            "directory_quotes": 0,
                            "automatic_year_job": generated,
                            "coverage": directory_coverage(
                                state, ROOT, utcnow(), settings.discovery_interval_days
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                assert generated == 1
        finally:
            await close_browser_sessions()
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
