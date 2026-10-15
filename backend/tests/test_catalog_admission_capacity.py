"""Official sitemap admission shares the bounded first-slot reservations."""

from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.schemas.domain import JobInput, JobKind, ProviderName
from app.services.catalogs import discover_accor, discover_gha, discover_hilton
from app.services.jobs import LIVE, enqueue

PROVIDERS = ("accor", "gha", "hilton")
DISCOVER = {"accor": discover_accor, "gha": discover_gha, "hilton": discover_hilton}


def config(*enabled, per_tick=20):
    return Settings(
        _env_file=None,
        max_pending_jobs=20,
        catalog_jobs_per_tick=per_tick,
        provider_overrides={name: ProviderPolicy(enabled=name in enabled) for name in ProviderName},
    )


def candidate(provider, index):
    if provider == "accor":
        return f"{index:04}"
    if provider == "gha":
        return f"https://www.ghadiscovery.com/offline-brand/hotel-{index}"
    return f"https://www.hilton.com/en/hotels/a{index:06}-offline-hotel"


def detail(provider, index, *, priority=70):
    value = candidate(provider, index)
    return JobInput(
        provider=provider,
        kind=JobKind.DISCOVER_HOTELS,
        priority=priority,
        payload={"provider_hotel_id" if provider == "accor" else "official_url": value},
    )


async def seed_source(db, provider, indexes):
    future = (utcnow() + timedelta(days=7)).timestamp()
    db.add(
        AppSetting(
            key="catalog:" + provider,
            value={
                "codes" if provider == "accor" else "urls": [candidate(provider, i) for i in indexes],
                "cursor": 0,
                "refresh_at": future,
                "next_fetch_at": future,
            },
        )
    )


def backlog(provider, count, status="PENDING"):
    return [
        CrawlJob(
            id=f"{provider}-backlog-{i}",
            provider=provider,
            kind=JobKind.DISCOVER_HOTELS,
            status=status,
            dedupe_key=f"{provider}-backlog-{i}",
            payload={"provider_hotel_id": f"offline-{i}"},
        )
        for i in range(count)
    ]


async def live_count(db):
    return await db.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE)))


async def discover_cached(sessions, queue, settings, provider):
    def unexpected_request(_request):
        pytest.fail("Cached admission unexpectedly requested a sitemap")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
        return await DISCOVER[provider](sessions, queue, settings, client)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_catalog_keeps_first_slot_for_every_other_enabled_group(sessions, queue, provider):
    settings = config(*ProviderName)
    async with sessions() as db, db.begin():
        await seed_source(db, provider, range(30))
    assert await discover_cached(sessions, queue, settings, provider) == 15
    assert await discover_cached(sessions, queue, settings, provider) == 0
    async with sessions() as db:
        assert await live_count(db) == 15
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 15
        assert set(await db.scalars(select(CrawlJob.provider))) == {provider}


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_full_disabled_running_backlog_stays_counted_and_cursor_does_not_advance(
    sessions, queue, provider
):
    settings = config(provider)
    pause_until = utcnow() + timedelta(hours=12)
    async with sessions() as db, db.begin():
        await seed_source(db, provider, range(3))
        db.add_all(backlog("ihg", 20, status="RUNNING"))
        db.add(ProviderStatus(provider="ihg", status="BLOCKED", blocked_until=pause_until))
    assert await discover_cached(sessions, queue, settings, provider) == 0
    async with sessions() as db, db.begin():
        assert await live_count(db) == 20
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 0
        assert set(await db.scalars(select(CrawlJob.status))) == {"RUNNING"}
        assert (await db.get(ProviderStatus, "ihg")).blocked_until.replace(tzinfo=pause_until.tzinfo) == (
            pause_until
        )
        (await db.get(CrawlJob, "ihg-backlog-0")).status = "SUCCEEDED"
    assert await discover_cached(sessions, queue, settings, provider) == 1
    async with sessions() as db:
        assert await live_count(db) == 20
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 1


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_resumed_live_identity_consumes_neither_capacity_nor_per_tick_budget(sessions, queue, provider):
    settings = config(provider, "ihg", "hyatt", per_tick=2)
    async with sessions() as db, db.begin():
        await seed_source(db, provider, [0, 1, 2, 3])
        db.add_all(backlog("ihg", 16))
        existing = await enqueue(db, detail(provider, 0, priority=3))
        existing.status = "QUEUED"
        existing_id = existing.id
    assert await discover_cached(sessions, queue, settings, provider) == 2
    async with sessions() as db:
        assert await live_count(db) == 19  # One first slot still belongs to Hyatt.
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 3
        saved = await db.get(CrawlJob, existing_id)
        assert saved.status == "QUEUED" and saved.priority == 3
        assert (
            await db.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.provider == provider))
            == 3
        )
    assert await discover_cached(sessions, queue, settings, provider) == 0


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_duplicate_source_entries_do_not_spend_two_slots(sessions, queue, provider):
    async with sessions() as db, db.begin():
        await seed_source(db, provider, [0, 0, 1])
    assert await discover_cached(sessions, queue, config(provider, per_tick=2), provider) == 2
    async with sessions() as db:
        assert await live_count(db) == 2
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 3


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_completed_identity_is_new_admission_not_a_live_dedupe(sessions, queue, provider):
    async with sessions() as db, db.begin():
        await seed_source(db, provider, [0, 1])
        db.add_all(backlog("ihg", 19))
        previous = await enqueue(db, detail(provider, 0))
        previous.status = "SUCCEEDED"
    assert await discover_cached(sessions, queue, config(provider), provider) == 1
    async with sessions() as db:
        assert await live_count(db) == 20
        assert (await db.get(AppSetting, "catalog:" + provider)).value["cursor"] == 1
        rows = (await db.scalars(select(CrawlJob).where(CrawlJob.provider == provider))).all()
        assert len(rows) == 2 and {row.status for row in rows} == {"SUCCEEDED", "PENDING"}
