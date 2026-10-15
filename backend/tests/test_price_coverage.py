"""Coverage is confirmed distinct stays, never discovered hotels or queued jobs."""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, StayScan
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.domain import JobKind
from app.services.price_coverage import one_night_condition, price_coverage_by_provider

NOW = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
TODAY = NOW.date()


def hotel(identity="one", provider="marriott", *, active=True, chinese=True):
    host = "www.marriott.com.cn" if chinese else "www.marriott.com"
    return Hotel(
        id=identity,
        provider=provider,
        provider_hotel_id=identity.upper(),
        hotel_name=f"Offline {identity}",
        official_url=f"https://{host}/hotels/{identity}",
        active=active,
    )


def scan(offset, *, hotel_id="one", age=1, status="AVAILABLE", nights=1, adults=2, rooms=1):
    return StayScan(
        hotel_id=hotel_id,
        check_in=TODAY + timedelta(days=offset),
        check_out=TODAY + timedelta(days=offset + nights),
        adults=adults,
        rooms=rooms,
        status=status,
        observed_at=NOW - timedelta(hours=age),
        job_id=f"offline-{hotel_id}-{offset}-{nights}-{adults}-{rooms}",
    )


def source_job(observation, root):
    return CrawlJob(
        id=observation.job_id,
        hotel_id=observation.hotel_id,
        provider="marriott",
        kind="FETCH_RATE",
        status="SUCCEEDED",
        check_in=observation.check_in,
        check_out=observation.check_out,
        dedupe_key=observation.job_id,
        payload={"automatic_catalog_root": root},
    )


async def test_empty_catalog_is_not_complete_or_worldwide_coverage(sessions):
    async with sessions() as db:
        result = await price_coverage_by_provider(db, Settings(), NOW)
    assert set(result) == {"marriott", "ihg", "hyatt", "hilton", "accor", "gha"}
    for row in result.values():
        assert row["scope"] == "ACTIVE_COLLECTED_HOTELS_CURRENT_SOURCE"
        assert row["eligible_hotels"] == row["required_stays"] == row["fresh_stays"] == 0
        assert not row["collected_first_pass_complete"]
        assert not row["collected_fresh_coverage_complete"]
        assert row["query_mode"] == "SINGLE_STAY"


async def test_tier_boundaries_and_successful_no_inventory_are_distinct_fresh_stays(sessions):
    async with sessions() as db, db.begin():
        db.add(hotel())
        db.add_all(
            [
                scan(0, age=2),
                scan(30, age=3),  # Due at the exact threshold.
                scan(31, age=8, status="UNAVAILABLE"),
                scan(90, age=9),
                scan(91, age=47, status="NOT_OPEN"),
                scan(364, age=48),
            ]
        )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["required_stays"] == 365
    assert row["previously_scanned_stays"] == 6
    assert row["fresh_stays"] == row["stale_stays"] == 3
    assert row["unscanned_stays"] == 359 and row["pending_stays"] == 362
    assert row["fresh_available_stays"] == row["fresh_unavailable_stays"] == row["fresh_not_open_stays"] == 1


async def test_scope_excludes_old_source_inactive_nondefault_multinight_expired_and_future_rows(sessions):
    async with sessions() as db, db.begin():
        db.add_all([hotel(), hotel("old", chinese=False), hotel("disabled", active=False)])
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add(source_job(scan(1), CN_ROOT))
        db.add_all(
            [
                scan(1),
                scan(1, hotel_id="old"),
                scan(1, hotel_id="disabled"),
                scan(2, adults=1),
                scan(3, rooms=2),
                scan(4, nights=2),
                scan(-1),
                scan(365),
                scan(5, status="FAILED"),
                scan(6, age=-1),
            ]
        )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["eligible_hotels"] == 1
    assert row["required_stays"] == 365
    assert row["fresh_stays"] == row["previously_scanned_stays"] == 1


async def test_changed_hotel_url_does_not_relabel_old_or_unknown_source_observations(sessions):
    async with sessions() as db, db.begin():
        db.add(hotel(chinese=True))  # Hotel was refreshed after the EN observations.
        db.add(AppSetting(key="catalog:marriott", value={"root": CN_ROOT}))
        db.add_all([scan(1), scan(2), scan(3, status="UNAVAILABLE"), scan(4)])
        db.add(source_job(scan(1), EN_ROOT))
        db.add(source_job(scan(3), CN_ROOT))
        # Unmarked/manual real quote is still valid evidence for its actual source.
        observed = scan(4)
        db.add(
            PriceHistory(
                hotel_id="one",
                provider="marriott",
                offer_key="a" * 64,
                check_in=observed.check_in,
                check_out=observed.check_out,
                adults=2,
                rooms=1,
                currency="USD",
                cash_price=100,
                availability=True,
                price_basis="nightly",
                captured_at=observed.observed_at,
                source_url="https://www.marriott.com.cn/reservation/rateListMenu.mi",
                raw_response_hash="b" * 64,
                job_id=observed.job_id,
            )
        )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["fresh_stays"] == row["previously_scanned_stays"] == 2
    assert row["source_unverified_stays"] == 2
    assert row["fresh_unavailable_stays"] == row["fresh_available_stays"] == 1


