from datetime import timedelta
from unittest.mock import AsyncMock

import audit_ihg_sitemap as audit
import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, PriceHistory, ProviderStatus, utcnow
from app.providers.ihg_sitemap import STATE_KEY
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services.jobs import enqueue

URL = "https://www.ihg.com/armyhotels/hotels/us/en/aberdeen-proving-ground/zyapb/hoteldetail"


def config():
    return Settings(
        _env_file=None, provider_overrides={"ihg": ProviderPolicy(enabled=True)}, global_jobs_per_tick=1
    )


async def marker(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="audit:directory-only", value={"provider": "ihg"}))


async def detail(sessions):
    async with sessions() as db, db.begin():
        job = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.DISCOVER_HOTELS,
                payload={"official_url": URL, "catalog_source": STATE_KEY},
            ),
        )
        return job.id


async def test_requires_existing_ihg_isolated_marker_before_any_source_request(sessions, queue):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No request authorized"))
    ) as client:
        with pytest.raises(AssertionError, match="isolated IHG"):
            await audit.run(sessions, queue, config(), client, emit=lambda _: None)
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0
        assert await db.get(AppSetting, STATE_KEY) is None


async def test_final_tick_can_queue_a_detail_without_reporting_it_executed(sessions, queue, monkeypatch):
    await marker(sessions)

    async def discover(*args):
        await detail(sessions)
        return 1

    monkeypatch.setattr(audit, "discover_ihg_sitemap", discover)
    async with httpx.AsyncClient() as client:
        report = await audit.run(sessions, queue, config(), client, max_ticks=1, emit=lambda _: None)
    assert report["outcome"] == "DETAIL_QUEUED_PENDING" and report["pending_detail_jobs"] == 1
    assert report["job_id"] is None and report["real_hotels_persisted"] == 0
    assert report["year_jobs_generated"] == report["directory_quotes"] == report["notifications"] == 0


async def test_existing_due_supplement_uses_worker_then_year_scheduler_not_an_extra_map_read(
    sessions, queue, monkeypatch
):
    await marker(sessions)
    job_id = await detail(sessions)
    execute = AsyncMock(
        return_value=[
            HotelData(provider="ihg", provider_hotel_id="ZYAPB", hotel_name="Offline hotel", official_url=URL)
        ]
    )
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("No extra map read needed"))
    ) as client:
        report = await audit.run(sessions, queue, config(), client, emit=lambda _: None)
    assert report["outcome"] == "DETAIL_SUCCEEDED" and report["job_id"] == job_id
    assert report["real_hotels_persisted"] == report["year_jobs_generated"] == 1
    assert report["directory_quotes"] == report["notifications"] == 0
    execute.assert_awaited_once()
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0


async def test_successful_old_supplement_is_not_retested_during_source_cooldown(sessions, queue, monkeypatch):
    await marker(sessions)
    job_id = await detail(sessions)
    deadline = utcnow().timestamp() + 3600
    async with sessions() as db, db.begin():
        (await db.get(CrawlJob, job_id)).status = "SUCCEEDED"
        db.add(AppSetting(key=STATE_KEY, value={"next_fetch_at": deadline}))
    execute = AsyncMock(side_effect=AssertionError("Do not repeat a successful hotel"))
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Source not due"))
    ) as client:
        report = await audit.run(sessions, queue, config(), client, emit=lambda _: None)
    assert report["outcome"] == "SOURCE_RETRY_NOT_DUE" and report["job_id"] is None
    execute.assert_not_awaited()
    async with sessions() as db:
        assert (await db.get(AppSetting, STATE_KEY)).value["next_fetch_at"] == deadline


async def test_provider_pause_prevents_detail_execution_and_source_read(sessions, queue, monkeypatch):
    await marker(sessions)
    job_id = await detail(sessions)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="ihg", status="BLOCKED", blocked_until=utcnow() + timedelta(hours=1)))
    execute = AsyncMock(side_effect=AssertionError("Paused provider"))
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("Paused source"))
    ) as client:
        report = await audit.run(sessions, queue, config(), client, emit=lambda _: None)
    assert report["outcome"] == "PROVIDER_PAUSED" and report["pending_detail_jobs"] == 1
    execute.assert_not_awaited()
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "PENDING"
