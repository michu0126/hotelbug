"""Global browser directories need fair unseen/repair lanes, not insertion order."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, utcnow
from app.services.ihg_catalogs import directory_policy, discover_browser_directory


async def seed(sessions, provider, *, new_count=1, old_count=3):
    root, _ = directory_policy(provider)
    origin = "https://www.ihg.com/" if provider == "ihg" else "https://www.marriott.com/en-us/destinations/"
    suffix = "" if provider == "ihg" else ".mi"
    old = [origin + f"offline-old-{i}" + suffix for i in range(old_count)]
    new = [origin + f"offline-new-{i}" + suffix for i in range(new_count)]
    now = utcnow()
    pages = {
        root: {
            "status": "SUCCEEDED",
            "directory_urls": old + new,
            "hotel_ids": [],
            "last_scan_at": now.timestamp(),
            "next_scan_at": (now + timedelta(days=14)).timestamp(),
        }
    }
    pages.update(
        {
            url: {
                "status": "PARTIAL",
                "directory_urls": [],
                "hotel_ids": [],
                "last_scan_at": (now - timedelta(hours=2)).timestamp(),
                "next_scan_at": 0,
            }
            for url in old
        }
    )
    pages.update({url: {"next_scan_at": 0} for url in new})
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:" + provider, value={"pages": pages, "urls": []}))
    return old, new


def settings(provider, budget=1):
    return Settings(provider_overrides={provider: ProviderPolicy(enabled=True)}, catalog_jobs_per_tick=budget)


@pytest.mark.parametrize("provider", ["ihg", "marriott"])
async def test_new_directories_get_next_turn_despite_large_due_repair_prefix(sessions, queue, provider):
    old, new = await seed(sessions, provider, old_count=30)
    assert await discover_browser_directory(sessions, queue, settings(provider), provider) == 1
    async with sessions() as db, db.begin():
        first = await db.scalar(select(CrawlJob))
        assert first.payload["official_url"] == old[0]
        first.status = "SUCCEEDED"
        state = await db.get(AppSetting, "catalog:" + provider)
        info = state.value["pages"][old[0]]
        # Simulated successful Worker commit; no network or live clock reset.
        state.value = {
            **state.value,
            "pages": {
                **state.value["pages"],
                old[0]: {
                    **info,
                    "status": "PARTIAL",
                    "next_scan_at": (utcnow() + timedelta(hours=1)).timestamp(),
                },
            },
        }
    await queue.redis.flushdb()  # Dispatch-lane progress must be in the DB, not Redis.
    assert await discover_browser_directory(sessions, queue, settings(provider), provider) == 1
    async with sessions() as db:
        second = await db.scalar(select(CrawlJob).where(CrawlJob.id != first.id))
        assert second.payload["official_url"] == new[0]


@pytest.mark.parametrize("provider", ["ihg", "marriott"])
async def test_repair_lane_uses_oldest_due_page_not_oldest_dictionary_key(sessions, queue, provider):
    old, _ = await seed(sessions, provider, new_count=0, old_count=2)
    async with sessions() as db, db.begin():
        state = await db.get(AppSetting, "catalog:" + provider)
        pages = {url: dict(info) for url, info in state.value["pages"].items()}
        pages[old[0]]["next_scan_at"] = (utcnow() - timedelta(hours=1)).timestamp()
        pages[old[1]]["next_scan_at"] = (utcnow() - timedelta(hours=3)).timestamp()
        state.value = {**state.value, "pages": pages}
    assert await discover_browser_directory(sessions, queue, settings(provider), provider) == 1
    async with sessions() as db:
        job = await db.scalar(select(CrawlJob))
        assert job.payload["official_url"] == old[1]


@pytest.mark.parametrize("provider", ["ihg", "marriott"])
async def test_multi_slot_tick_shares_unseen_and_due_repair_without_exceeding_budget(
    sessions, queue, provider
):
    old, new = await seed(sessions, provider, new_count=4, old_count=4)
    assert await discover_browser_directory(sessions, queue, settings(provider, budget=4), provider) == 4
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob))).all()
        sources = {job.payload["official_url"] for job in jobs}
        assert len(sources & set(old)) == len(sources & set(new)) == 2
        assert len(sources) == 4
