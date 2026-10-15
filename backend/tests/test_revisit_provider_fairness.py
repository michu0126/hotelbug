"""A large group's calendar history must not monopolize global revisit slots."""

import pytest
from sqlalchemy import select
from test_failed_stays import failure
from test_rechecks import scan

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.services.failed_stays import expand_failed_stays
from app.services.rechecks import expand_successful_rechecks


@pytest.mark.parametrize("lane", ["observed", "failed"])
@pytest.mark.parametrize("budget", [1, 2])
async def test_large_old_group_cannot_hide_another_groups_due_stays(sessions, lane, budget):
    create = scan if lane == "observed" else failure
    expand = expand_successful_rechecks if lane == "observed" else expand_failed_stays
    async with sessions() as db, db.begin():
        for index in range(20):
            await create(db, suffix=f"gha-{index:03}", provider="gha", age=20)
        await create(db, suffix="ihg-one", provider="ihg", age=4)
    # New SQL sessions model a Scheduler restart; one free slot must rotate
    # across groups rather than traverse every GHA date before looking at IHG.
    for _ in range(2 // budget):
        async with sessions() as db, db.begin():
            assert await expand(db, Settings(), ["gha", "ihg"], budget) == budget
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 2
        assert {job.provider for job in jobs} == {"gha", "ihg"}


@pytest.mark.parametrize("lane", ["observed", "failed"])
async def test_one_slot_visits_all_six_groups_across_scheduler_restarts(sessions, lane):
    create = scan if lane == "observed" else failure
    expand = expand_successful_rechecks if lane == "observed" else expand_failed_stays
    providers = ["marriott", "ihg", "gha", "accor", "hilton", "hyatt"]
    async with sessions() as db, db.begin():
        for provider in providers:
            for index in range(8):
                await create(db, suffix=f"{provider}-{index}", provider=provider, age=20)
    for _ in providers:
        async with sessions() as db, db.begin():
            assert await expand(db, Settings(), providers, 1) == 1
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 6
        assert {job.provider for job in jobs} == set(providers)


@pytest.mark.parametrize("lane", ["observed", "failed"])
async def test_prefetched_but_unconsumed_dates_do_not_move_group_cursor(sessions, lane):
    create = scan if lane == "observed" else failure
    expand = expand_successful_rechecks if lane == "observed" else expand_failed_stays
    key = "global_recheck_cursor" if lane == "observed" else "global_failed_stay_cursor"
    async with sessions() as db, db.begin():
        await create(db, suffix="gha", provider="gha")
        await create(db, suffix="ihg", provider="ihg")
        assert await expand(db, Settings(), ["gha", "ihg"], 1) == 1
        assert await db.get(AppSetting, key + ":gha")
        assert await db.get(AppSetting, key + ":ihg") is None
    async with sessions() as db, db.begin():
        assert await expand(db, Settings(), ["gha", "ihg"], 1) == 1
        assert (await db.get(AppSetting, key + ":provider")).value == {"provider": "ihg"}


@pytest.mark.parametrize("lane", ["observed", "failed"])
async def test_paused_and_disabled_groups_cannot_take_revisit_turns(sessions, lane):
    from datetime import timedelta

    create = scan if lane == "observed" else failure
    expand = expand_successful_rechecks if lane == "observed" else expand_failed_stays
    async with sessions() as db, db.begin():
        for provider in ("accor", "gha", "ihg"):
            await create(db, suffix=provider, provider=provider)
        db.add(ProviderStatus(provider="accor", blocked_until=utcnow() + timedelta(hours=1)))
        assert await expand(db, Settings(), ["accor", "ihg"], 1) == 1
        jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 1 and jobs[0].provider == "ihg"


@pytest.mark.parametrize("lane", ["observed", "failed"])
@pytest.mark.parametrize("malformed", [True, False])
async def test_legacy_cursor_wraps_and_damaged_values_do_not_stop_groups(sessions, lane, malformed):
    create = scan if lane == "observed" else failure
    expand = expand_successful_rechecks if lane == "observed" else expand_failed_stays
    key = "global_recheck_cursor" if lane == "observed" else "global_failed_stay_cursor"
    time_field = "observed_at" if lane == "observed" else "created_at"
    id_field = "scan_id" if lane == "observed" else "job_id"
    async with sessions() as db, db.begin():
        for provider in ("gha", "ihg"):
            await create(db, suffix=provider, provider=provider)
        value = (
            {time_field: "not-a-date", id_field: [], "provider": []}
            if malformed
            else {time_field: utcnow().isoformat(), id_field: "past-all-eligible-rows"}
        )
        db.add(AppSetting(key=key, value=value))
        db.add(AppSetting(key=key + ":provider", value={"provider": []}))
        assert await expand(db, Settings(), ["gha", "ihg"], 2) == 2
        for provider in ("gha", "ihg"):
            cursor = await db.get(AppSetting, key + ":" + provider)
            assert isinstance(cursor.value[id_field], str)
