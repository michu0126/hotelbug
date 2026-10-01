"""Persistent worldwide IHG directory jobs; browsers run only in the Worker."""

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.providers.ihg_catalog import ROOT, directory_url
from app.schemas.domain import CatalogPageData, JobInput, JobKind
from app.services.jobs import LIVE, enqueue
from app.services.rates import upsert_hotel

STATE_KEY = "catalog:ihg"


async def catalog_state(session):
    state = await session.scalar(select(AppSetting).where(AppSetting.key == STATE_KEY).with_for_update())
    if state is None:
        try:
            async with session.begin_nested():
                state = AppSetting(key=STATE_KEY, value={"pages": {ROOT: {"next_scan_at": 0}}, "urls": []})
                session.add(state)
                await session.flush()
        except IntegrityError:
            state = await session.scalar(
                select(AppSetting).where(AppSetting.key == STATE_KEY).with_for_update()
            )
    return state


async def discover_ihg(sessions, queue, settings, client=None) -> int:
    if not settings.global_monitoring_enabled or not settings.provider_policy("ihg").enabled:
        return 0
    token = await queue.lock("hotelbug:catalog:ihg", 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            now = utcnow()
            provider = await session.get(ProviderStatus, "ihg")
            if (
                provider
                and provider.blocked_until
                and provider.blocked_until.replace(tzinfo=now.tzinfo) > now
            ):
                return 0
            state = await catalog_state(session)
            data = dict(state.value)
            pages = {url: dict(info) for url, info in data.get("pages", {}).items()}
            pages.setdefault(ROOT, {"next_scan_at": 0})
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(settings.catalog_jobs_per_tick, max(0, settings.max_pending_jobs - pending))
            live = (
                await session.scalars(
                    select(CrawlJob).where(
                        CrawlJob.provider == "ihg",
                        CrawlJob.kind == JobKind.DISCOVER_CATALOG,
                        CrawlJob.status.in_(LIVE),
                    )
                )
            ).all()
            live_urls = {j.payload.get("official_url") for j in live}
            created = 0
            for url, info in pages.items():
                if created >= budget:
                    break
                if url in live_urls or info.get("next_scan_at", 0) > now.timestamp():
                    continue
                job = await enqueue(
                    session,
                    JobInput(
                        provider="ihg",
                        kind=JobKind.DISCOVER_CATALOG,
                        priority=70,
                        payload={"official_url": directory_url(url)},
                    ),
                )
                info.update(status="QUEUED", job_id=job.id)
                created += 1
            state.value = {**data, "pages": pages}
            return created
    finally:
        await queue.unlock("hotelbug:catalog:ihg", token)


async def persist_catalog(session, job, result: CatalogPageData, settings):
    if result.provider != job.provider or directory_url(str(result.source_url)) != directory_url(
        job.payload["official_url"]
    ):
        raise ValueError("catalog source does not match job")
    state = await catalog_state(session)
    data = dict(state.value)
    pages = {url: dict(info) for url, info in data.get("pages", {}).items()}
    for url in result.directory_urls:
        pages.setdefault(directory_url(str(url)), {"next_scan_at": 0})
    urls = list(data.get("urls", []))
    seen = set(urls)
    for hotel in result.hotels:
        await upsert_hotel(session, hotel)
        url = str(hotel.official_url)
        if url not in seen:
            urls.append(url)
            seen.add(url)
    now = utcnow()
    source = directory_url(str(result.source_url))
    error = (
        None
        if result.complete
        else "DIRECTORY_COVERAGE_UNCONFIRMED"
        if result.reported_total is None
        else "DIRECTORY_PARTIAL"
    )
    pages[source] = {
        "status": "SUCCEEDED" if result.complete else "PARTIAL",
        "reported_total": result.reported_total,
        "parsed_hotels": len(result.hotels),
        "last_scan_at": now.timestamp(),
        "next_scan_at": (
            now
            + (timedelta(days=settings.discovery_interval_days) if result.complete else timedelta(hours=1))
        ).timestamp(),
        "last_error": error,
        "job_id": job.id,
    }
    state.value = {**data, "pages": pages, "urls": urls, "cursor": len(urls), "last_error": error}


async def record_catalog_failure(session, job, error):
    state = await catalog_state(session)
    data = dict(state.value)
    pages = {url: dict(info) for url, info in data.get("pages", {}).items()}
    # Invalid user-supplied URLs are not inserted into the automatic directory walk.
    try:
        url = directory_url(job.payload.get("official_url", ""))
    except Exception:
        return
    pages[url] = {
        **pages.get(url, {}),
        "status": "FAILED",
        "last_error": error.code.value,
        "next_scan_at": (utcnow() + timedelta(hours=1)).timestamp(),
        "job_id": job.id,
    }
    state.value = {**data, "pages": pages, "last_error": error.code.value}
