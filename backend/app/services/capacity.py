"""Bounded ordinary-job admission with a first slot for every enabled group."""

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.tables import AppSetting, CrawlJob
from app.schemas.domain import ProviderName
from app.services.jobs import LIVE

LOCK_KEY = "ordinary_job_capacity"


@dataclass
class JobCapacity:
    maximum: int
    counts: dict[str, int]
    enabled: set[str]

    def available_for(self, *providers: str) -> int:
        """Other empty enabled groups keep one slot, including directory startup."""
        reserved = sum(
            1 for provider in self.enabled.difference(providers) if not self.counts.get(provider, 0)
        )
        return max(0, self.maximum - sum(self.counts.values()) - reserved)

    def consume(self, provider: str) -> None:
        if self.available_for(provider) < 1:
            raise ValueError("No ordinary-job capacity for this provider")
        self.counts[provider] = self.counts.get(provider, 0) + 1


async def load_capacity(session, settings) -> JobCapacity:
    """Hold one DB row until commit so cooperating admission lanes serialize.

    Count every LIVE job, including paused, disabled and running groups. No
    existing job is cancelled or deferred. Thus an already-full legacy backlog
    still needs a normal completion/expiry before any new work can enter.
    Callers must reuse the returned budget within their transaction and consume
    one slot after each successful enqueue. Verification jobs are not changed.
    """
    query = select(AppSetting).where(AppSetting.key == LOCK_KEY).with_for_update()
    marker = await session.scalar(query)
    if marker is None:
        try:
            async with session.begin_nested():
                session.add(AppSetting(key=LOCK_KEY, value={}))
                await session.flush()
        except IntegrityError:
            await session.scalar(query)
    counts = dict(
        (
            await session.execute(
                select(CrawlJob.provider, func.count())
                .where(CrawlJob.status.in_(LIVE))
                .group_by(CrawlJob.provider)
            )
        ).all()
    )
    enabled = {name.value for name in ProviderName if settings.provider_policy(name).enabled}
    return JobCapacity(settings.max_pending_jobs, counts, enabled)
