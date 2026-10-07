import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database.session import Session
from app.models.tables import CrawlJob, ProviderStatus, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.alerts import reconcile_terminal_verifications
from app.services.catalogs import discover_accor, discover_gha, discover_hilton
from app.services.ihg_catalogs import discover_hyatt, discover_marriott
from app.services.ihg_sitemaps import discover_ihg_global
from app.services.jobs import (
    cancel_outside_window,
    due_dispatch_jobs,
    enqueue,
    expire_pending_stays,
    within_monitoring_window,
)
from app.services.notifications import deliver_one
from app.services.provider_health import healthcheck_due
from app.services.queue import Queue
from app.services.runtime_settings import RuntimeHttpClients, load_runtime_settings
from app.services.watchlists import expand_global, expand_watchlists

log = logging.getLogger(__name__)


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
            for job, effective_priority in rows:
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
                await queue.put(job.id, effective_priority)
                job.status = "QUEUED"
                dispatched += 1
            # A failed/cancelled query is not an indefinitely pending candidate.
            # Bounded batches also repair failures written by an older Worker.
            await reconcile_terminal_verifications(session)
            # Probe only idle/degraded groups; recent actual scans already prove
            # price-query health and must not trigger another fixed-hotel test.
            for provider in FACTORIES:
                if not settings.provider_policy(provider).enabled:
                    continue
                if await healthcheck_due(session, provider, now):
                    await enqueue(
                        session,
                        JobInput(
                            provider=provider,
                            kind=JobKind.PROVIDER_HEALTHCHECK,
                            priority=10,
                            payload={"scheduled_probe": True},
                        ),
                    )
            await expand_watchlists(session, settings)
            await expand_global(session, settings)
            return dispatched
    finally:
        await queue.unlock("hotelbug:scheduler", token)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    queue = Queue(redis)
    clients = RuntimeHttpClients()
    catalog_turn = 0
    try:
        while True:
            try:
                async with Session() as session:
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
                await discovery(Session, queue, runtime, clients.catalog)
                await tick(Session, queue, runtime)
                await deliver_one(Session, runtime, clients.telegram)
                await redis.set("hotelbug:heartbeat:scheduler", utcnow().isoformat(), ex=60)
            except Exception as exc:
                log.error("scheduler cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.scheduler_interval_seconds)
    finally:
        await clients.close()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
