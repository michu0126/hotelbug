"""Persistent live Accor/GHA/Hilton directory audit; no price or notification calls.

Uses production discovery and Worker with an isolated saved database. A new
process resumes the official cursor rather than repeatedly testing the first
hotel. Previously successful details and explicit exclusions are not re-run.
"""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse

import httpx
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
from app.providers.accor_browser import hotel_code, is_explicit_test_hotel
from app.providers.browser_session import close_browser_sessions
from app.providers.gha_catalog import candidate_progress as gha_candidate_progress
from app.providers.gha_catalog import hotel_candidate_url as gha_hotel_candidate
from app.providers.registry import FACTORIES
from app.schemas.domain import JobKind
from app.services.catalogs import discover_accor, discover_gha, discover_hilton, read_sitemaps
from app.services.queue import Queue
from app.services.watchlists import expand_global

DISCOVER = {"accor": discover_accor, "gha": discover_gha, "hilton": discover_hilton}
MARKER = "audit:sitemap-catalog"


def public_location_projection(html):
    """Read only public Hotel schema/address fields, never arbitrary page scripts."""
    soup = BeautifulSoup(html, "html.parser")
    observed = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, dict):
            types = value.get("@type", [])
            types = [types] if isinstance(types, str) else types
            if isinstance(types, list) and any(
                item in {"Hotel", "LodgingBusiness", "Resort"} for item in types if isinstance(item, str)
            ):
                address = value.get("address")
                geo = value.get("geo")
                observed.append(
                    {
                        "type": types,
                        "name": value.get("name") if isinstance(value.get("name"), str) else None,
                        "address": {
                            key: address.get(key)
                            for key in (
                                "streetAddress",
                                "addressLocality",
                                "addressRegion",
                                "postalCode",
                                "addressCountry",
                            )
                            if isinstance(address.get(key), str)
                        }
                        if isinstance(address, dict)
                        else None,
                        "geo": {
                            key: geo.get(key)
                            for key in ("latitude", "longitude")
                            if isinstance(geo.get(key), (str, float, int))
                        }
                        if isinstance(geo, dict)
                        else None,
                    }
                )
            if "@graph" in value:
                visit(value["@graph"])

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            visit(json.loads(script.get_text()))
        except (ValueError, TypeError):
            continue
    return {
        "public_hotel_schema": observed,
        "rendered_address_elements": [
            element.get_text(" ", strip=True)[:500] for element in soup.select("address")
        ],
        "public_hotel_headings": [
            {"tag": item.name, "text": item.get_text(" ", strip=True)[:250]}
            for item in soup.select("h1,h2,h3")
        ],
        "public_location_sections": [
            item.parent.get_text(" ", strip=True)[:1500]
            for item in soup.select("h1,h2,h3,h4,h5,h6")
            if item.get_text(" ", strip=True).casefold()
            in {
                "location",
                "hotel location",
                "address",
                "contact",
                "contact details",
                "hotel information",
                "getting here",
            }
            and item.parent
            and item.parent.name not in {"body", "html"}
        ],
        "public_destination_links": [
            {"text": item.get_text(" ", strip=True), "path": urlparse(item.get("href")).path}
            for item in soup.select("a[href]")
            if "/destinations/" in urlparse(item.get("href")).path
        ],
        "public_heading_context": [
            item.parent.parent.get_text(" ", strip=True)[:1500]
            for item in soup.select("h1")
            if item.parent and item.parent.parent and item.parent.parent.name not in {"body", "html"}
        ],
        "public_heading_markup": [
            [
                {
                    "tag": part.name,
                    "class": part.get("class", []),
                    "text": part.get_text(" ", strip=True)[:500],
                }
                for part in item.parent.parent.select("h1,p,span,div")
            ]
            for item in soup.select("h1")
            if item.parent and item.parent.parent and item.parent.parent.name not in {"body", "html"}
        ],
    }


