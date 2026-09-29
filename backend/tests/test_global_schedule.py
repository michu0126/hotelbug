from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_pipeline import FixtureProvider

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel
from app.services.watchlists import expand_global


async def test_global_rotation_covers_more_than_first_hundred_hotels(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(global_jobs_per_tick=20)
    async with sessions() as s, s.begin():
        for index in range(105):
            s.add(
                Hotel(
                    id=f"hotel-{index:03}",
                    provider="marriott",
                    provider_hotel_id=str(index),
                    hotel_name=f"Hotel {index}",
                    official_url="https://www.marriott.com/",
                )
            )
    for _ in range(6):
        async with sessions() as s, s.begin():
            await expand_global(s, settings)
    async with sessions() as s:
        assert await s.scalar(select(func.count(func.distinct(CrawlJob.hotel_id)))) == 105


async def test_global_last_night_and_backlog_limit(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings()
    async with sessions() as s, s.begin():
        hotel = await upsert_hotel(s, await FixtureProvider().get_hotel_details("fixture-only"))
        s.add(AppSetting(key=f"global_date_cursor:{hotel.id}", value={"offset": 364}))
        assert await expand_global(s, settings) == 1
        job = await s.scalar(select(CrawlJob))
        assert job.check_out == date.today() + timedelta(days=365)
        assert (await s.get(AppSetting, f"global_date_cursor:{hotel.id}")).value == {"offset": 0}
        settings.max_pending_jobs = 1
        assert await expand_global(s, settings) == 0


@pytest.mark.parametrize("offset", [-1, 365, 366])
async def test_job_entry_rejects_stays_outside_year(sessions, offset):
    check_in = date.today() + timedelta(days=offset)
    async with sessions() as s, s.begin():
        with pytest.raises(ValueError, match="365"):
            await enqueue(
                s,
                JobInput(
                    provider="marriott",
                    kind=JobKind.FETCH_RATE,
                    hotel_id="unused",
                    check_in=check_in,
                    check_out=check_in + timedelta(days=1),
                ),
            )
