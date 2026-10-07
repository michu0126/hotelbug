"""Resume IHG official-map supplementation in its existing isolated directory audit."""

import argparse
import asyncio
import json
from pathlib import Path

import httpx
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceHistory,
    ProviderStatus,
    utcnow,
)
from app.providers.browser_session import close_browser_sessions
from app.providers.ihg_sitemap import STATE_KEY
from app.schemas.domain import JobKind
from app.services.catalogs import read_sitemaps
from app.services.ihg_sitemaps import discover_ihg_sitemap
from app.services.queue import Queue
from app.services.watchlists import expand_global


async def run(sessions, queue, settings, client, max_ticks=4, emit=print):
    async with sessions() as db:
        marker = await db.get(AppSetting, "audit:directory-only")
        assert marker and marker.value == {"provider": "ihg"}, "Not the isolated IHG directory audit"
        before_quotes = await db.scalar(select(func.count()).select_from(PriceHistory))
        before_notifications = await db.scalar(select(func.count()).select_from(NotificationLog))
    outcome = "NO_DETAIL_EXECUTED"
    selected_job = None
    for _ in range(max_ticks):
        async with sessions() as db:
            now = utcnow()
            status = await db.get(ProviderStatus, "ihg")
            if status and status.blocked_until and status.blocked_until.replace(tzinfo=now.tzinfo) > now:
                outcome = "PROVIDER_PAUSED"
                break
            job = await db.scalar(
                select(CrawlJob)
                .where(
                    CrawlJob.provider == "ihg",
                    CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                    CrawlJob.status.in_(("PENDING", "QUEUED")),
                    CrawlJob.scheduled_at <= now,
                    CrawlJob.payload["catalog_source"].as_string() == STATE_KEY,
                )
                .order_by(CrawlJob.scheduled_at, CrawlJob.id)
            )
            source = await db.get(AppSetting, STATE_KEY)
            data = source.value if source else {}
        if job:
            selected_job = job.id
            emit(
                json.dumps(
                    {"starting_detail_job": job.id, "public_source": job.payload["official_url"]},
                    ensure_ascii=True,
                )
            )
            await queue.put(job.id, job.priority)
            await process_one(sessions, queue, settings)
            async with sessions() as db:
                current = await db.get(CrawlJob, job.id)
                delay = (current.scheduled_at.replace(tzinfo=now.tzinfo) - utcnow()).total_seconds()
                interval_only = current.status == "PENDING" and current.error_type is None
            if interval_only and 0 < delay <= settings.provider_policy("ihg").interval_seconds + 2:
                await asyncio.sleep(delay + 0.1)
                await queue.put(job.id, job.priority)
                await process_one(sessions, queue, settings)
            async with sessions() as db:
                current = await db.get(CrawlJob, job.id)
                outcome = "DETAIL_SUCCEEDED" if current.status == "SUCCEEDED" else "DETAIL_READ_UNSUCCESSFUL"
            break
        wait = data.get("next_fetch_at", 0) - utcnow().timestamp()
        if wait > 31:
            outcome = "SOURCE_RETRY_NOT_DUE"
            break
        if wait > 0:
            emit(json.dumps({"waiting_for_normal_source_tick_seconds": round(wait, 1)}))
            await asyncio.sleep(wait + 0.1)
        await discover_ihg_sitemap(sessions, queue, settings, client)
        async with sessions() as db:
            source = await db.get(AppSetting, STATE_KEY)
            data = source.value if source else {}
        if data.get("last_error") not in (None, "SITEMAP_MISSING"):
            outcome = "SOURCE_READ_UNSUCCESSFUL"
            break
    async with sessions() as db, db.begin():
        source = await db.get(AppSetting, STATE_KEY)
        data = source.value if source else {}
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == before_quotes
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == before_notifications
        job = await db.get(CrawlJob, selected_job) if selected_job else None
        status = await db.get(ProviderStatus, "ihg")
        pending_details = await db.scalar(
            select(func.count())
            .select_from(CrawlJob)
            .where(
                CrawlJob.provider == "ihg",
                CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                CrawlJob.status.in_(("PENDING", "QUEUED", "RUNNING")),
                CrawlJob.payload["catalog_source"].as_string() == STATE_KEY,
            )
        )
        if outcome == "NO_DETAIL_EXECUTED" and pending_details:
            outcome = "DETAIL_QUEUED_PENDING"
        report = {
            "outcome": outcome,
            "job_id": selected_job,
            "job_status": job.status if job else None,
            "pending_detail_jobs": pending_details,
            "error": job.error_type if job else None,
            "error_message": job.error_message if job else None,
            "real_hotels_persisted": await db.scalar(
                select(func.count()).select_from(Hotel).where(Hotel.provider == "ihg", Hotel.active.is_(True))
            ),
            "sitemaps_total": len(data.get("maps", [])),
            "sitemaps_read": len(read_sitemaps(data)),
            "sitemaps_missing": len(data.get("missing_maps", [])),
            "sitemaps_failed": len(data.get("map_failures", {})),
            "raw_candidate_urls": len(data.get("urls", [])),
            "candidate_positions_processed": data.get("cursor", 0),
            "source_error": data.get("last_error"),
            "directory_quotes": 0,
            "notifications": 0,
            "total_existing_price_history": before_quotes,
            "provider_status": status.status if status else None,
            "blocked_until": status.blocked_until.isoformat() if status and status.blocked_until else None,
            "year_jobs_generated": await expand_global(db, settings) if outcome == "DETAIL_SUCCEEDED" else 0,
        }
        emit(json.dumps(report, ensure_ascii=True))
        return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--max-ticks", type=int, choices=range(1, 7), default=4)
    args = parser.parse_args()
    path = args.state_dir.resolve() / "catalog-audit.sqlite"
    assert path.is_file(), "Existing isolated directory database required"
    engine = create_async_engine("sqlite+aiosqlite:///" + str(path))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = FakeRedis(decode_responses=True)
    settings = Settings(
        _env_file=None,
        provider_overrides={"ihg": ProviderPolicy(enabled=True)},
        browser_channel="chrome",
        browser_proxy_url=args.proxy or "",
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
        telegram_bot_token="",
        telegram_chat_id="",
    )
    try:
        async with httpx.AsyncClient(proxy=args.proxy, follow_redirects=True) as client:
            report = await run(sessions, Queue(redis), settings, client, args.max_ticks)
        if report["outcome"] in {"SOURCE_READ_UNSUCCESSFUL", "DETAIL_READ_UNSUCCESSFUL"}:
            raise SystemExit(2)
    finally:
        try:
            await close_browser_sessions()
        finally:
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
