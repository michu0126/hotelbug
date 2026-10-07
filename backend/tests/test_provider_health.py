"""Offline health scheduling; ordinary price polling is never suppressed."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_pipeline import FixtureProvider

from app.core.config import Settings
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, ProviderStatus, StayScan, utcnow
from app.providers.registry import FACTORIES
from app.scheduler.main import tick
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.provider_health import healthcheck_due, recent_healthy_scan
from app.services.rates import upsert_hotel


async def successful_scan(session, status="AVAILABLE"):
    hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
    day = date.today() + timedelta(days=20)
    job = await enqueue(
        session,
        JobInput(
            provider="marriott",
            kind=JobKind.FETCH_RATE,
            hotel_id=hotel.id,
            check_in=day,
            check_out=day + timedelta(days=1),
        ),
    )
    job.status = "SUCCEEDED"
    state = ProviderStatus(
        provider="marriott",
        status="ONLINE",
        requests_today=0,
        success_count=0,
        failure_count=0,
        average_latency=0,
    )
    session.add(state)
    scan = StayScan(
        hotel_id=hotel.id,
        check_in=day,
        check_out=day + timedelta(days=1),
        adults=2,
        rooms=1,
        status=status,
        observed_at=utcnow() - timedelta(minutes=10),
        job_id=job.id,
    )
    session.add(scan)
    await session.flush()
    return job, scan, state


@pytest.mark.parametrize("status", ["AVAILABLE", "UNAVAILABLE", "NOT_OPEN"])
async def test_genuine_recent_query_is_health_evidence_without_extra_probe(sessions, status):
    async with sessions() as session, session.begin():
        await successful_scan(session, status)
        assert await recent_healthy_scan(session, "marriott", utcnow())
        assert not await healthcheck_due(session, "marriott", utcnow())


@pytest.mark.parametrize(
    "condition",
    [
        "no_scan",
        "stale",
        "future",
        "failed_task",
        "directory_only",
        "wrong_dates",
        "orphan_job",
        "wrong_provider",
        "degraded",
        "broken",
    ],
)
async def test_flags_or_unverified_stale_data_do_not_replace_real_price_evidence(sessions, condition):
    async with sessions() as session, session.begin():
        job, scan, state = await successful_scan(session)
        if condition == "no_scan":
            await session.delete(scan)
        elif condition == "stale":
            scan.observed_at = utcnow() - timedelta(hours=2)
        elif condition == "future":
            scan.observed_at = utcnow() + timedelta(hours=2)
        elif condition == "failed_task":
            job.status = "FAILED"
        elif condition == "directory_only":
            job.kind = "DISCOVER_CATALOG"
        elif condition == "wrong_dates":
            job.check_out += timedelta(days=1)
        elif condition == "orphan_job":
            scan.job_id = "nonexistent"
        elif condition == "wrong_provider":
            job.provider = "gha"
        elif condition == "degraded":
            state.status = "DEGRADED"
        elif condition == "broken":
            state.status = "BROKEN"
        assert not await recent_healthy_scan(session, "marriott", utcnow())
        assert await healthcheck_due(session, "marriott", utcnow())


@pytest.mark.parametrize(
    "status,age,due",
    [
        ("PENDING", 4, False),
        ("QUEUED", 4, False),
        ("RUNNING", 4, False),
        ("FAILED", 0.5, False),
        ("SUCCEEDED", 0.5, False),
        ("FAILED", 4, True),
    ],
)
async def test_existing_live_or_recent_probe_prevents_hourly_duplicate(sessions, status, age, due):
    async with sessions() as session, session.begin():
        job = await enqueue(session, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        job.status = status
        job.created_at = utcnow() - timedelta(hours=age)
        assert await healthcheck_due(session, "marriott", utcnow()) is due


async def test_paused_group_gets_no_new_probe(sessions):
    async with sessions() as session, session.begin():
        session.add(
            ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=utcnow() + timedelta(hours=1))
        )
        assert not await healthcheck_due(session, "marriott", utcnow())


async def test_scheduler_uses_existing_success_and_tags_only_its_own_probe(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(global_monitoring_enabled=False)
    async with sessions() as session, session.begin():
        _, scan, _ = await successful_scan(session)
        scan_id = scan.id
    await tick(sessions, queue, settings)
    async with sessions() as session, session.begin():
        assert (
            await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.kind == "PROVIDER_HEALTHCHECK")
            )
            == 0
        )
        scan = await session.get(StayScan, scan_id)
        scan.observed_at = utcnow() - timedelta(hours=2)
    await tick(sessions, queue, settings)
    await tick(sessions, queue, settings)
    async with sessions() as session:
        checks = (
            await session.scalars(select(CrawlJob).where(CrawlJob.kind == "PROVIDER_HEALTHCHECK"))
        ).all()
        assert len(checks) == 1 and checks[0].payload == {"scheduled_probe": True}


@pytest.mark.parametrize("automatic", [True, False])
async def test_worker_skips_obsolete_auto_probe_but_preserves_explicit_check(
    sessions, queue, monkeypatch, automatic
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    calls = []

    class CountingHealth(FixtureProvider):
        async def health_check(self):
            calls.append("health")
            return await super().health_check()

    monkeypatch.setitem(FACTORIES, "marriott", CountingHealth)
    async with sessions() as session, session.begin():
        await successful_scan(session)
        job = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.PROVIDER_HEALTHCHECK,
                payload={"scheduled_probe": True} if automatic else {},
            ),
        )
        job_id = job.id
    await queue.put(job_id, 10)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as session:
        job = await session.get(CrawlJob, job_id)
        state = await session.get(ProviderStatus, "marriott")
        assert calls == ([] if automatic else ["health"])
        assert job.status == ("CANCELLED" if automatic else "SUCCEEDED")
        assert state.requests_today == (0 if automatic else 1)
        if automatic:
            assert job.error_message == "Recent successful rate scan; redundant scheduled health probe"


@pytest.mark.parametrize("condition", ["stale", "degraded"])
async def test_automatic_probe_still_runs_without_current_healthy_evidence(
    sessions, queue, monkeypatch, condition
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    monkeypatch.setitem(FACTORIES, "marriott", FixtureProvider)
    async with sessions() as session, session.begin():
        _, scan, state = await successful_scan(session)
        if condition == "stale":
            scan.observed_at = utcnow() - timedelta(hours=2)
        else:
            state.status = "DEGRADED"
        job = await enqueue(
            session,
            JobInput(
                provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK, payload={"scheduled_probe": True}
            ),
        )
        job_id = job.id
    await queue.put(job_id, 10)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as session:
        assert (await session.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert (await session.get(ProviderStatus, "marriott")).requests_today == 1
