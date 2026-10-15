"""Automatic probes follow the selected official source without extra network tests."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from test_pipeline import FixtureProvider

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan, utcnow
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.providers.registry import FACTORIES
from app.scheduler.main import tick
from app.schemas.domain import JobInput, JobKind, ProviderName
from app.services.jobs import enqueue
from app.services.provider_health import (
    healthcheck_due,
    marriott_probe_hotel,
    ordinary_price_work_due,
    recent_healthy_scan,
)

CN_URL = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=SINNV&isSearch=true&currency="
EN_URL = "https://www.marriott.com/en-us/hotels/travel/nycmq-new-york-marriott-marquis"


def settings():
    return Settings(
        _env_file=None,
        global_monitoring_enabled=False,
        provider_overrides={p.value: ProviderPolicy(enabled=p.value == "marriott") for p in ProviderName},
    )


def hotel(identity="cn-hotel", *, url=CN_URL, active=True):
    return Hotel(
        id=identity,
        provider="marriott",
        provider_hotel_id="SINNV" if url == CN_URL else "NYCMQ",
        hotel_name="Singapore Marriott Tang Plaza" if url == CN_URL else "New York Marriott Marquis",
        official_url=url,
        active=active,
    )


async def select_cn(db):
    db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT, "pages": {}}))


async def test_selected_source_waits_for_real_matching_active_hotel(sessions, queue):
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel("old-en", url=EN_URL))
        db.add(hotel("inactive-cn", active=False))
        assert await marriott_probe_hotel(db) is None
        assert await marriott_probe_hotel(db, "missing") is None
        assert not await healthcheck_due(db, "marriott", utcnow())
    await tick(sessions, queue, settings())
    async with sessions() as db, db.begin():
        assert (await db.scalars(select(CrawlJob))).all() == []
        (await db.get(Hotel, "inactive-cn")).active = True
    await tick(sessions, queue, settings())
    async with sessions() as db:
        probes = (await db.scalars(select(CrawlJob))).all()
        assert len(probes) == 1
        assert probes[0].hotel_id == "inactive-cn"
        assert probes[0].payload == {"scheduled_probe": True}


@pytest.mark.parametrize("kind", [JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY])
@pytest.mark.parametrize("status", ["PENDING", "QUEUED", "RUNNING"])
async def test_due_price_work_suppresses_automatic_probe(sessions, queue, kind, status):
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel())
        await db.flush()
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=kind,
                hotel_id="cn-hotel",
                check_in=day,
                check_out=day + timedelta(days=1),
            ),
        )
        job.status = status
        assert await ordinary_price_work_due(db, "marriott", utcnow())
        assert not await healthcheck_due(db, "marriott", utcnow())
    await tick(sessions, queue, settings())
    async with sessions() as db:
        assert (
            await db.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.PROVIDER_HEALTHCHECK))
        ).all() == []


@pytest.mark.parametrize(
    "condition", ["future", "past_stay", "failed", "inactive", "old_source", "wrong_provider"]
)
async def test_ineligible_price_work_does_not_hide_a_needed_probe(sessions, condition):
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel())
        item = hotel("other-hotel", url=EN_URL if condition == "old_source" else CN_URL)
        item.provider_hotel_id = "OTHER"
        item.active = condition != "inactive"
        db.add(item)
        await db.flush()
        job = CrawlJob(
            provider="gha" if condition == "wrong_provider" else "marriott",
            hotel_id=item.id,
            kind=JobKind.FETCH_RATE,
            status="FAILED" if condition == "failed" else "PENDING",
            check_in=day - timedelta(days=30) if condition == "past_stay" else day,
            check_out=day - timedelta(days=29) if condition == "past_stay" else day + timedelta(days=1),
            scheduled_at=utcnow() + timedelta(hours=1) if condition == "future" else utcnow(),
            dedupe_key="other-stay",
        )
        db.add(job)
        assert not await ordinary_price_work_due(db, "marriott", utcnow())
        assert await healthcheck_due(db, "marriott", utcnow())


@pytest.mark.parametrize("automatic", [True, False])
async def test_worker_rechecks_price_work_without_canceling_manual_checks(
    sessions, queue, monkeypatch, automatic
):
    calls = []

    class CountingHealth(FixtureProvider):
        async def health_check(self):
            calls.append("health")
            return await super().health_check()

    monkeypatch.setitem(FACTORIES, "marriott", CountingHealth)
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel())
        await db.flush()
        await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id="cn-hotel",
                check_in=day,
                check_out=day + timedelta(days=1),
            ),
        )
        probe = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.PROVIDER_HEALTHCHECK,
                hotel_id="cn-hotel",
                payload={"scheduled_probe": True} if automatic else {},
            ),
        )
        probe_id = probe.id
    await queue.put(probe_id, 10)
    await process_one(sessions, queue, settings())
    async with sessions() as db:
        probe = await db.get(CrawlJob, probe_id)
        assert calls == ([] if automatic else ["health"])
        assert probe.status == ("CANCELLED" if automatic else "SUCCEEDED")
        if automatic:
            assert "Eligible ordinary price work" in probe.error_message
            assert await db.get(ProviderStatus, "marriott") is None


@pytest.mark.parametrize("hotel_id", [None, "old-en", "inactive-cn"])
async def test_worker_cancels_unbound_or_previous_source_auto_probe(sessions, queue, monkeypatch, hotel_id):
    calls = []

    class CountingHealth(FixtureProvider):
        async def health_check(self):
            calls.append("health")
            return await super().health_check()

    monkeypatch.setitem(FACTORIES, "marriott", CountingHealth)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel("old-en", url=EN_URL))
        db.add(hotel("inactive-cn", active=False))
        await db.flush()
        probe = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.PROVIDER_HEALTHCHECK,
                hotel_id=hotel_id,
                payload={"scheduled_probe": True},
            ),
        )
        probe_id = probe.id
    await queue.put(probe_id, 10)
    await process_one(sessions, queue, settings())
    async with sessions() as db:
        probe = await db.get(CrawlJob, probe_id)
        assert probe.status == "CANCELLED"
        assert "selected directory source" in probe.error_message
        assert not calls
        assert await db.get(ProviderStatus, "marriott") is None


async def test_existing_pause_is_preserved_before_source_probe_cancellation(sessions, queue):
    deadline = utcnow() + timedelta(hours=5)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(
            ProviderStatus(
                provider="marriott", status="BLOCKED", blocked_until=deadline, last_error="BLOCKED_BY_ANTIBOT"
            )
        )
        probe = await enqueue(
            db,
            JobInput(
                provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK, payload={"scheduled_probe": True}
            ),
        )
        probe_id = probe.id
    await queue.put(probe_id, 10)
    await process_one(sessions, queue, settings())
    async with sessions() as db:
        probe = await db.get(CrawlJob, probe_id)
        state = await db.get(ProviderStatus, "marriott")
        assert probe.status == "PENDING"
        assert probe.scheduled_at.replace(tzinfo=deadline.tzinfo) == deadline
        assert state.blocked_until.replace(tzinfo=deadline.tzinfo) == deadline
        assert state.last_error == "BLOCKED_BY_ANTIBOT"


@pytest.mark.parametrize("source", ["cn-hotel", "en-hotel"])
async def test_only_current_source_success_is_marriott_health_evidence(sessions, source):
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel())
        db.add(hotel("en-hotel", url=EN_URL))
        db.add(ProviderStatus(provider="marriott", status="ONLINE"))
        await db.flush()
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=source,
                check_in=day,
                check_out=day + timedelta(days=1),
            ),
        )
        job.status = "SUCCEEDED"
        db.add(
            StayScan(
                hotel_id=source,
                check_in=day,
                check_out=day + timedelta(days=1),
                adults=2,
                rooms=1,
                status="AVAILABLE",
                observed_at=utcnow(),
                job_id=job.id,
            )
        )
        assert await recent_healthy_scan(db, "marriott", utcnow()) is (source == "cn-hotel")
        assert await healthcheck_due(db, "marriott", utcnow()) is (source != "cn-hotel")


async def test_actual_configured_auto_probe_denial_keeps_group_pause_without_other_host(
    sessions, queue, monkeypatch
):
    calls = []

    class RejectedCNProbe(MarriottBrowserProvider):
        async def search_rates(self, request):
            calls.append((self._root, request.provider_hotel_id, self._hotel_name))
            raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Explicit CN rejection")

    monkeypatch.setitem(FACTORIES, "marriott", RejectedCNProbe)
    async with sessions() as db, db.begin():
        await select_cn(db)
        db.add(hotel())
        await db.flush()
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.PROVIDER_HEALTHCHECK,
                hotel_id="cn-hotel",
                payload={"scheduled_probe": True},
            ),
        )
        job_id = job.id
    started = utcnow()
    await queue.put(job_id, 10)
    await process_one(sessions, queue, settings())
    async with sessions() as db:
        job = await db.get(CrawlJob, job_id)
        state = await db.get(ProviderStatus, "marriott")
        assert calls == [("https://www.marriott.com.cn", "SINNV", "Singapore Marriott Tang Plaza")]
        assert job.status == "FAILED"
        assert state.status == "BLOCKED"
        assert state.blocked_until.replace(tzinfo=started.tzinfo) >= started + timedelta(hours=6)
        assert job.scheduled_at == state.blocked_until
        assert state.last_error == "BLOCKED_BY_ANTIBOT"


@pytest.mark.parametrize("fails", [False, True])
async def test_configured_marriott_health_uses_exact_hotel_code_and_source(monkeypatch, fails):
    provider = MarriottBrowserProvider(hotel_name="Singapore Marriott Tang Plaza")
    provider.configure_hotel_url(CN_URL)
    search = AsyncMock(return_value=[object()])
    if fails:
        search.side_effect = ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "Explicit rejection")
    monkeypatch.setattr(provider, "search_rates", search)
    if fails:
        with pytest.raises(ProviderError):
            await provider.health_check()
    else:
        assert (await provider.health_check()).status == "ONLINE"
    request = search.await_args.args[0]
    assert request.provider_hotel_id == "SINNV"
    assert request.check_in == date.today() + timedelta(days=10)
    assert provider._root == "https://www.marriott.com.cn"
    assert provider._official_code == "SINNV"
    assert provider._hotel_name == "Singapore Marriott Tang Plaza"


async def test_explicit_unconfigured_marriott_check_retains_manual_default(monkeypatch):
    provider = MarriottBrowserProvider(hotel_name="Original label")
    search = AsyncMock(return_value=[object()])
    monkeypatch.setattr(provider, "search_rates", search)
    await provider.health_check()
    assert search.await_args.args[0].provider_hotel_id == "NYCMQ"
    assert provider._hotel_name == "Original label"
    assert provider._root == "https://www.marriott.com"
