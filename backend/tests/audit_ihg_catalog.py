"""Resumable live worldwide directory audit using production discovery/Worker.

Keeps evidence across runs instead of re-testing a temporary root/first leaf.
Does not execute price jobs, health probes or any notification delivery.
"""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import (
    AppSetting,
    Base,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceHistory,
    ProviderStatus,
    utcnow,
)
from app.providers.browser_session import close_browser_sessions
from app.providers.ihg_page import PROPERTY_PATH
from app.providers.registry import FACTORIES
from app.schemas.domain import JobKind
from app.services.catalog_coverage import directory_coverage
from app.services.ihg_catalogs import directory_policy, discover_browser_directory
from app.services.queue import Queue
from app.services.watchlists import expand_global


def public_ihg_directory_controls(html):
    """Public directory controls/unsupported hotel paths, never private query values."""
    soup = BeautifulSoup(html, "html.parser")
    counter = soup.select_one('[data-render-hotel-count="true"] #cmp-card__title-bar-count')
    heading = soup.select_one("h1")
    controls = []
    for button in soup.select('button, a[role="button"]'):
        label = button.get_text(" ", strip=True)
        if any(word in label.casefold() for word in ("show more", "load more", "next", "view all")):
            controls.append(
                {
                    "text": label,
                    "aria_label": button.get("aria-label"),
                    "testid": button.get("data-testid"),
                    "class": button.get("class", []),
                    "disabled": button.has_attr("disabled") or button.get("aria-disabled") == "true",
                }
            )
    unsupported_links = []
    for link in soup.select("h4 a[href]"):
        parsed = urlparse(urljoin("https://www.ihg.com/", link["href"]))
        name = link.get_text(" ", strip=True)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.ihg.com"
            or parsed.query
            or parsed.fragment
            or not PROPERTY_PATH.fullmatch(parsed.path)
            or not name
        ):
            unsupported_links.append(
                {
                    "name": name,
                    "scheme": parsed.scheme,
                    "hostname": parsed.hostname,
                    "path": parsed.path,
                    "has_query": bool(parsed.query),
                    "has_fragment": bool(parsed.fragment),
                }
            )
    return {
        "heading": heading.get_text(" ", strip=True) if heading else None,
        "counter": counter.get_text(strip=True) if counter else None,
        "hotel_heading_links": len(soup.select("h4 a[href]")),
        "featured_headings": [
            node.get_text(" ", strip=True)
            for node in soup.select("h2")
            if node.get_text(" ", strip=True).startswith("Featured Hotels in ")
        ],
        "pagination_controls": controls,
        "unsupported_hotel_links": unsupported_links,
    }


def install_directory_diagnostics(provider):
    """Same project-owned page after the normal request, no extra navigation."""
    assert provider == "ihg", "Directory control diagnostic currently supports IHG only"
    original_factory = FACTORIES[provider]

    def diagnostic_factory():
        instance = original_factory()
        original = instance.discover_catalog

        async def discover(query):
            try:
                return await original(query)
            finally:
                page = instance._page
                if page and not page.is_closed():
                    try:
                        _, validate = directory_policy(provider)
                        public_source = validate(page.url)
                        print(
                            json.dumps(
                                {
                                    "public_ihg_directory_source": public_source,
                                    "public_ihg_directory_controls": public_ihg_directory_controls(
                                        await page.content()
                                    ),
                                },
                                ensure_ascii=True,
                            ),
                            flush=True,
                        )
                    except Exception:
                        print(json.dumps({"public_ihg_directory_controls_unavailable": True}), flush=True)

        instance.discover_catalog = discover
        return instance

    FACTORIES[provider] = diagnostic_factory


async def ensure_audit_database(sessions, provider):
    directory_policy(provider)
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, "audit:directory-only")
        if marker:
            assert marker.value == {"provider": provider}, "Different directory audit provider"
            return
        for model in (CrawlJob, Hotel, PriceHistory, NotificationLog, AppSetting, ProviderStatus):
            assert not await db.scalar(select(func.count()).select_from(model)), (
                "Not an empty isolated directory audit database"
            )
        db.add(AppSetting(key="audit:directory-only", value={"provider": provider}))


