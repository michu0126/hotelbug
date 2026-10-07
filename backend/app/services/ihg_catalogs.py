"""Persistent worldwide public directory jobs; browsers run only in the Worker.

The original module name remains compatible with existing IHG callers.
"""

from collections import deque
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.errors import ErrorCode
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.providers.ihg_catalog import ROOT as IHG_ROOT
from app.providers.ihg_catalog import directory_url as ihg_directory_url
from app.schemas.domain import CatalogPageData, JobInput, JobKind
from app.services.jobs import CATALOG_PRIORITY, LIVE, enqueue
from app.services.rates import upsert_hotel

STATE_KEY = "catalog:ihg"


def directory_policy(provider):
    if provider == "ihg":
        return IHG_ROOT, ihg_directory_url
    if provider == "marriott":
        from app.providers.marriott_catalog import ROOT, directory_url

        return ROOT, directory_url
    if provider == "hyatt":
        from app.providers.hyatt_catalog import ROOT, directory_url

        return ROOT, directory_url
    raise ValueError("Provider public directory not implemented")


async def catalog_state(session, provider="ihg"):
    root, _ = directory_policy(provider)
    key = "catalog:" + provider
    state = await session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
    if state is None:
        try:
            async with session.begin_nested():
                state = AppSetting(key=key, value={"pages": {root: {"next_scan_at": 0}}, "urls": []})
                session.add(state)
                await session.flush()
        except IntegrityError:
            state = await session.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
    return state


async def discover_ihg(sessions, queue, settings, client=None) -> int:
    return await discover_browser_directory(sessions, queue, settings, "ihg")


async def discover_marriott(sessions, queue, settings, client=None) -> int:
    return await discover_browser_directory(sessions, queue, settings, "marriott")


async def discover_hyatt(sessions, queue, settings, client=None) -> int:
    return await discover_browser_directory(sessions, queue, settings, "hyatt")


async def discover_browser_directory(sessions, queue, settings, provider_name) -> int:
    root, directory_url = directory_policy(provider_name)
    lock_key = "hotelbug:catalog:" + provider_name
    if not settings.global_monitoring_enabled or not settings.provider_policy(provider_name).enabled:
        return 0
    token = await queue.lock(lock_key, 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            now = utcnow()
            provider = await session.get(ProviderStatus, provider_name)
            if (
                provider
                and provider.blocked_until
                and provider.blocked_until.replace(tzinfo=now.tzinfo) > now
            ):
                return 0
            state = await catalog_state(session, provider_name)
            data = dict(state.value)
            pages = {url: dict(info) for url, info in data.get("pages", {}).items()}
            pages.setdefault(root, {"next_scan_at": 0})
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(settings.catalog_jobs_per_tick, max(0, settings.max_pending_jobs - pending))
            live = (
                await session.scalars(
                    select(CrawlJob).where(
                        CrawlJob.provider == provider_name,
                        CrawlJob.kind == JobKind.DISCOVER_CATALOG,
                        CrawlJob.status.in_(LIVE),
                    )
                )
            ).all()
            live_urls = {j.payload.get("official_url") for j in live}
            unseen = deque()
            repairs = []
            for url, info in pages.items():
                needs_audit = info.get("status") in {"SUCCEEDED", "PARTIAL"} and (
                    "directory_urls" not in info or "hotel_ids" not in info
                )
                if url in live_urls or (not needs_audit and info.get("next_scan_at", 0) > now.timestamp()):
                    continue
                if "last_scan_at" not in info and info.get("status") not in {
                    "SUCCEEDED",
                    "PARTIAL",
                    "FAILED",
                }:
                    unseen.append((url, info))
                else:
                    repairs.append((0 if needs_audit else info.get("next_scan_at", 0), url, info))
            # Partial pages can become due hourly before a global first pass
            # finishes. Persistently alternate lanes so neither unseen places
            # nor old/failed pages monopolizes a bounded discovery tick.
            repair_queue = deque((url, info) for _, url, info in sorted(repairs, key=lambda item: item[:2]))
            repair_last = bool(data.get("repair_last", False))
            created = 0
            while created < budget and (unseen or repair_queue):
                use_repair = bool(repair_queue) and (not unseen or not repair_last)
                url, info = (repair_queue if use_repair else unseen).popleft()
                job = await enqueue(
                    session,
                    JobInput(
                        provider=provider_name,
                        kind=JobKind.DISCOVER_CATALOG,
                        priority=CATALOG_PRIORITY,
                        payload={"official_url": directory_url(url)},
                    ),
                )
                info.update(status="QUEUED", job_id=job.id)
                repair_last = use_repair
                created += 1
            if created:
                data["repair_last"] = repair_last
            state.value = {**data, "pages": pages}
            return created
    finally:
        await queue.unlock(lock_key, token)


async def persist_catalog(session, job, result: CatalogPageData, settings):
    _, directory_url = directory_policy(job.provider)
    if result.provider != job.provider or directory_url(str(result.source_url)) != directory_url(
        job.payload["official_url"]
    ):
        raise ValueError("catalog source does not match job")
    state = await catalog_state(session, job.provider)
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
    page_complete = result.complete if result.page_complete is None else result.page_complete
    pages[source] = {
        "status": "SUCCEEDED" if result.complete else "PARTIAL",
        "page_complete": page_complete,
        "coverage_complete": result.complete,
        "reported_total": result.reported_total,
        "parsed_hotels": len(result.hotels),
        "unparsed_hotel_links": result.unparsed_hotel_links,
        "directory_urls": [directory_url(str(url)) for url in result.directory_urls],
        "hotel_ids": [hotel.provider_hotel_id for hotel in result.hotels],
        "last_scan_at": now.timestamp(),
        "next_scan_at": (
            now + (timedelta(days=settings.discovery_interval_days) if page_complete else timedelta(hours=1))
        ).timestamp(),
        "last_error": error,
        "job_id": job.id,
    }
    state.value = {**data, "pages": pages, "urls": urls, "cursor": len(urls), "last_error": error}


async def record_catalog_failure(session, job, error, settings=None):
    _, directory_url = directory_policy(job.provider)
    state = await catalog_state(session, job.provider)
    data = dict(state.value)
    pages = {url: dict(info) for url, info in data.get("pages", {}).items()}
    # Invalid user-supplied URLs are not inserted into the automatic directory walk.
    try:
        url = directory_url(job.payload.get("official_url", ""))
    except Exception:
        return
    now = utcnow()
    root_redirect = job.provider == "ihg" and url != IHG_ROOT and error.code == ErrorCode.DIRECTORY_REDIRECT
    pages[url] = {
        **pages.get(url, {}),
        "status": "FAILED",
        "last_error": error.code.value,
        "next_scan_at": (
            now
            + (
                timedelta(days=settings.discovery_interval_days if settings else 14)
                if root_redirect
                else timedelta(hours=1)
            )
        ).timestamp(),
        "job_id": job.id,
    }
    if root_redirect:
        # Keep the failed regional page visible, not falsely complete/empty.
        # Normal source refresh can reconsider it, without hourly homepage reads.
        pages[url].update(redirected_to=IHG_ROOT, last_scan_at=now.timestamp())
    state.value = {**data, "pages": pages, "last_error": error.code.value}
