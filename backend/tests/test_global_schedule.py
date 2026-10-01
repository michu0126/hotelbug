from datetime import date, timedelta

import pytest
from sqlalchemy import func, select
from test_pipeline import FixtureProvider

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, Watchlist
from app.schemas.domain import JobInput, JobKind
from app.services import jobs as jobs_module
from app.services import watchlists
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel
from app.services.watchlists import advance_date_cursor, expand_global, expand_watchlists


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
        assert (await s.get(AppSetting, f"global_date_cursor:{hotel.id}")).value == {
            "offset": 0,
            "next_check_in": date.today().isoformat(),
        }
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


@pytest.mark.parametrize("scope", ["global", "watchlist"])
async def test_date_rotation_does_not_skip_a_night_across_midnight(sessions, monkeypatch, scope):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    class ClockDate(date):
        current = date(2026, 10, 1)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(watchlists, "date", ClockDate)
    monkeypatch.setattr(jobs_module, "date", ClockDate)
    settings = Settings()
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
        if scope == "watchlist":
            session.add(
                Watchlist(name="Nightly", scope="hotel", filters={"hotel_id": hotel.id, "days_ahead": 365})
            )
        expand = expand_global if scope == "global" else expand_watchlists
        assert await expand(session, settings) == 1
    ClockDate.current += timedelta(days=1)
    async with sessions() as session, session.begin():
        assert await expand(session, settings) == 1
        dates = (await session.scalars(select(CrawlJob.check_in).order_by(CrawlJob.check_in))).all()
        assert dates == [date(2026, 10, 1), date(2026, 10, 2)]


@pytest.mark.parametrize(
    ("value", "horizon", "expected"),
    [
        ({"offset": 9}, 364, date(2026, 10, 10)),
        ({"next_check_in": "2026-10-10", "offset": 50}, 364, date(2026, 10, 10)),
        ({"next_check_in": "2026-09-30"}, 364, date(2026, 10, 1)),
        ({"next_check_in": "2027-10-01"}, 364, date(2026, 10, 1)),
        ({"next_check_in": "2026-11-01"}, 2, date(2026, 10, 1)),
        ({"next_check_in": "not-a-date", "offset": 2}, 364, date(2026, 10, 3)),
    ],
)
def test_absolute_date_cursor_migration_and_window_bounds(value, horizon, expected):
    chosen, next_value = advance_date_cursor(value, date(2026, 10, 1), horizon)
    assert chosen == expected
    assert (
        date(2026, 10, 1)
        <= date.fromisoformat(next_value["next_check_in"])
        <= date(2026, 10, 1) + timedelta(days=horizon)
    )


def test_absolute_cursor_preserves_unvisited_date_after_month_changes():
    _, saved = advance_date_cursor({"offset": 50}, date(2026, 10, 1), 364)
    chosen, _ = advance_date_cursor(saved, date(2026, 11, 1), 364)
    assert chosen == date(2026, 11, 21)


def test_absolute_cursor_visits_every_night_and_wraps_at_year_boundary():
    today = date(2026, 10, 1)
    saved = {}
    dates = []
    for _ in range(365):
        chosen, saved = advance_date_cursor(saved, today, 364)
        dates.append(chosen)
    assert dates == [today + timedelta(days=offset) for offset in range(365)]
    assert saved == {"offset": 0, "next_check_in": today.isoformat()}