async def test_enqueued_calendar_and_rate_jobs_do_not_count_as_observations(sessions):
    async with sessions() as db, db.begin():
        db.add(hotel())
        for kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR):
            db.add(
                CrawlJob(
                    provider="marriott",
                    kind=kind,
                    hotel_id="one",
                    check_in=TODAY,
                    check_out=TODAY + timedelta(days=1),
                    dedupe_key=kind.value,
                    status="QUEUED",
                )
            )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
    assert row["previously_scanned_stays"] == row["fresh_stays"] == 0
    assert row["pending_stays"] == 365


async def test_full_collected_coverage_can_expire_without_changing_first_pass(sessions):
    async with sessions() as db, db.begin():
        db.add(hotel())
        db.add_all(scan(offset) for offset in range(365))
    async with sessions() as db:
        fresh = (await price_coverage_by_provider(db, Settings(), NOW))["marriott"]
        stale = (await price_coverage_by_provider(db, Settings(), NOW + timedelta(hours=3)))["marriott"]
    assert fresh["fresh_stays"] == 365 and fresh["pending_stays"] == 0
    assert fresh["collected_fresh_coverage_complete"]
    assert stale["collected_first_pass_complete"]
    assert not stale["collected_fresh_coverage_complete"]
    assert stale["stale_stays"] == stale["pending_stays"] == 31


async def test_timezone_window_uses_built_in_shanghai_midnight(sessions):
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, Settings(), NOW.replace(hour=17)))["marriott"]
    assert row["window_start"] == "2026-10-15"
    assert row["window_end_exclusive"] == "2027-10-15"


async def test_configured_refresh_work_cannot_be_mistaken_for_actual_throughput(sessions):
    async with sessions() as db, db.begin():
        db.add_all(hotel(str(index)) for index in range(32))
    settings = Settings(
        provider_overrides={"marriott": ProviderPolicy(enabled=True, interval_seconds=5, concurrency=4)}
    )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, settings, NOW))["marriott"]
    assert row["required_refresh_jobs_per_day"] == 32 * 545
    assert row["provider_start_ceiling_per_day"] == 17280
    assert row["minimum_first_pass_start_span_seconds"] == (32 * 365 - 1) * 5
    assert row["refresh_demand_exceeds_start_ceiling"]
    assert row["provider_enabled"] and row["global_monitoring_enabled"]


async def test_custom_intervals_disabled_provider_and_global_off_keep_evidence(sessions):
    async with sessions() as db, db.begin():
        db.add(hotel())
        db.add(scan(0, age=3))
    settings = Settings(
        global_monitoring_enabled=False,
        hot_interval_hours=4,
        warm_interval_hours=12,
        cold_interval_hours=72,
        provider_overrides={"marriott": ProviderPolicy(enabled=False, interval_seconds=60)},
    )
    async with sessions() as db:
        row = (await price_coverage_by_provider(db, settings, NOW))["marriott"]
    assert row["fresh_stays"] == 1
    assert row["required_refresh_jobs_per_day"] == round(31 * 6 + 60 * 2 + 274 / 3, 3)
    assert row["provider_start_ceiling_per_day"] == 1440
    assert not row["provider_enabled"] and not row["global_monitoring_enabled"]


async def test_queries_are_grouped_and_count_is_constant_as_catalog_grows(sessions):
    async with sessions() as db, db.begin():
        db.add_all(hotel(str(index)) for index in range(50))
        db.add_all(scan(index % 365, hotel_id=str(index)) for index in range(50))
    async with sessions() as db:
        queries = []
        bind = db.get_bind()

        def record(_connection, _cursor, statement, *_args):
            queries.append(statement)

        event.listen(bind, "before_cursor_execute", record)
        try:
            result = await price_coverage_by_provider(db, Settings(), NOW)
        finally:
            event.remove(bind, "before_cursor_execute", record)
    assert len(queries) == 3  # One source setting read plus exactly two aggregate queries.
    assert sum("GROUP BY" in statement for statement in queries) == 2
    assert result["marriott"]["fresh_stays"] == 50


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
def test_one_night_filter_compiles_for_supported_databases(dialect):
    sql = str(
        one_night_condition(dialect.name).compile(dialect=dialect, compile_kwargs={"literal_binds": True})
    )
    if dialect.name == "sqlite":
        assert "julianday" in sql
    else:
        assert "stay_scans.check_out = stay_scans.check_in + 1" in sql
