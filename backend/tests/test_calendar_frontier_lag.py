"""A slow worldwide hotel rotation must not forever select only today's night."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from test_pipeline import FixtureProvider

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob, Watchlist
from app.services import jobs, watchlists
from app.services.rates import upsert_hotel
from app.services.watchlists import advance_date_cursor, expand_global, expand_watchlists


@pytest.mark.parametrize("elapsed_days", [1, 3, 31])
def test_slow_rotation_reaches_future_dates_instead_of_restarting_today(elapsed_days):
    state = {}
    ahead = []
    start = date(2026, 10, 1)
    for index in range(10):
        today = start + timedelta(days=index * elapsed_days)
        chosen, state = advance_date_cursor(state, today, 364)
        ahead.append((chosen - today).days)
        assert today <= chosen <= today + timedelta(days=364)
    assert max(ahead) >= 8, ahead


@pytest.mark.parametrize("horizon", [0, 1, 2, 364])
def test_slow_frontier_visits_each_window_offset_and_wraps(horizon):
    state = {}
    offsets = []
    start = date(2026, 10, 1)
    for index in range(horizon + 6):
        today = start + timedelta(days=index * 3)
        chosen, state = advance_date_cursor(state, today, horizon)
        offsets.append((chosen - today).days)
        assert 0 <= state["offset"] <= horizon
        assert today <= date.fromisoformat(state["next_check_in"]) <= today + timedelta(days=horizon)
    assert set(offsets) == set(range(horizon + 1))
    assert state["rolling_frontier"] is True


@pytest.mark.parametrize("invalid_index", [True, -1, 365, "50"])
def test_invalid_rolling_metadata_cannot_escape_shortened_window(invalid_index):
    today = date(2026, 10, 1)
    chosen, state = advance_date_cursor(
        {
            "next_check_in": "2027-09-30",
            "frontier_index": invalid_index,
            "window_day": None,
            "rolling_frontier": True,
            "lag_count": True,
        },
        today,
        2,
    )
    assert chosen == today
    assert state["frontier_index"] == 1
    assert state["lag_count"] == 0


@pytest.mark.parametrize("scope", ["global", "watchlist"])
@pytest.mark.parametrize("elapsed_days", [1, 3])
async def test_persistent_slow_frontier_survives_new_transactions_and_stays_inside_year(
    sessions, monkeypatch, scope, elapsed_days
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    class ClockDate(date):
        current = date(2026, 10, 1)

        @classmethod
        def today(cls):
            return cls.current

    monkeypatch.setattr(jobs, "date", ClockDate)
    monkeypatch.setattr(watchlists, "date", ClockDate)
    settings = Settings(global_jobs_per_tick=1)
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, await FixtureProvider().get_hotel_details("fixture-only"))
        if scope == "watchlist":
            db.add(
                Watchlist(
                    id="watch-only",
                    name="Offline",
                    scope="hotel",
                    filters={"hotel_id": hotel.id, "days_ahead": 365},
                )
            )
        cursor_key = f"global_date_cursor:{hotel.id}" if scope == "global" else "watch_cursor:watch-only"
    expand = expand_global if scope == "global" else expand_watchlists
    ahead = []
    for index in range(10):
        ClockDate.current = date(2026, 10, 1) + timedelta(days=index * elapsed_days)
        async with sessions() as db, db.begin():
            assert await expand(db, settings) == 1
            created = await db.scalar(select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(1))
            ahead.append((created.check_in - ClockDate.current).days)
            assert (
                ClockDate.current
                <= created.check_in
                < created.check_out
                <= ClockDate.current + timedelta(days=365)
            )
    assert max(ahead) >= 8, ahead
    async with sessions() as db:
        assert (await db.get(AppSetting, cursor_key)).value["rolling_frontier"] is True
