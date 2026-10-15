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
from app.services.ihg_catalogs import (
    directory_policy,
    discover_browser_directory,
    selected_directory_root,
)
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


async def ensure_audit_database(sessions, provider, *, require_marker=False):
    directory_policy(provider)
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, "audit:directory-only")
        if marker:
            assert marker.value == {"provider": provider}, "Different directory audit provider"
            return
        assert not require_marker, "Explicit database requires an existing isolated directory audit marker"
        for model in (CrawlJob, Hotel, PriceHistory, NotificationLog, AppSetting, ProviderStatus):
            assert not await db.scalar(select(func.count()).select_from(model)), (
                "Not an empty isolated directory audit database"
            )
        db.add(AppSetting(key="audit:directory-only", value={"provider": provider}))


def emit_progress(message):
    print(message, flush=True)


async def run_batch(sessions, queue, settings, limit=4, excluded=(), emit=emit_progress, provider="ihg"):
    """Every source is selected by the actual durable discovery scheduler.

    Stop on the first unsuccessful read; never reset cooldowns or retries.
    Excluded previously tested pages remain pending, not falsely successful.
    """
    await ensure_audit_database(sessions, provider)
    _, directory_url = directory_policy(provider)
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
            emit(
                json.dumps(
                    {
                        "stage": "directory_worker",
                        "job_id": job_id,
                        "source": job.payload["official_url"],
                    },
                    ensure_ascii=False,
                )
            )
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
        catalog = state.value if state else {}
        root = selected_directory_root(catalog, provider)
        report = {
            "provider": provider,
            "selected_directory_root": root,
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
            "coverage": directory_coverage(catalog, root, utcnow(), settings.discovery_interval_days),
        }
        assert quotes == previous_quotes and notifications == previous_notifications, (
            "Directory audit unexpectedly wrote prices/notifications"
        )
        emit(json.dumps(report, ensure_ascii=False))
        return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ihg", "marriott", "hyatt"), default="ihg")
    location = parser.add_mutually_exclusive_group(required=True)
    location.add_argument("--state-dir", type=Path, help="Dedicated audit folder, never production data")
    location.add_argument(
        "--database", type=Path, help="Resume an existing marked isolated directory audit database"
    )
    parser.add_argument(
        "--session-dir", type=Path, help="Existing project-owned browser session folder, never a user profile"
    )
    parser.add_argument(
        "--report", type=Path, help="Optional new JSON report path; existing reports are never overwritten"
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
    args = parser.parse_args(argv)
    if args.database:
        args.database = args.database.resolve()
        if not args.database.is_file():
            parser.error("--database must name an existing isolated audit database")
    if args.state_dir:
        args.state_dir = args.state_dir.resolve()
    if args.session_dir:
        args.session_dir = args.session_dir.resolve()
    if args.report:
        args.report = args.report.resolve()
        if args.report.exists():
            parser.error("Use a new --report path; existing evidence is preserved")
    return args


def audit_settings(args):
    browser = {"browser_session_dir": str(args.session_dir)} if args.session_dir else {}
    return Settings(
        _env_file=None,
        provider_overrides={name: ProviderPolicy(enabled=name == args.provider) for name in FACTORIES},
        browser_channel="chrome",
        browser_proxy_url=args.proxy or "",
        telegram_bot_token="",
        telegram_chat_id="",
        catalog_jobs_per_tick=1,
        global_jobs_per_tick=1,
        job_timeout_seconds=300,
        **browser,
    )


async def main(argv=None):
    args = parse_args(argv)
    if args.directory_diagnostics:
        install_directory_diagnostics(args.provider)
    if args.state_dir:
        args.state_dir.mkdir(parents=True, exist_ok=True)
    database = args.database or args.state_dir / "catalog-audit.sqlite"
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
    report_stream = args.report.open("x", encoding="utf-8") if args.report else None
    report = {"provider": args.provider, "database": str(database), "outcome": "RUNNING"}
    settings = audit_settings(args)
    engine = create_async_engine("sqlite+aiosqlite:///" + database.as_posix())
    redis = FakeRedis(decode_responses=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        if args.database:
            # Explicit resume never initializes or modifies an unmarked database.
            await ensure_audit_database(sessions, args.provider, require_marker=True)
        else:
            async with engine.begin() as connection:
                await connection.run_sync(Base.metadata.create_all)
        if args.follow_six_senses:
            assert args.provider == "ihg", "Public Six Senses hotel-link probe is IHG-only"
            from probe_ihg_sixsenses import install_public_link_probe

            install_public_link_probe(sessions)
        report.update(
            await run_batch(
                sessions, Queue(redis), settings, args.max_pages, args.exclude_url, provider=args.provider
            )
        )
        if report["outcome"] == "DIRECTORY_READ_UNSUCCESSFUL":
            raise SystemExit(2)
    except BaseException as exc:
        report["diagnostic_error_type"] = type(exc).__name__
        if report["outcome"] == "RUNNING":
            report["outcome"] = "AUDIT_ERROR"
        raise
    finally:
        try:
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        except BaseException:
            report["cleanup_status"] = "FAILED"
            raise
        finally:
            try:
                await redis.aclose()
                await engine.dispose()
            finally:
                if report_stream:
                    with report_stream:
                        json.dump(report, report_stream, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    asyncio.run(main())
