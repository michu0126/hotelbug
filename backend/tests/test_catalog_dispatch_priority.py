"""Automatic global discovery must not outrank monitored public-price queries."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, utcnow
from app.scheduler.main import tick
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services.catalogs import discover_accor, discover_gha, discover_hilton
from app.services.ihg_catalogs import discover_hyatt, discover_ihg, discover_marriott
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel

DISCOVER = {
    "accor": discover_accor,
    "gha": discover_gha,
    "hilton": discover_hilton,
    "ihg": discover_ihg,
    "marriott": discover_marriott,
    "hyatt": discover_hyatt,
}


class NoRequests:
    async def get(self, *args, **kwargs):
        raise AssertionError("A persisted catalog must not make a network request in this test")


async def setup_jobs(sessions, queue, name, count=1):
    policies = {provider: ProviderPolicy(enabled=provider == name) for provider in DISCOVER}
    settings = Settings(provider_overrides=policies, catalog_jobs_per_tick=20)
    async with sessions() as db, db.begin():
        if name in {"accor", "gha", "hilton"}:
            source = (
                {"codes": [f"{i:04d}" for i in range(count)]}
                if name == "accor"
                else {
                    "urls": [f"https://www.ghadiscovery.com/offline/hotel-{i}" for i in range(count)]
                    if name == "gha"
                    else [f"https://www.hilton.com/en/hotels/{i:07d}-offline" for i in range(count)]
                }
            )
            db.add(
                AppSetting(
                    key="catalog:" + name,
                    value={**source, "cursor": 0, "refresh_at": 9999999999, "next_fetch_at": 9999999999},
                )
            )
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider=name,
                provider_hotel_id="OFFLINE",
                hotel_name="Explicit offline hotel",
                official_url="https://example.com/offline",
            ),
        )
        hotel_id = hotel.id
    remaining = count
    while remaining:
        created = await DISCOVER[name](sessions, queue, settings, NoRequests())
        assert created == min(20, remaining)
        remaining -= created
    async with sessions() as db, db.begin():
        catalog = await db.scalar(
            select(CrawlJob).where(CrawlJob.kind.in_((JobKind.DISCOVER_HOTELS, JobKind.DISCOVER_CATALOG)))
        )
        base = {"provider": name, "hotel_id": hotel_id}
        watch = await enqueue(
            db,
            JobInput(
                **base,
                kind=JobKind.FETCH_RATE,
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                priority=85,
            ),
        )
        hot = await enqueue(
            db,
            JobInput(
                **base,
                kind=JobKind.FETCH_RATE,
                check_in=date.today() + timedelta(days=1),
                check_out=date.today() + timedelta(days=2),
                priority=80,
            ),
        )
        verify = await enqueue(
            db,
            JobInput(
                **base,
                kind=JobKind.VERIFY_ANOMALY,
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                priority=100,
            ),
        )
        ids = {"catalog": catalog.id, "watch": watch.id, "hot": hot.id, "verify": verify.id}
    settings.global_monitoring_enabled = False
    return settings, ids


@pytest.mark.parametrize("provider", DISCOVER)
async def test_fresh_catalog_follows_confirmation_watch_and_near_stay(sessions, queue, provider):
    settings, ids = await setup_jobs(sessions, queue, provider)
    assert await tick(sessions, queue, settings) == 4
    assert [await queue.pop() for _ in range(4)] == [ids["verify"], ids["watch"], ids["hot"], ids["catalog"]]


@pytest.mark.parametrize("provider", DISCOVER)
async def test_aged_catalog_still_progresses_and_redis_replay_preserves_order(sessions, queue, provider):
    settings, ids = await setup_jobs(sessions, queue, provider)
    async with sessions() as db, db.begin():
        (await db.get(CrawlJob, ids["catalog"])).scheduled_at = utcnow() - timedelta(hours=49)
    for _ in range(2):
        await queue.redis.flushdb()
        assert await tick(sessions, queue, settings) >= 4
        assert [await queue.pop() for _ in range(4)] == [
            ids["verify"],
            ids["catalog"],
            ids["watch"],
            ids["hot"],
        ]
    async with sessions() as db:
        assert (await db.get(CrawlJob, ids["catalog"])).priority == 70


@pytest.mark.parametrize("provider", ["accor", "gha", "hilton"])
async def test_bulk_discovery_cannot_hide_watched_prices_beyond_dispatch_page(sessions, queue, provider):
    settings, ids = await setup_jobs(sessions, queue, provider, count=110)
    assert await tick(sessions, queue, settings) == 100
    ready = [await queue.pop() for _ in range(100)]
    assert ready[:3] == [ids["verify"], ids["watch"], ids["hot"]]
    assert len(set(ready)) == 100 and await queue.pop() is None
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob))).all()
        catalogs = [job for job in jobs if job.kind == JobKind.DISCOVER_HOTELS]
        assert len(catalogs) == 110
        assert sum(job.status == "QUEUED" for job in catalogs) == 97
        assert sum(job.status == "PENDING" for job in catalogs) == 13
