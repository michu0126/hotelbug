"""Monthly minimum-price observations never inflate confirmed stay coverage."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import event

from app.core.config import Settings
from app.models.tables import AppSetting, CalendarHintHistory, CrawlJob, Hotel, PriceHistory, StayScan
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.services.calendar_hint_coverage import BASIS, calendar_hint_coverage_by_provider
from app.services.price_coverage import price_coverage_by_provider

NOW = datetime(2026, 10, 15, 4, tzinfo=timezone.utc)
TODAY = NOW.date()


def hotel(identity="one", *, active=True, chinese=True, provider="marriott"):
    return Hotel(
        id=identity,
        provider=provider,
        provider_hotel_id="PBHMD" if identity == "one" else identity.upper().rjust(5, "0")[:5],
        hotel_name="Offline calendar hotel " + identity,
        official_url=f"https://www.marriott.com{'.cn' if chinese else ''}/hotels/{identity}",
        active=active,
    )


def hint(offset=0, *, age=1, **overrides):
    identity = overrides.get("hotel_id", "one")
    code = "PBHMD" if identity == "one" else identity.upper().rjust(5, "0")[:5]
    return CalendarHintHistory(
        **{
            "hotel_id": "one",
            "provider": "marriott",
            "job_id": str(uuid4()),
            "series_key": "a" * 64,
            "check_in": TODAY + timedelta(days=offset),
            "adults": 2,
            "rooms": 1,
            "displayed_price": Decimal("617"),
            "currency": "USD",
            "basis": BASIS,
            "source_url": f"https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode={code}&isSearch=true&currency=",
            "captured_at": NOW - timedelta(hours=age),
            "raw_response_hash": "b" * 64,
            **overrides,
        }
    )


@pytest.mark.parametrize("root", [None, EN_ROOT, "https://unverified.invalid"])
async def test_without_accepted_cn_source_counts_are_unknown_not_zero(sessions, root):
    async with sessions() as db, db.begin():
        if root:
            db.add(AppSetting(key="catalog:marriott", value={"root": root, "requested_root": CN_ROOT}))
        db.add(hotel())
        db.add(hint())
    async with sessions() as db:
        result = await calendar_hint_coverage_by_provider(db, Settings(), NOW)
    assert len(result) == 6
    for row in result.values():
        assert not row["supported"]
        assert row["fresh_dates"] is None and row["observed_dates"] is None
        assert row["observed_hotels"] is None and row["eligible_hotels"] is None
    assert result["marriott"]["status"] == "CN_SOURCE_NOT_SELECTED"


async def test_accepted_source_with_no_observations_is_supported_but_not_measured_coverage(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(hotel())
    async with sessions() as db:
        result = await calendar_hint_coverage_by_provider(db, Settings(), NOW)
    row = result["marriott"]
    assert row["supported"]
    assert row["required_dates"] == 365 and row["eligible_hotels"] == 1
    assert row["observed_dates"] == row["fresh_dates"] == row["stale_dates"] == row["observed_hotels"] == 0
    assert result["ihg"]["status"] == "UNSUPPORTED_PROVIDER" and result["ihg"]["observed_dates"] is None


async def test_hot_warm_cold_cutoffs_and_duplicate_history_use_latest_distinct_hotel_date(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add_all([hotel(), hotel("two")])
        db.add_all(
            [
                hint(0, age=30),
                hint(0, age=2),
                hint(0, age=1, currency="CNY"),
                hint(30, age=3),
                hint(31, age=8),
                hint(90, age=9),
                hint(91, age=47),
                hint(364, age=48),
                hint(0, age=1, hotel_id="two"),
            ]
        )
    async with sessions() as db:
        row = (await calendar_hint_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["observed_dates"] == 7
    assert row["fresh_dates"] == 4 and row["stale_dates"] == 3
    assert row["observed_hotels"] == row["eligible_hotels"] == 2
    assert row["required_dates"] == 730


async def test_scope_excludes_inactive_old_source_wrong_conditions_and_out_of_window_rows(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add_all(
            [
                hotel(),
                hotel("disabled", active=False),
                hotel("old", chinese=False),
                hotel("other", provider="ihg"),
            ]
        )
        db.add_all(
            [
                hint(),
                hint(1, hotel_id="disabled"),
                hint(2, hotel_id="old"),
                hint(3, hotel_id="other"),
                hint(4, provider="ihg"),
                hint(5, adults=1),
                hint(6, rooms=2),
                hint(-1),
                hint(365),
                hint(7, age=-1),
                hint(8, basis="UNKNOWN"),
                hint(9, displayed_price=0),
                hint(
                    10,
                    source_url="https://www.marriott.com/search/availabilityCalendar.mi?propertyCode=PBHMD",
                ),
                hint(
                    11,
                    source_url="https://www.marriott.com.cn.evil.invalid/search/availabilityCalendar.mi?propertyCode=PBHMD",
                ),
                hint(
                    12,
                    source_url="https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=PBHPR&isSearch=true&currency=",
                ),
            ]
        )
    async with sessions() as db:
        row = (await calendar_hint_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert (
        row["eligible_hotels"] == row["observed_hotels"] == row["observed_dates"] == row["fresh_dates"] == 1
    )


async def test_jobs_stay_scans_and_room_offers_are_not_calendar_hints_and_vice_versa(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(hotel())
        db.add(hint())
        db.add(
            CrawlJob(
                hotel_id="one",
                provider="marriott",
                kind="FETCH_CALENDAR_HINTS",
                status="QUEUED",
                dedupe_key="pending-hint",
            )
        )
        db.add(
            StayScan(
                hotel_id="one",
                check_in=TODAY + timedelta(days=1),
                check_out=TODAY + timedelta(days=2),
                adults=2,
                rooms=1,
                observed_at=NOW,
                status="AVAILABLE",
                job_id="room-job",
            )
        )
        db.add(
            PriceHistory(
                hotel_id="one",
                provider="marriott",
                job_id="room-job",
                offer_key="c" * 64,
                check_in=TODAY + timedelta(days=1),
                check_out=TODAY + timedelta(days=2),
                adults=2,
                rooms=1,
                currency="USD",
                total_price=700,
                availability=True,
                price_basis="stay_total",
                captured_at=NOW,
                source_url="https://www.marriott.com.cn/reservation/rateListMenu.mi",
                raw_response_hash="d" * 64,
            )
        )
    async with sessions() as db:
        calendar = (await calendar_hint_coverage_by_provider(db, Settings(), NOW))["marriott"]
        rooms = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert calendar["fresh_dates"] == calendar["observed_dates"] == 1
    assert rooms["fresh_stays"] == rooms["previously_scanned_stays"] == 1


async def test_newer_future_timestamp_does_not_hide_valid_past_observation(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(hotel())
        db.add_all([hint(0, age=10), hint(0, age=-1)])
    async with sessions() as db:
        row = (await calendar_hint_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["observed_dates"] == row["stale_dates"] == 1 and row["fresh_dates"] == 0


async def test_configured_intervals_and_shanghai_midnight_are_used(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(hotel())
        db.add(hint(1, age=3))
    async with sessions() as db:
        custom = (await calendar_hint_coverage_by_provider(db, Settings(hot_interval_hours=4), NOW))[
            "marriott"
        ]
        next_day = (await calendar_hint_coverage_by_provider(db, Settings(), NOW.replace(hour=17)))[
            "marriott"
        ]
    assert custom["fresh_dates"] == 1
    assert custom["freshness_hours"]["hot"] == 4
    assert next_day["window_start"] == "2026-10-16"
    assert next_day["window_end_exclusive"] == "2027-10-16"


async def test_query_count_is_constant_and_no_history_rows_are_loaded_into_python(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add_all(hotel(str(index)) for index in range(50))
        db.add_all(hint(index, hotel_id=str(index)) for index in range(50))
    async with sessions() as db:
        queries = []
        bind = db.get_bind()

        def record(_connection, _cursor, statement, *_args):
            queries.append(statement)

        event.listen(bind, "before_cursor_execute", record)
        try:
            row = (await calendar_hint_coverage_by_provider(db, Settings(), NOW))["marriott"]
        finally:
            event.remove(bind, "before_cursor_execute", record)
    assert len(queries) == 3
    assert any("GROUP BY" in query and "max(calendar_hint_history.captured_at)" in query for query in queries)
    assert row["fresh_dates"] == row["observed_hotels"] == 50


async def test_catalog_api_returns_separate_hint_coverage(sessions, monkeypatch):
    from app.api.main import app
    from app.database.session import get_session
    from app.services import calendar_hint_coverage as coverage_module

    monkeypatch.setattr(coverage_module, "utcnow", lambda: NOW)
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(hotel())
        db.add(hint())

    async def dependency():
        async with sessions() as db:
            yield db

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/catalogs")
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous
    assert response.status_code == 200
    rows = {row["provider"]: row for row in response.json()}
    assert rows["marriott"]["calendar_hints"]["fresh_dates"] == 1
    assert rows["marriott"]["price_coverage"]["fresh_stays"] == 0
    assert rows["ihg"]["calendar_hints"]["supported"] is False
