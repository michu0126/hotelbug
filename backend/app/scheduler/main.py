import asyncio
import logging
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.database.session import Session
from app.models.tables import CrawlJob, ProviderStatus, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.queue import Queue

log = logging.getLogger(__name__)


async def tick(sessions: async_sessionmaker, queue: Queue, settings: Settings) -> int:
    token = await queue.lock("hotelbug:scheduler", 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
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
            # At-least-once dispatch: every due DB job is replayable after Redis loss.
            rows = (
                await session.scalars(
                    select(CrawlJob)
                    .where(CrawlJob.status.in_(("PENDING", "QUEUED")), CrawlJob.scheduled_at <= now)
                    .order_by(CrawlJob.priority.desc(), CrawlJob.scheduled_at)
                    .limit(100)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            dispatched = 0
            for job in rows:
                if not settings.provider_policy(job.provider).enabled:
                    job.status = "CANCELLED"
                    job.error_message = "Provider disabled by configuration"
                    continue
                state = await session.get(ProviderStatus, job.provider)
                if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                    job.scheduled_at = state.blocked_until
                    continue
                await queue.put(job.id, job.priority)
                job.status = "QUEUED"
                dispatched += 1
            # Only verified registered providers receive automatic health checks.
            for provider in FACTORIES:
                if not settings.provider_policy(provider).enabled:
                    continue
                recent = await session.scalar(
                    select(CrawlJob.id)
                    .where(
                        CrawlJob.provider == provider,
                        CrawlJob.kind == JobKind.PROVIDER_HEALTHCHECK,
                        CrawlJob.created_at > now - timedelta(hours=1),
                    )
                    .limit(1)
                )
                if not recent:
                    await enqueue(
                        session, JobInput(provider=provider, kind=JobKind.PROVIDER_HEALTHCHECK, priority=10)
                    )
            return dispatched
    finally:
        await queue.unlock("hotelbug:scheduler", token)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    queue = Queue(redis)
    try:
        while True:
            try:
                await tick(Session, queue, settings)
                await redis.set("hotelbug:heartbeat:scheduler", utcnow().isoformat(), ex=60)
            except Exception as exc:
                log.error("scheduler cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(settings.scheduler_interval_seconds)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
