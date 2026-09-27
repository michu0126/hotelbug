from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from test_domain import fixture_rate

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, PriceHistory, ProviderStatus, Rate, utcnow
from app.providers.base import HotelProvider
from app.providers.registry import FACTORIES
from app.scheduler.main import tick
from app.schemas.domain import HealthResult, HotelData, JobInput, JobKind
from app.services.jobs import enqueue
from app.services.rates import persist_rates, upsert_hotel


class FixtureProvider(HotelProvider):
    async def search_hotels(self, query):
        return []

    async def get_hotel_details(self, hotel_id):
        return HotelData(
            provider="marriott",
            provider_hotel_id=hotel_id,
            hotel_name="Fixture hotel",
            official_url="https://www.marriott.com/",
        )

    async def search_rates(self, request):
        return [fixture_rate()]

    async def get_calendar_rates(self, request):
        return await self.search_rates(request)

    async def health_check(self):
        return HealthResult(status="ONLINE", message="fixture only")


async def test_history_append_and_job_idempotency(sessions):
    async with sessions() as s, s.begin():
        hotel = await upsert_hotel(s, await FixtureProvider().get_hotel_details("fixture-only"))
        rate = fixture_rate()
        assert await persist_rates(s, hotel, [rate], "job1") == 1
        assert await persist_rates(s, hotel, [rate], "job1") == 0
        changed = rate.model_copy(
            update={
                "cash_price": Decimal("90.10"),
                "total_price": Decimal("100.11"),
                "captured_at": rate.captured_at + timedelta(seconds=1),
            }
        )
        assert await persist_rates(s, hotel, [changed], "job2") == 1
        assert await s.scalar(select(func.count()).select_from(PriceHistory)) == 2
        assert await s.scalar(select(Rate.total_price)) == Decimal("100.11")


async def test_scheduler_worker_and_redis_recovery(sessions, queue, monkeypatch):
    monkeypatch.setitem(FACTORIES, "marriott", FixtureProvider)
    settings = Settings()
    async with sessions() as s, s.begin():
        hotel = await upsert_hotel(s, await FixtureProvider().get_hotel_details("fixture-only"))
        data = JobInput(
            provider="marriott",
            kind=JobKind.FETCH_RATE,
            hotel_id=hotel.id,
            check_in="2027-01-07",
            check_out="2027-01-08",
            priority=99,
        )
        first = await enqueue(s, data)
        assert (await enqueue(s, data)).id == first.id
        job_id = first.id
    await tick(sessions, queue, settings)
    await queue.redis.flushdb()  # Redis is a rebuildable projection, not the source of truth.
    await tick(sessions, queue, settings)
    assert await process_one(sessions, queue, settings)
    async with sessions() as s:
        assert (await s.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert await s.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert (await s.get(ProviderStatus, "marriott")).status == "ONLINE"


async def test_expired_worker_lease_is_recovered(sessions, queue):
    async with sessions() as s, s.begin():
        row = await enqueue(s, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        row.status, row.lease_owner = "RUNNING", "dead-worker"
        row.lease_until = utcnow() - timedelta(seconds=1)
        job_id = row.id
    await tick(sessions, queue, Settings())
    assert await queue.pop() == job_id
    async with sessions() as s:
        row = await s.get(CrawlJob, job_id)
        assert row.status == "QUEUED" and row.lease_owner is None


async def test_blocked_provider_is_paused(sessions, queue, monkeypatch):
    class Blocked(FixtureProvider):
        async def health_check(self):
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Access denied")

    monkeypatch.setitem(FACTORIES, "marriott", Blocked)
    async with sessions() as s, s.begin():
        row = await enqueue(s, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = row.id
    await tick(sessions, queue, Settings())
    await process_one(sessions, queue, Settings())
    async with sessions() as s:
        assert (await s.get(CrawlJob, job_id)).status == "FAILED"
        assert (await s.get(ProviderStatus, "marriott")).status == "BLOCKED"


async def test_unimplemented_provider_does_not_fake_success(sessions, queue):
    async with sessions() as s, s.begin():
        row = await enqueue(s, JobInput(provider="accor", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = row.id
    await tick(sessions, queue, Settings())
    await process_one(sessions, queue, Settings())
    async with sessions() as s:
        row = await s.get(CrawlJob, job_id)
        assert row.status == "FAILED" and row.error_type == "NOT_IMPLEMENTED"
