import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database.session import Session
from app.models.tables import CrawlJob, ProviderStatus, utcnow
from app.providers.marriott_cn_catalog import ROOT as MARRIOTT_CN_ROOT
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.alerts import reconcile_terminal_verifications
from app.services.calendar_hint_jobs import expand_calendar_hints
from app.services.calendar_hints import admit_calendar_candidates, hint_month_current
from app.services.catalog_price_source import load_catalog_price_source
from app.services.catalogs import discover_accor, discover_gha, discover_hilton
from app.services.ihg_catalogs import discover_hyatt, discover_marriott, obsolete_directory_job
from app.services.ihg_sitemaps import discover_ihg_global
from app.services.jobs import (
    cancel_outside_window,
    due_dispatch_jobs,
    enqueue,
    expire_pending_stays,
    reserve_provider_turn,
    within_monitoring_window,
)
from app.services.notifications import deliver_one
from app.services.provider_health import healthcheck_due, marriott_probe_hotel
from app.services.queue import Queue
from app.services.runtime_settings import RuntimeHttpClients, load_runtime_settings
from app.services.watchlists import expand_global, expand_watchlists

log = logging.getLogger(__name__)
FAIR_DISPATCH_PRIORITY = 99  # One head per group, below independently verified anomaly requests.
PROVIDER_TURN_PRIORITY = 99.5  # Persistent cross-group turn, still below priority-100 confirmations.


