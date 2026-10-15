"""A growing group leaves bounded admission slots for other enabled groups."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan, Watchlist, utcnow
from app.schemas.domain import ProviderName
from app.services.failed_stays import expand_failed_stays
from app.services.ihg_catalogs import directory_policy, discover_browser_directory
from app.services.jobs import LIVE
from app.services.rechecks import expand_rechecks
from app.services.watchlists import expand_global, expand_watchlists


def config(*enabled):
    return Settings(
        max_pending_jobs=20,
        global_jobs_per_tick=20,
        provider_overrides={name: ProviderPolicy(enabled=name in enabled) for name in ProviderName},
    )


def hotel(provider, index=0):
    return Hotel(
        id=f"{provider}-hotel-{index}",
        provider=provider,
        provider_hotel_id=str(index),
        hotel_name=f"Offline {provider} {index}",
        official_url="https://www.ihg.com/",
    )


def backlog(count, *, provider="ihg", status="PENDING"):
    return [
        CrawlJob(
            id=f"{provider}-backlog-{index}",
            provider=provider,
            kind="DISCOVER_HOTELS",
            status=status,
            dedupe_key=f"{provider}-backlog-{index}",
            payload={"provider_hotel_id": f"pending-{index}"},
        )
        for index in range(count)
    ]


async def live_count(db):
    return await db.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE)))


@pytest.mark.parametrize("lane", ["global", "watchlist", "recheck", "failed"])
async def test_every_nightly_generation_lane_preserves_other_groups_first_slot(sessions, lane):
    settings = config("ihg", "gha")
    stay = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        db.add_all(backlog(18))
        for index in range(2):
            item = hotel("ihg", index)
            db.add(item)
            await db.flush()
            if lane == "watchlist":
                db.add(
                    Watchlist(
                        id=f"watch-{index}", name="Offline", scope="hotel", filters={"hotel_id": item.id}
                    )
                )
            elif lane == "recheck":
                db.add(
                    StayScan(
                        hotel_id=item.id,
                        check_in=stay,
                        check_out=stay + timedelta(days=1),
                        adults=2,
                        rooms=1,
                        status="AVAILABLE",
                        observed_at=utcnow() - timedelta(hours=4),
                        job_id="offline-history",
                    )
                )
            elif lane == "failed":
                db.add(
                    CrawlJob(
                        provider="ihg",
                        hotel_id=item.id,
                        kind="FETCH_RATE",
                        status="FAILED",
                        check_in=stay,
                        check_out=stay + timedelta(days=1),
                        error_type="TIMEOUT",
                        created_at=utcnow() - timedelta(hours=4),
                        dedupe_key=f"old-failure-{index}",
                    )
                )
        if lane == "global":
            created = await expand_global(db, settings)
        elif lane == "watchlist":
            created = await expand_watchlists(db, settings)
        elif lane == "recheck":
            created = await expand_rechecks(db, settings, ["ihg"], 10)
        else:
            created = await expand_failed_stays(db, settings, ["ihg"], 10)
        assert created == 1
        assert await live_count(db) == 19
    # A later pause must not strand the untouched slot. A new transaction
    # reconstructs reservations from actual jobs, not a process-local counter.
    until = utcnow() + timedelta(hours=12)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="ihg", blocked_until=until))
        db.add(hotel("gha"))
        assert await expand_global(db, settings) == 1
        assert await live_count(db) == 20
        assert (await db.get(ProviderStatus, "ihg")).blocked_until.replace(tzinfo=until.tzinfo) == until
        assert (await db.get(CrawlJob, "ihg-backlog-0")).status == "PENDING"
        assert await expand_global(db, settings) == 0


async def test_all_six_groups_have_a_first_slot_without_increasing_global_ceiling(sessions):
    providers = list(ProviderName)
    settings = config(*providers)
    async with sessions() as db, db.begin():
        db.add_all([hotel("ihg", index) for index in range(20)])
        assert await expand_global(db, settings) == 15
        assert await live_count(db) == 15
    for provider in providers:
        if provider == "ihg":
            continue
        async with sessions() as db, db.begin():
            db.add(hotel(provider))
            await expand_global(db, settings)
            assert await live_count(db) <= 20
    async with sessions() as db, db.begin():
        assert await live_count(db) == 20
        actual = set(await db.scalars(select(CrawlJob.provider).where(CrawlJob.status.in_(LIVE)).distinct()))
        assert actual == set(providers)
        assert await expand_global(db, settings) == 0


async def test_existing_full_paused_backlog_is_preserved_without_overcommitting(sessions):
    settings = config("ihg", "gha")
    until = utcnow() + timedelta(hours=12)
    async with sessions() as db, db.begin():
        db.add_all(backlog(20))
        db.add(ProviderStatus(provider="ihg", blocked_until=until))
        db.add(hotel("gha"))
        assert await expand_global(db, settings) == 0
        assert await live_count(db) == 20
        assert set(await db.scalars(select(CrawlJob.status))) == {"PENDING"}
        # Reclaim only normal completion capacity, without cancelling old work.
        (await db.get(CrawlJob, "ihg-backlog-0")).status = "SUCCEEDED"
        assert await expand_global(db, settings) == 1
        assert await live_count(db) == 20


async def test_disabled_empty_group_does_not_hold_a_reservation(sessions):
    async with sessions() as db, db.begin():
        db.add_all(backlog(19))
        db.add(hotel("ihg"))
        assert await expand_global(db, config("ihg", "gha")) == 0
        assert await expand_global(db, config("ihg")) == 1
        assert await live_count(db) == 20


async def test_denied_watch_admission_does_not_advance_its_date_cursor(sessions):
    async with sessions() as db, db.begin():
        db.add_all(backlog(19))
        item = hotel("ihg")
        db.add(item)
        db.add(Watchlist(id="watch", name="Offline", scope="hotel", filters={"hotel_id": item.id}))
        db.add(AppSetting(key="watch_cursor:watch", value={"offset": 50}))
        assert await expand_watchlists(db, config("ihg", "gha")) == 0
        assert (await db.get(AppSetting, "watch_cursor:watch")).value == {"offset": 50}


async def test_disabled_or_running_jobs_still_count_toward_global_ceiling(sessions):
    async with sessions() as db, db.begin():
        db.add_all(backlog(20, provider="accor", status="RUNNING"))
        db.add(hotel("gha"))
        assert await expand_global(db, config("gha")) == 0
        assert await live_count(db) == 20


@pytest.mark.parametrize("provider", ["ihg", "marriott", "hyatt"])
async def test_browser_directory_admission_leaves_other_group_a_first_slot(sessions, queue, provider):
    settings = config(provider, "gha").model_copy(update={"catalog_jobs_per_tick": 20})
    root, _ = directory_policy(provider)
    async with sessions() as db, db.begin():
        db.add_all(backlog(18, provider=provider))
        # The root is new, but the provider already has a large live backlog.
        db.add(AppSetting(key="catalog:" + provider, value={"pages": {root: {"next_scan_at": 0}}}))
    assert await discover_browser_directory(sessions, queue, settings, provider) == 1
    async with sessions() as db, db.begin():
        assert await live_count(db) == 19
        state = await db.get(AppSetting, "catalog:" + provider)
        assert state.value["pages"][root]["status"] == "QUEUED"
        db.add(hotel("gha"))
        assert await expand_global(db, settings) == 1
        assert await live_count(db) == 20
    assert await discover_browser_directory(sessions, queue, settings, provider) == 0


@pytest.mark.parametrize("provider", ["ihg", "marriott", "hyatt"])
async def test_full_budget_does_not_mark_unadmitted_directory_as_queued(sessions, queue, provider):
    settings = config(provider, "gha")
    root, _ = directory_policy(provider)
    async with sessions() as db, db.begin():
        db.add_all(backlog(19, provider=provider))
        db.add(AppSetting(key="catalog:" + provider, value={"pages": {root: {"next_scan_at": 0}}}))
    assert await discover_browser_directory(sessions, queue, settings, provider) == 0
    async with sessions() as db:
        assert (await db.get(AppSetting, "catalog:" + provider)).value["pages"][root] == {"next_scan_at": 0}
        assert await live_count(db) == 19
