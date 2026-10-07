"""Synthetic uneven worldwide catalogs; no official website requests."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, or_, select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.services.watchlists import expand_global


async def seed(db, provider, prefix, count):
    for index in range(count):
        db.add(
            Hotel(
                id=f"{prefix}-{index:03}",
                provider=provider,
                provider_hotel_id=f"TEST{index:04}",
                hotel_name="Offline hotel",
                official_url="https://example.com/",
            )
        )


def settings(budget=2):
    return Settings(
        global_jobs_per_tick=budget,
        provider_overrides={name: ProviderPolicy(enabled=True) for name in ("ihg", "gha", "accor")},
    )


async def test_large_healthy_catalog_does_not_take_small_groups_first_frontier_slots(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 50)
        await seed(db, "gha", "z-small", 2)
        assert await expand_global(db, settings()) == 2
        counts = dict(
            (await db.execute(select(CrawlJob.provider, func.count()).group_by(CrawlJob.provider))).all()
        )
        assert counts == {"ihg": 1, "gha": 1}


async def test_single_slot_rotation_survives_restart_and_visits_each_group(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 50)
        await seed(db, "gha", "z-small", 2)
        await seed(db, "accor", "y-small", 2)
    for _ in range(3):
        async with sessions() as db, db.begin():
            assert await expand_global(db, settings(1)) == 1
    async with sessions() as db:
        providers = (await db.scalars(select(CrawlJob.provider))).all()
        assert sorted(providers) == ["accor", "gha", "ihg"]


async def test_spare_capacity_is_used_without_reselecting_same_hotel_in_one_tick(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 10)
        await seed(db, "gha", "z-small", 1)
        assert await expand_global(db, settings(6)) == 6
        ids = (await db.scalars(select(CrawlJob.hotel_id))).all()
        assert len(set(ids)) == 6
        assert "z-small-000" in ids


async def test_provider_keysets_do_not_reset_existing_per_hotel_date_progress(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 4)
        await seed(db, "gha", "z-small", 2)
        db.add(AppSetting(key="global_hotel_cursor", value={"hotel_id": "a-large-001"}))
        db.add(AppSetting(key="global_date_cursor:a-large-002", value={"offset": 123}))
        assert await expand_global(db, settings()) == 2
        job = await db.scalar(select(CrawlJob).where(CrawlJob.provider == "ihg"))
        assert job.hotel_id == "a-large-002" and job.check_in == date.today() + timedelta(days=123)
    async with sessions() as db, db.begin():
        assert await expand_global(db, settings()) == 2
        ids = (await db.scalars(select(CrawlJob.hotel_id).where(CrawlJob.provider == "ihg"))).all()
        assert sorted(ids) == ["a-large-002", "a-large-003"]


async def test_paused_and_disabled_groups_keep_their_cursors(sessions):
    async with sessions() as db, db.begin():
        for provider, prefix in (("ihg", "a"), ("gha", "z"), ("accor", "y")):
            await seed(db, provider, prefix, 2)
        db.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=1)))
        db.add(AppSetting(key="global_hotel_cursor:ihg", value={"hotel_id": "a-000"}))
        db.add(AppSetting(key="global_hotel_cursor:accor", value={"hotel_id": "y-000"}))
        config = settings()
        config.provider_overrides["accor"].enabled = False
        assert await expand_global(db, config) == 2
        assert set((await db.scalars(select(CrawlJob.provider))).all()) == {"gha"}
        assert (await db.get(AppSetting, "global_hotel_cursor:ihg")).value == {"hotel_id": "a-000"}
        assert (await db.get(AppSetting, "global_hotel_cursor:accor")).value == {"hotel_id": "y-000"}


async def test_independent_keysets_cover_large_catalog_and_wrap_small_catalog(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 105)
        await seed(db, "gha", "z-small", 3)
    for _ in range(7):
        async with sessions() as db, db.begin():
            await expand_global(db, settings(20))
    async with sessions() as db:
        unique = dict(
            (
                await db.execute(
                    select(CrawlJob.provider, func.count(func.distinct(CrawlJob.hotel_id))).group_by(
                        CrawlJob.provider
                    )
                )
            ).all()
        )
        assert unique == {"ihg": 105, "gha": 3}
        assert (
            await db.scalar(
                select(CrawlJob.id).where(
                    or_(
                        CrawlJob.check_in < date.today(),
                        CrawlJob.check_out > date.today() + timedelta(days=365),
                    )
                )
            )
            is None
        )


@pytest.mark.parametrize("broken", [None, 123, ["ihg"]])
async def test_nonstring_legacy_and_group_cursor_fields_do_not_break_rotation(sessions, broken):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 2)
        await seed(db, "gha", "z-small", 2)
        db.add(AppSetting(key="global_provider_cursor", value={"provider": broken}))
        db.add(AppSetting(key="global_hotel_cursor", value={"hotel_id": broken}))
        db.add(AppSetting(key="global_hotel_cursor:ihg", value={"hotel_id": broken}))
        assert await expand_global(db, settings()) == 2
        assert set((await db.scalars(select(CrawlJob.provider))).all()) == {"ihg", "gha"}


async def test_full_capacity_preserves_group_and_date_cursors(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 2)
        await seed(db, "gha", "z-small", 2)
        config = settings()
        assert await expand_global(db, config) == 2
        snapshot = {s.key: s.value.copy() for s in (await db.scalars(select(AppSetting))).all()}
        config.max_pending_jobs = 2
        assert await expand_global(db, config) == 0
        after = {s.key: s.value for s in (await db.scalars(select(AppSetting))).all()}
        assert after == snapshot


async def test_inactive_and_empty_groups_do_not_steal_the_available_slot(sessions):
    async with sessions() as db, db.begin():
        await seed(db, "ihg", "a-large", 2)
        await seed(db, "gha", "z-small", 1)
        await db.flush()
        (await db.get(Hotel, "z-small-000")).active = False
        assert await expand_global(db, settings(1)) == 1
        assert (await db.scalar(select(CrawlJob))).provider == "ihg"
        assert await db.get(AppSetting, "global_hotel_cursor:gha") is None
        assert await db.get(AppSetting, "global_hotel_cursor:accor") is None