async def tick(sessions: async_sessionmaker, queue: Queue, settings: Settings) -> int:
    token = await queue.lock("hotelbug:scheduler", 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            settings = await load_runtime_settings(session, settings)
            now = utcnow()
            await session.execute(
                update(CrawlJob)
                .where(
                    CrawlJob.status == "RUNNING",
                    CrawlJob.lease_until < now,
                    CrawlJob.retry_count >= settings.max_retries,
                )
                .values(
                    status="FAILED",
                    error_type="TIMEOUT",
                    error_message="Worker lease expired repeatedly",
                    lease_owner=None,
                    lease_until=None,
                )
            )
            await session.execute(
                update(CrawlJob)
                .where(CrawlJob.status == "RUNNING", CrawlJob.lease_until < now)
                .values(
                    status="PENDING", retry_count=CrawlJob.retry_count + 1, lease_owner=None, lease_until=None
                )
            )
            # Retire invalid stays before capacity/paused-provider checks. A
            # deferred job can pass midnight while its retry is still future.
            await expire_pending_stays(session)
            # At-least-once dispatch: every due DB job is replayable after Redis loss.
            rows = await due_dispatch_jobs(session, now, FACTORIES)
            dispatched = 0
            eligible = []
            for job, effective_priority in rows:
                if await obsolete_directory_job(session, job):
                    job.status = "CANCELLED"
                    job.error_message = (
                        (job.error_message + "; ") if job.error_message else ""
                    ) + "Official directory source changed"
                    job.lease_owner, job.lease_until = None, None
                    continue
                if job.kind == JobKind.FETCH_CALENDAR_HINTS and not hint_month_current(job):
                    cancel_outside_window(job)
                    continue
                # Also guard due work beyond the bounded expiry batch. Never
                # overwrite its original failure with a disabled/pause reason.
                if job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
                    if not within_monitoring_window(job.check_in, job.check_out):
                        cancel_outside_window(job)
                        continue
                if not settings.provider_policy(job.provider).enabled:
                    job.status = "CANCELLED"
                    job.error_message = "Provider disabled by configuration"
                    continue
                state = await session.get(ProviderStatus, job.provider)
                if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                    job.scheduled_at = state.blocked_until
                    continue
                eligible.append((job, effective_priority))
            fair_head = await reserve_provider_turn(session, eligible)
            promoted_providers = set()
            for job, effective_priority in eligible:
                # SQL reservation alone is not enough: Redis otherwise leaves
                # low-priority groups behind a continuously replenished price
                # backlog. The first normal row is that group's pinned fair head.
                # Promote only its dispatch score, not its stored priority;
                # confirmations keep their higher lane and in-group order stays.
                if effective_priority < 100 and job.provider not in promoted_providers:
                    effective_priority = max(effective_priority, FAIR_DISPATCH_PRIORITY)
                    promoted_providers.add(job.provider)
                if job.id == fair_head:
                    effective_priority = max(effective_priority, PROVIDER_TURN_PRIORITY)
                await queue.put(job.id, effective_priority)
                job.status = "QUEUED"
                dispatched += 1
            # A failed/cancelled query is not an indefinitely pending candidate.
            # Bounded batches also repair failures written by an older Worker.
            await reconcile_terminal_verifications(session)
            await admit_calendar_candidates(session, settings)
            await expand_calendar_hints(session, settings)
            # Probe only idle/degraded groups; recent actual scans already prove
            # price-query health and must not trigger another fixed-hotel test.
            for provider in FACTORIES:
                if not settings.provider_policy(provider).enabled:
                    continue
                if await healthcheck_due(session, provider, now):
                    hotel = await marriott_probe_hotel(session) if provider == "marriott" else None
                    if provider == "marriott" and hotel is None:
                        continue
                    await enqueue(
                        session,
                        JobInput(
                            provider=provider,
                            kind=JobKind.PROVIDER_HEALTHCHECK,
                            hotel_id=hotel.id if hotel else None,
                            priority=10,
                            payload={"scheduled_probe": True},
                        ),
                    )
            await expand_watchlists(session, settings)
            source = await load_catalog_price_source(session)
            await expand_global(
                session,
                settings,
                monthly_providers=("marriott",) if source.root == MARRIOTT_CN_ROOT else (),
            )
            return dispatched
    finally:
        await queue.unlock("hotelbug:scheduler", token)


async def dispatch_loop(sessions, queue, settings) -> None:
    # Discovery has its own HTTP pool and loop: a slow sitemap must not delay
    # due stays, expired leases, verification jobs or Telegram delivery.
    clients = RuntimeHttpClients()
    try:
        while True:
            try:
                async with sessions() as session:
                    runtime = await load_runtime_settings(session, settings)
                await clients.update(runtime)
                await tick(sessions, queue, runtime)
                await deliver_one(sessions, runtime, clients.telegram)
            except Exception as exc:
                log.error("scheduler dispatch cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.scheduler_interval_seconds)
    finally:
        await clients.close()


async def catalog_loop(sessions, queue, settings) -> None:
    clients = RuntimeHttpClients()
    catalog_turn = 0
    try:
        while True:
            try:
                async with sessions() as session:
                    runtime = await load_runtime_settings(session, settings)
                await clients.update(runtime)
                catalogs = (
                    discover_accor,
                    discover_gha,
                    discover_hilton,
                    discover_ihg_global,
                    discover_marriott,
                    discover_hyatt,
                )
                discovery = catalogs[catalog_turn % len(catalogs)]
                catalog_turn += 1
                await discovery(sessions, queue, runtime, clients.catalog)
            except Exception as exc:
                log.error("scheduler catalog cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.scheduler_interval_seconds)
    finally:
        await clients.close()


async def heartbeat_loop(redis) -> None:
    while True:
        try:
            await redis.set("hotelbug:heartbeat:scheduler", utcnow().isoformat(), ex=60)
        except Exception as exc:
            log.error("scheduler heartbeat failed: %s", type(exc).__name__)
        await asyncio.sleep(15)


async def run_scheduler(sessions, queue, settings, redis) -> None:
    # TaskGroup cancels and joins all lanes before HTTP/Redis resources close.
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(dispatch_loop(sessions, queue, settings))
        tasks.create_task(catalog_loop(sessions, queue, settings))
        tasks.create_task(heartbeat_loop(redis))


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    queue = Queue(redis)
    try:
        await run_scheduler(Session, queue, settings, redis)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