async def run_batch(sessions, queue, settings, limit=4, excluded=(), emit=print, provider="ihg"):
    """Every source is selected by the actual durable discovery scheduler.

    Stop on the first unsuccessful read; never reset cooldowns or retries.
    Excluded previously tested pages remain pending, not falsely successful.
    """
    await ensure_audit_database(sessions, provider)
    root, directory_url = directory_policy(provider)
    excluded = {directory_url(url) for url in excluded}
    async with sessions() as db, db.begin():
        previous_quotes = await db.scalar(select(func.count()).select_from(PriceHistory))
        previous_notifications = await db.scalar(select(func.count()).select_from(NotificationLog))
        saved = await db.get(AppSetting, "audit:excluded-directories")
        excluded.update(directory_url(url) for url in (saved.value.get("sources", []) if saved else []))
        if saved:
            saved.value = {"sources": sorted(excluded)}
        else:
            db.add(AppSetting(key="audit:excluded-directories", value={"sources": sorted(excluded)}))
    completed = 0
    outcome = "BATCH_COMPLETE"
    for _ in range(limit + len(excluded) + 1):
        if completed >= limit:
            break
        async with sessions() as db:
            now = utcnow()
            state = await db.get(ProviderStatus, provider)
            if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                outcome = "PROVIDER_PAUSED"
                break
        await discover_browser_directory(sessions, queue, settings, provider)
        async with sessions() as db:
            candidates = (
                await db.scalars(
                    select(CrawlJob)
                    .where(
                        CrawlJob.provider == provider,
                        CrawlJob.kind == JobKind.DISCOVER_CATALOG,
                        CrawlJob.status.in_(("PENDING", "QUEUED")),
                        CrawlJob.scheduled_at <= utcnow(),
                    )
                    .order_by(CrawlJob.scheduled_at, CrawlJob.id)
                )
            ).all()
            job = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.payload.get("official_url") not in excluded
                ),
                None,
            )
            if job is None:
                outcome = "NO_DIRECTORY_DUE"
                continue
            job_id, priority = job.id, job.priority
        await queue.put(job_id, priority)
        await process_one(sessions, queue, settings)
        # Worker may have deferred an otherwise healthy task solely for its
        # ordinary provider interval. Wait for that saved timestamp, not a
        # changed profile/job/cooldown. Actual provider errors stop the batch.
        async with sessions() as db:
            current = await db.get(CrawlJob, job_id)
            now = utcnow()
            delay = (current.scheduled_at.replace(tzinfo=now.tzinfo) - now).total_seconds()
            limited = current.status == "PENDING" and current.error_type is None
        if limited and 0 < delay <= settings.provider_policy(provider).interval_seconds + 2:
            await asyncio.sleep(delay + 0.1)
            await queue.put(job_id, priority)
            await process_one(sessions, queue, settings)
        async with sessions() as db:
            current = await db.get(CrawlJob, job_id)
            emit(
                json.dumps(
                    {
                        "job_id": job_id,
                        "source": current.payload["official_url"],
                        "status": current.status,
                        "error": current.error_type,
                    },
                    ensure_ascii=False,
                )
            )
            if current.status != "SUCCEEDED":
                outcome = "DIRECTORY_READ_UNSUCCESSFUL"
                break
        completed += 1
        outcome = "BATCH_COMPLETE"
    async with sessions() as db, db.begin():
        state = await db.get(AppSetting, "catalog:" + provider)
        provider_state = await db.get(ProviderStatus, provider)
        hotels = await db.scalar(select(func.count()).select_from(Hotel))
        quotes = await db.scalar(select(func.count()).select_from(PriceHistory))
        notifications = await db.scalar(select(func.count()).select_from(NotificationLog))
        # This demonstrates the real connection to yearly scheduling, without
        # executing/re-testing a quote or writing any invented price.
        generated = await expand_global(db, settings) if completed else 0
        report = {
            "provider": provider,
            "outcome": outcome,
            "directory_jobs_succeeded_this_run": completed,
            "real_hotels_persisted": hotels,
            "directory_quotes": quotes - previous_quotes,
            "notifications": notifications - previous_notifications,
            "total_existing_price_history": quotes,
            "year_jobs_generated": generated,
            "excluded_previously_tested_sources": sorted(excluded),
            "provider_status": provider_state.status if provider_state else None,
            "blocked_until": provider_state.blocked_until.isoformat()
            if provider_state and provider_state.blocked_until
            else None,
            "coverage": directory_coverage(
                state.value if state else {}, root, utcnow(), settings.discovery_interval_days
            ),
        }
        assert quotes == previous_quotes and notifications == previous_notifications, (
            "Directory audit unexpectedly wrote prices/notifications"
        )
        emit(json.dumps(report, ensure_ascii=False))
        return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ihg", "marriott", "hyatt"), default="ihg")
    parser.add_argument(
        "--state-dir", type=Path, required=True, help="Dedicated audit folder, never production data"
    )
    parser.add_argument("--proxy")
    parser.add_argument("--max-pages", type=int, choices=range(1, 21), default=4)
    parser.add_argument("--exclude-url", action="append", default=[])
    parser.add_argument(
        "--directory-diagnostics", action="store_true", help="IHG same-page public directory controls only"
    )
    parser.add_argument(
        "--follow-six-senses",
        action="store_true",
        help="IHG only: at most one normal public hotel-link visit across the batch, no quote request",
    )
    args = parser.parse_args()
    if args.directory_diagnostics:
        install_directory_diagnostics(args.provider)
    directory = args.state_dir.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        _env_file=None,
        provider_overrides={name: ProviderPolicy(enabled=name == args.provider) for name in FACTORIES},
        browser_channel="chrome",
        browser_proxy_url=args.proxy or "",
        telegram_bot_token="",
        telegram_chat_id="",
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + str(directory / "catalog-audit.sqlite"))
    redis = FakeRedis(decode_responses=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        if args.follow_six_senses:
            assert args.provider == "ihg", "Public Six Senses hotel-link probe is IHG-only"
            from probe_ihg_sixsenses import install_public_link_probe

            install_public_link_probe(sessions)
        report = await run_batch(
            sessions, Queue(redis), settings, args.max_pages, args.exclude_url, provider=args.provider
        )
        if report["outcome"] == "DIRECTORY_READ_UNSUCCESSFUL":
            raise SystemExit(2)
    finally:
        try:
            await close_browser_sessions()
        finally:
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
