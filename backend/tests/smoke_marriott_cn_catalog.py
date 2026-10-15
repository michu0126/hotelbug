"""Run one official CN directory through the real Worker in an isolated database.

The resulting hotels generate (but do not execute) automatic date jobs. This
never connects to the deployed database or sends Telegram notifications.
"""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, PriceHistory, Rate, utcnow
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_catalog import ROOT, directory_url
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.ihg_catalogs import discover_marriott, request_directory_root, selected_directory_root
from app.services.jobs import enqueue
from app.services.queue import Queue
from app.services.watchlists import expand_global


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--official-url", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--inspect-cards", action="store_true")
    parser.add_argument(
        "--resume", action="store_true", help="Run the next due country from this saved CN root audit"
    )
    args = parser.parse_args()
    source = directory_url(args.official_url)
    if (args.database.exists() and not args.resume) or args.report.exists():
        parser.error("Use new diagnostic database/report paths; existing evidence is preserved")
    if args.resume and not args.database.exists():
        parser.error("Resume requires the existing diagnostic database")
    settings = Settings(
        _env_file=None,
        browser_channel="chrome",
        browser_session_dir=args.session_dir,
        browser_proxy_url="",
        provider_overrides={"marriott": ProviderPolicy(enabled=True)},
        global_jobs_per_tick=30,
        job_timeout_seconds=300,
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + args.database.resolve().as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = FakeRedis(decode_responses=True)
    queue = Queue(redis)
    report = {"source": source, "status": "RUNNING", "telegram_calls": 0, "price_requests": 0}
    factory = FACTORIES["marriott"]
    if args.inspect_cards:

        class DiagnosticProvider(MarriottBrowserProvider):
            async def discover_catalog(self, query):
                page = await self._get_page()
                report["official_query_responses"] = []
                report["public_catalog_payloads"] = []
                captures = []

                async def capture_catalog(response):
                    try:
                        body = response.request.post_data_json
                        report["public_catalog_payloads"].append(
                            {
                                "path": urlparse(response.url).path,
                                "request": {
                                    k: body[k] for k in ("operationName", "query", "variables") if k in body
                                },
                                "response": await asyncio.wait_for(response.json(), timeout=15),
                            }
                        )
                    except Exception as exc:
                        report["catalog_capture_error"] = type(exc).__name__

                def public_query(response):
                    url = urlparse(response.url)
                    if url.hostname == "www.marriott.com.cn" and url.path.startswith("/mi/query/"):
                        # No request headers, bodies, cookies or query strings.
                        report["official_query_responses"].append(
                            {"path": url.path, "status": response.status}
                        )
                        if (
                            url.path
                            in {
                                "/mi/query/phoenixShopSearchPropertiesByLocationInput",
                                "/mi/query/phoenixShopSearchPropertiesByLocation",
                            }
                            and response.status == 200
                        ):
                            # Observed public hotel/geography response only.
                            # Never export headers, cookies or user profile state.
                            captures.append(asyncio.create_task(capture_catalog(response)))

                page.on("response", public_query)
                try:
                    return await super().discover_catalog(query)
                finally:
                    if captures:
                        try:
                            await asyncio.wait_for(asyncio.gather(*captures), timeout=20)
                        except TimeoutError:
                            report["catalog_capture_error"] = "TimeoutError"
                    # Keep evidence even when waiting or parsing fails. Only
                    # public directory cards, not account headers or state.
                    try:
                        report["final_directory_url"] = directory_url(page.url)
                    except Exception:
                        report["final_directory_url"] = None
                    report["public_directory_cards"] = await page.locator(
                        '[data-testid="cardContainer-testid"]'
                    ).evaluate_all("""cards => cards.map(c => ({
                        name: c.querySelector('button.title-container')?.getAttribute('aria-label'),
                        links: [...c.querySelectorAll('a[href]')].map(a => ({text:a.textContent.trim(), href:a.href}))
                    }))""")
                    report["result_counter"] = await page.locator(
                        ".search-summary-container"
                    ).all_text_contents()
                    report["region_buttons"] = await page.locator('button[id$="_heading"]').evaluate_all(
                        "buttons => buttons.map(b => ({id:b.id, expanded:b.getAttribute('aria-expanded'), text:b.textContent.trim()}))"
                    )
                    report["pagination_controls"] = await page.locator("button, a[href]").evaluate_all(
                        """nodes => nodes.filter(n => /下一页|下一頁|next|pagination/i.test(
                            (n.getAttribute('aria-label') || '') + ' ' + n.className + ' ' + n.textContent
                        )).map(n => ({tag:n.tagName, text:n.textContent.trim(), label:n.getAttribute('aria-label'),
                            href:n.getAttribute('href'), class:n.className, disabled:n.disabled === true}))"""
                    )

        FACTORIES["marriott"] = lambda: DiagnosticProvider(
            browser_channel="chrome", session_dir=args.session_dir
        )
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=True, indent=2)
    try:
        if args.resume:
            async with sessions() as db:
                state = await db.get(AppSetting, "catalog:marriott")
                if not state or selected_directory_root(state.value, "marriott") != ROOT:
                    raise ValueError("Resume requires a previously selected CN directory audit")
                marker = await db.get(AppSetting, "audit:directory-only")
                if marker and marker.value != {"provider": "marriott"}:
                    raise ValueError("Different isolated audit provider")
                if not marker and (await db.scalars(select(PriceHistory))).first() is not None:
                    raise ValueError("An unmarked price database cannot become a directory audit")
            await discover_marriott(sessions, queue, settings)
            async with sessions() as db:
                job = await db.scalar(
                    select(CrawlJob)
                    .where(
                        CrawlJob.provider == "marriott",
                        CrawlJob.kind == JobKind.DISCOVER_CATALOG,
                        CrawlJob.status.in_(("PENDING", "QUEUED")),
                        CrawlJob.scheduled_at <= utcnow(),
                    )
                    .order_by(CrawlJob.scheduled_at, CrawlJob.id)
                    .limit(1)
                )
                if job is None:
                    raise ValueError("No directory is currently due; preserve any provider pause")
                job_id, source = job.id, job.payload["official_url"]
                report["source"] = source
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
            async with sessions() as db, db.begin():
                job = await enqueue(
                    db,
                    JobInput(
                        provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": source}
                    ),
                )
                job_id = job.id
                await request_directory_root(db, job)
        async with sessions() as db, db.begin():
            marker = await db.get(AppSetting, "audit:directory-only")
            if not marker:
                db.add(AppSetting(key="audit:directory-only", value={"provider": "marriott"}))
            report["existing_rates"] = await db.scalar(select(func.count()).select_from(Rate))
            report["existing_price_history"] = await db.scalar(select(func.count()).select_from(PriceHistory))
        await queue.put(job_id, 90)
        print("stage=official_directory_worker", flush=True)
        await process_one(sessions, queue, settings)
        async with sessions() as db, db.begin():
            job = await db.get(CrawlJob, job_id)
            report.update(
                worker_status=job.status, error_type=job.error_type, error_message=job.error_message
            )
            hotels = (await db.scalars(select(Hotel).order_by(Hotel.provider_hotel_id))).all()
            report["hotels"] = [
                {
                    "code": h.provider_hotel_id,
                    "name": h.hotel_name,
                    "country": h.country,
                    "url": h.official_url,
                }
                for h in hotels
            ]
            catalog = await db.get(AppSetting, "catalog:marriott")
            report["catalog_page"] = catalog.value.get("pages", {}).get(source) if catalog else None
            report["date_jobs_created"] = (
                await expand_global(db, settings) if job.status == "SUCCEEDED" else 0
            )
            dates = (await db.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))).all()
            report["date_jobs"] = [
                {
                    "hotel_id": j.hotel_id,
                    "check_in": j.check_in.isoformat(),
                    "check_out": j.check_out.isoformat(),
                }
                for j in dates
            ]
            assert await db.scalar(select(func.count()).select_from(Rate)) == report["existing_rates"]
            assert (
                await db.scalar(select(func.count()).select_from(PriceHistory))
                == report["existing_price_history"]
            )
            report["status"] = "SUCCEEDED" if job.status == "SUCCEEDED" else "FAILED"
    except BaseException as exc:
        report.update(status="FAILED", diagnostic_error_type=type(exc).__name__)
        raise
    finally:
        FACTORIES["marriott"] = factory
        try:
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        except BaseException:
            report.update(cleanup_status="FAILED", status="FAILED")
            raise
        finally:
            await redis.aclose()
            await engine.dispose()
            args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
            print(
                json.dumps(
                    {
                        k: report.get(k)
                        for k in (
                            "status",
                            "worker_status",
                            "error_type",
                            "error_message",
                            "date_jobs_created",
                            "cleanup_status",
                        )
                    },
                    ensure_ascii=True,
                ),
                flush=True,
            )
    if report["status"] != "SUCCEEDED":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