def install_location_diagnostics(provider):
    factory = FACTORIES[provider]

    def diagnostic_factory():
        instance = factory()
        original = instance.get_hotel_details

        async def read_details(hotel_id):
            # GHA has already navigated the official hotel page to obtain its
            # BOOK NOW ID. Read that same loaded page before normal navigation.
            page = instance._page
            if provider == "gha" and page and not page.is_closed():
                parsed = urlparse(page.url)
                if parsed.hostname == "www.ghadiscovery.com" and parsed.path != "/booking/select_room":
                    print(
                        json.dumps(
                            {
                                "public_location_source": parsed._replace(query="", fragment="").geturl(),
                                **public_location_projection(await page.content()),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
            details = await original(hotel_id)
            # Accor opens its public hotel page inside get_hotel_details. Read
            # that same page after details, never open an extra diagnostic URL.
            page = instance._page
            if provider == "accor" and page and not page.is_closed():
                parsed = urlparse(page.url)
                if (
                    parsed.hostname == "all.accor.com"
                    and parsed.path == f"/hotel/{hotel_code(hotel_id)}/index.en.shtml"
                ):
                    content = await page.content()
                    print(
                        json.dumps(
                            {
                                "public_location_source": parsed._replace(query="", fragment="").geturl(),
                                "public_accor_location_markup": [
                                    [
                                        {
                                            "tag": part.name,
                                            "class": part.get("class", []),
                                            "itemprop": part.get("itemprop"),
                                            "text": part.get_text(" ", strip=True)[:300],
                                        }
                                        for part in title.parent.select("p,address,h3,[itemprop]")
                                    ]
                                    for title in BeautifulSoup(content, "html.parser").select("h2")
                                    if title.get_text(" ", strip=True) == "Hotel location"
                                ],
                                **public_location_projection(content),
                            },
                            ensure_ascii=True,
                        ),
                        flush=True,
                    )
            return details

        instance.get_hotel_details = read_details
        return instance

    FACTORIES[provider] = diagnostic_factory


def source_key(provider, payload):
    if provider == "accor":
        return hotel_code(payload["provider_hotel_id"])
    return payload["official_url"]


def checked_source(provider, value):
    if provider == "accor":
        return hotel_code(value)
    url = urlparse(value)
    host = {"gha": "www.ghadiscovery.com", "hilton": "www.hilton.com"}[provider]
    assert url.scheme == "https" and url.netloc == host and not url.query and not url.fragment, (
        "Exclusions must be public official hotel URLs"
    )
    return value.rstrip("/")


async def ensure_audit_database(sessions, provider):
    assert provider in DISCOVER, "Unsupported sitemap audit provider"
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, MARKER)
        if marker:
            assert marker.value == {"provider": provider}, "Different audit provider"
            return
        for model in (CrawlJob, Hotel, PriceHistory, NotificationLog, AppSetting, ProviderStatus):
            assert not await db.scalar(select(func.count()).select_from(model)), (
                "Refusing an existing database without its sitemap audit marker"
            )
        db.add(AppSetting(key=MARKER, value={"provider": provider}))


async def run_batch(sessions, queue, settings, client, provider, limit=2, excluded=(), emit=print):
    await ensure_audit_database(sessions, provider)
    excluded = {checked_source(provider, item) for item in excluded}
    async with sessions() as db, db.begin():
        if provider == "accor":
            # Reconcile the already observed test name in this isolated saved
            # audit without re-fetching it or deleting its original evidence.
            existing = (
                await db.scalars(select(Hotel).where(Hotel.provider == provider, Hotel.active.is_(True)))
            ).all()
            for hotel in existing:
                if is_explicit_test_hotel(hotel.hotel_name):
                    hotel.active = False
        previous_quotes = await db.scalar(select(func.count()).select_from(PriceHistory))
        previous_notifications = await db.scalar(select(func.count()).select_from(NotificationLog))
        saved = await db.get(AppSetting, "audit:excluded-sitemap-details")
        excluded.update(checked_source(provider, item) for item in (saved.value["sources"] if saved else []))
        if saved:
            saved.value = {"sources": sorted(excluded)}
        else:
            db.add(AppSetting(key="audit:excluded-sitemap-details", value={"sources": sorted(excluded)}))
        succeeded = (
            await db.scalars(
                select(CrawlJob).where(
                    CrawlJob.provider == provider,
                    CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                    CrawlJob.status == "SUCCEEDED",
                )
            )
        ).all()
        already_read = {source_key(provider, job.payload) for job in succeeded}
    completed = 0
    non_hotel_jobs_cancelled = 0
    outcome = "BATCH_COMPLETE"

    async def next_job():
        async with sessions() as db:
            candidates = (
                await db.scalars(
                    select(CrawlJob)
                    .where(
                        CrawlJob.provider == provider,
                        CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                        CrawlJob.status.in_(("PENDING", "QUEUED")),
                        CrawlJob.scheduled_at <= utcnow(),
                    )
                    .order_by(CrawlJob.scheduled_at, CrawlJob.id)
                )
            ).all()
            return next(
                (
                    job
                    for job in candidates
                    if source_key(provider, job.payload) not in excluded | already_read
                ),
                None,
            )

    for _ in range(limit + len(excluded) + len(already_read) + 1):
        if completed == limit:
            break
        async with sessions() as db:
            now = utcnow()
            status = await db.get(ProviderStatus, provider)
            if status and status.blocked_until and status.blocked_until.replace(tzinfo=now.tzinfo) > now:
                outcome = "PROVIDER_PAUSED"
                break
        job = await next_job()
        if job is None:
            added = await DISCOVER[provider](sessions, queue, settings, client)
            async with sessions() as db:
                row = await db.get(AppSetting, "catalog:" + provider)
                data = row.value if row else {}
            if data.get("last_error") not in (None, "SITEMAP_MISSING"):
                outcome = "SOURCE_READ_UNSUCCESSFUL"
                break
            job = await next_job()
            if job is None:
                if added:
                    # Exclusions stay pending; only the normal source cursor advances.
                    continue
                outcome = "NO_DETAIL_DUE"
                break
        identity = source_key(provider, job.payload)
        await queue.put(job.id, job.priority)
        await process_one(sessions, queue, settings)
        async with sessions() as db:
            current = await db.get(CrawlJob, job.id)
            now = utcnow()
            delay = (current.scheduled_at.replace(tzinfo=now.tzinfo) - now).total_seconds()
            normal_interval = current.status == "PENDING" and current.error_type is None
        if normal_interval and 0 < delay <= settings.provider_policy(provider).interval_seconds + 2:
            await asyncio.sleep(delay + 0.1)
            await queue.put(job.id, job.priority)
            await process_one(sessions, queue, settings)
        async with sessions() as db:
            current = await db.get(CrawlJob, job.id)
            emit(
                json.dumps(
                    {
                        "job_id": job.id,
                        "source": identity,
                        "status": current.status,
                        "error": current.error_type,
                    },
                    ensure_ascii=False,
                )
            )
            if current.status == "CANCELLED" and provider == "gha" and gha_hotel_candidate(identity) is None:
                non_hotel_jobs_cancelled += 1
                continue
            if current.status != "SUCCEEDED":
                outcome = "DETAIL_READ_UNSUCCESSFUL"
                break
        already_read.add(identity)
        completed += 1

    async with sessions() as db, db.begin():
        row = await db.get(AppSetting, "catalog:" + provider)
        data = row.value if row else {}
        status = await db.get(ProviderStatus, provider)
        quotes = await db.scalar(select(func.count()).select_from(PriceHistory))
        notifications = await db.scalar(select(func.count()).select_from(NotificationLog))
        assert quotes == previous_quotes and notifications == previous_notifications, (
            "Directory audit unexpectedly wrote prices or notifications"
        )
        candidate_counts = (
            gha_candidate_progress(data.get("urls", []), data.get("cursor", 0)) if provider == "gha" else None
        )
        report = {
            "provider": provider,
            "outcome": outcome,
            "details_succeeded_this_run": completed,
            "non_hotel_jobs_cancelled_this_run": non_hotel_jobs_cancelled,
            "real_hotels_persisted": await db.scalar(
                select(func.count()).select_from(Hotel).where(Hotel.active.is_(True))
            ),
            "inactive_catalog_entries": await db.scalar(
                select(func.count()).select_from(Hotel).where(Hotel.active.is_(False))
            ),
            "candidates": candidate_counts["hotel_candidates"]
            if candidate_counts
            else len(data.get("codes", data.get("urls", []))),
            "candidate_cursor": data.get("cursor", 0),
            "excluded_non_hotel_entries": candidate_counts["excluded_non_hotel_entries"]
            if candidate_counts
            else 0,
            "raw_source_entries": len(data.get("codes", data.get("urls", []))),
            "sitemaps_total": len(data.get("maps", [])),
            "sitemaps_read": len(read_sitemaps(data)),
            "sitemaps_missing": len(data.get("missing_maps", [])),
            "sitemaps_failed": len(data.get("map_failures", {})),
            "source_error": data.get("last_error"),
            "provider_status": status.status if status else None,
            "blocked_until": status.blocked_until.isoformat() if status and status.blocked_until else None,
            "next_source_read_at": data.get("next_fetch_at", data.get("refresh_at")),
            "excluded_previously_tested_sources": sorted(excluded),
            "directory_quotes": quotes - previous_quotes,
            "notifications": notifications - previous_notifications,
            "total_existing_price_history": quotes,
            "year_jobs_generated": await expand_global(db, settings) if completed else 0,
        }
        emit(json.dumps(report, ensure_ascii=False))
        return report


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(DISCOVER), required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument("--max-hotels", type=int, choices=range(1, 11), default=2)
    parser.add_argument("--exclude-source", action="append", default=[])
    parser.add_argument(
        "--location-diagnostics",
        action="store_true",
        help="Project-owned public hotel schema/address only; no tokens or page-script dumps",
    )
    args = parser.parse_args()
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
    if args.location_diagnostics:
        install_location_diagnostics(args.provider)
    engine = create_async_engine("sqlite+aiosqlite:///" + str(directory / "catalog-audit.sqlite"))
    redis = FakeRedis(decode_responses=True)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with httpx.AsyncClient(proxy=args.proxy, follow_redirects=True) as client:
            report = await run_batch(
                sessions, Queue(redis), settings, client, args.provider, args.max_hotels, args.exclude_source
            )
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
