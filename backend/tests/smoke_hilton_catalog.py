"""Two low-frequency live official sitemap reads -> isolated discovery job state."""

import argparse
import asyncio
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.models.tables import AppSetting, Base, CrawlJob, Hotel
from app.services.catalogs import discover_hilton
from app.services.queue import Queue


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proxy")
    args = parser.parse_args()
    os.environ["HILTON_ENABLED"] = "true"
    with TemporaryDirectory(prefix="hotelbug-hilton-catalog-") as directory:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(directory) / "test.db"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        redis = FakeRedis(decode_responses=True)
        try:
            async with httpx.AsyncClient(proxy=args.proxy, follow_redirects=True) as client:
                queue = Queue(redis)
                for index in range(2):
                    count = await discover_hilton(sessions, queue, Settings(), client)
                    async with sessions() as session:
                        state = (await session.get(AppSetting, "catalog:hilton")).value
                        result = {
                            "discovery_jobs_added": count,
                            "maps": len(state.get("maps", [])),
                            "map_cursor": state.get("map_cursor"),
                            "property_candidates": len(state.get("urls", [])),
                            "error": state.get("last_error"),
                            "hotels_not_yet_verified": await session.scalar(
                                select(func.count()).select_from(Hotel)
                            ),
                        }
                        print(json.dumps(result), flush=True)
                        if state.get("last_error"):
                            raise SystemExit(2)
                    if index == 0:
                        await asyncio.sleep(31)
                async with sessions() as session:
                    job = await session.scalar(select(CrawlJob))
                    if job is None or job.provider != "hilton" or not job.payload.get("official_url"):
                        raise SystemExit(2)
                    print(json.dumps({"first_job": job.payload, "kind": job.kind}), flush=True)
        finally:
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
