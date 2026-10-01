"""Live official sitemap -> selected discovery -> automatic yearly price job, no TG."""

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

from app.core.config import get_settings
from app.core.errors import ProviderError
from app.crawler.worker import process_one
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, NotificationLog, PriceHistory, utcnow
from app.providers.browser_session import close_browser_sessions
from app.providers.registry import FACTORIES
from app.schemas.domain import JobKind
from app.services.catalogs import discover_accor, discover_gha
from app.services.queue import Queue
from app.services.watchlists import expand_global


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("accor", "gha"), required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--limit", type=int, choices=range(1, 6), default=1)
    args = parser.parse_args()
    for name in FACTORIES:
        os.environ[name.upper() + "_ENABLED"] = str(name == args.provider).lower()
    os.environ["BROWSER_CHANNEL"] = "chrome"
    if args.proxy:
        os.environ["BROWSER_PROXY_URL"] = args.proxy
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"catalog_jobs_per_tick": 1, "global_jobs_per_tick": 1})
    if args.diagnostics:
        original_factory = FACTORIES[args.provider]

        def diagnostic_factory():
            provider = original_factory()
            original_search = provider.search_rates

            async def traced_search(request):
                try:
                    return await original_search(request)
                except ProviderError:
                    page = provider._page
                    if page and not page.is_closed():
                        print("failure_url=", page.url, flush=True)
                        print(
                            "failure_body=",
                            ascii((await page.locator("body").inner_text())[:3500]),
                            flush=True,
                        )
                        print(
                            "visible_offer_prices=",
                            await page.locator(".offer-price:visible").count(),
                            flush=True,
                        )
                    raise

            provider.search_rates = traced_search
            return provider

        FACTORIES[args.provider] = diagnostic_factory
    discover = {"accor": discover_accor, "gha": discover_gha}[args.provider]
    with TemporaryDirectory(prefix="hotelbug-sitemap-pipeline-") as directory:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(directory) / "test.db"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        redis = FakeRedis(decode_responses=True)
        queue = Queue(redis)

        async def run_job(job):
            while True:
                async with sessions() as session:
                    current = await session.get(CrawlJob, job.id)
                    now = utcnow()
                    due = (current.scheduled_at.replace(tzinfo=now.tzinfo) - now).total_seconds()
                await asyncio.sleep(
                    max(due + 0.1, settings.provider_policy(args.provider).interval_seconds + 0.1)
                )
                await queue.put(job.id, job.priority)
                await process_one(sessions, queue, settings)
                async with sessions() as session:
                    result = await session.get(CrawlJob, job.id)
                    print(
                        json.dumps(
                            {
                                "kind": result.kind,
                                "status": result.status,
                                "error": result.error_type,
                                "message": result.error_message,
                                "source": job.payload,
                                "retry_count": result.retry_count,
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
                    if result.status == "PENDING" and result.error_type in ("TIMEOUT", "NETWORK_ERROR"):
                        # Respect the production retry timestamp; never zero out
                        # a cooldown or rebuild a new job merely after a timeout.
                        continue
                    return result.status == "SUCCEEDED"

        try:
            async with httpx.AsyncClient(proxy=args.proxy, follow_redirects=True) as client:
                for _ in range(args.limit):
                    added = await discover(sessions, queue, settings, client)
                    if not added:
                        async with sessions() as session:
                            state = await session.get(AppSetting, "catalog:" + args.provider)
                            data = state.value
                            print(
                                json.dumps(
                                    {
                                        "maps": len(data.get("maps", [])),
                                        "map_cursor": data.get("map_cursor", 0),
                                        "candidates": len(data.get("urls", data.get("codes", []))),
                                        "error": data.get("last_error"),
                                    }
                                ),
                                flush=True,
                            )
                            if data.get("last_error"):
                                raise SystemExit(2)
                            remaining = data.get("next_fetch_at", 0) - utcnow().timestamp()
                        if not 0 <= remaining <= 60:
                            raise SystemExit("No discovery task due in this bounded live test")
                        await asyncio.sleep(remaining + 0.1)
                        added = await discover(sessions, queue, settings, client)
                    if added != 1:
                        raise SystemExit("Official catalog generated no discovery task")
                    async with sessions() as session:
                        job = await session.scalar(
                            select(CrawlJob)
                            .where(CrawlJob.status == "PENDING", CrawlJob.kind == JobKind.DISCOVER_HOTELS)
                            .order_by(CrawlJob.created_at.desc())
                        )
                    if not await run_job(job):
                        continue
                    async with sessions() as session, session.begin():
                        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
                        assert await expand_global(session, settings) == 1
                        price_job = await session.scalar(
                            select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE)
                        )
                    price_ok = await run_job(price_job)
                    async with sessions() as session:
                        hotel = await session.get(Hotel, price_job.hotel_id)
                        quote_count = await session.scalar(select(func.count()).select_from(PriceHistory))
                        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
                        print(
                            json.dumps(
                                {
                                    "provider": hotel.provider,
                                    "hotel": hotel.hotel_name,
                                    "hotel_code": hotel.provider_hotel_id,
                                    "automatically_scheduled_check_in": price_job.check_in.isoformat(),
                                    "live_quotes_persisted": quote_count,
                                    "manual_hotel_input": False,
                                    "telegram_sent": False,
                                }
                            ),
                            flush=True,
                        )
                    if not price_ok or not quote_count:
                        raise SystemExit(2)
                    return
                raise SystemExit("No automatically selected candidate completed discovery")
        finally:
            await close_browser_sessions()
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
