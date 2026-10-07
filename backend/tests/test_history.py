from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import insert
from test_domain import fixture_rate

from app.api.main import app
from app.database.session import get_session
from app.models.tables import PriceHistory
from app.schemas.domain import HotelData
from app.services import history
from app.services.rates import persist_rates, upsert_hotel

NOW = datetime(2026, 10, 4, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(history, "utcnow", lambda: NOW)


async def hotel(db, code="fixture-only"):
    return await upsert_hotel(
        db,
        HotelData(
            provider="marriott",
            provider_hotel_id=code,
            hotel_name="Offline history test",
            official_url="https://www.marriott.com/",
        ),
    )


def quote(value="100", age=1, **changes):
    return fixture_rate().model_copy(
        update={
            "cash_price": None,
            "tax": None,
            "total_price": Decimal(value),
            "captured_at": NOW - timedelta(days=age),
            **changes,
        }
    )


async def test_daily_aggregates_keep_gaps_and_full_decimal_amounts(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        rates = [
            quote("100.1234", 3),
            quote("200.5678", 3, captured_at=NOW - timedelta(days=3, hours=1)),
            quote("70.1111", 1),
        ]
        for i, rate in enumerate(rates):
            await persist_rates(db, h, [rate], str(i))
        result = await history.get_offer_trend(db, h, rates[0].offer_key(), 7)
        assert result["observation_count"] == 3 and result["observation_days"] == 2
        assert [p["date"] for p in result["points"]] == ["2026-10-01", "2026-10-03"]
        assert result["points"][0] == {
            "date": "2026-10-01",
            "low": "100.1234",
            "high": "200.5678",
            "mean": "150.3456",
            "samples": 2,
        }
        assert result["last_observed_price"] == "70.1111"
        assert result["window_high"] == "200.5678"
        assert result["timezone"] == "UTC"


async def test_all_samples_aggregate_without_1000_row_truncation(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        rate = quote()
        values = rate.model_dump(exclude={"provider_hotel_id"})
        values.update(hotel_id=h.id, offer_key=rate.offer_key(), source_url=str(rate.source_url))
        await db.execute(
            insert(PriceHistory),
            [
                {
                    **values,
                    "job_id": str(i),
                    "captured_at": rate.captured_at + timedelta(seconds=i),
                    "total_price": Decimal("1") if i == 0 else Decimal("10"),
                }
                for i in range(1101)
            ],
        )
        result = await history.get_offer_trend(db, h, rate.offer_key(), 7)
        assert result["observation_count"] == 1101
        assert result["window_low"] == "1.0000"
        assert result["points"][0]["samples"] == 1101


@pytest.mark.parametrize("days,count", [(1, 1), (7, 2), (30, 3), (90, 4)])
async def test_window_and_future_observations_are_excluded(sessions, days, count):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        for i, age in enumerate((0.01, 2, 10, 40, 100, -1)):
            await persist_rates(db, h, [quote(str(100 + i), age)], str(i))
        result = await history.get_offer_trend(db, h, quote().offer_key(), days)
        assert result["observation_count"] == count
        assert result["last_observed_price"] == "100.0000"
        assert result["historical_low"] == "100.0000"


async def test_other_offer_conditions_never_enter_trend(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        original = quote()
        await persist_rates(db, h, [original], "original")
        changes = [
            {"currency": "EUR"},
            {"room_code": "SUITE"},
            {"rate_code": "ADVANCE"},
            {"adults": 1},
            {"rooms": 2},
            {"member_rate": True},
            {"refundable": None},
            {"price_basis": "nightly"},
            {"check_out": original.check_out + timedelta(days=1)},
            {"total_price": None, "cash_price": Decimal("1")},
        ]
        for i, change in enumerate(changes):
            await persist_rates(db, h, [quote("1", **change)], str(i))
        result = await history.get_offer_trend(db, h, original.offer_key(), 30)
        assert result["observation_count"] == 1 and result["window_low"] == "100.0000"
        assert result["offer"]["price_field"] == "total_price"
        offers = await history.get_stay_offers(db, h, original.check_in)
        assert len(offers["offers"]) == 11
        other = await hotel(db, "other-hotel")
        assert await history.get_offer_trend(db, other, original.offer_key(), 30) is None


async def test_offer_keyset_pagination_deduplicates_and_resumes(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        original = quote()
        for i in range(103):
            rate = quote(rate_code=f"PLAN{i}")
            await persist_rates(db, h, [rate], f"first{i}")
            await persist_rates(
                db, h, [rate.model_copy(update={"captured_at": NOW - timedelta(hours=1)})], f"second{i}"
            )
        # Different stay and future-only offer do not leak into this date's choices.
        await persist_rates(db, h, [quote(rate_code="FUTURE", age=-1)], "future")
        await persist_rates(
            db,
            h,
            [
                quote(
                    check_in=original.check_in + timedelta(days=1),
                    check_out=original.check_out + timedelta(days=1),
                )
            ],
            "otherdate",
        )
        first = await history.get_stay_offers(db, h, original.check_in, limit=100)
        second = await history.get_stay_offers(
            db, h, original.check_in, after=first["next_cursor"], limit=100
        )
        keys = [o["offer_key"] for o in first["offers"] + second["offers"]]
        assert len(first["offers"]) == 100 and len(second["offers"]) == 3
        assert len(set(keys)) == 103 and keys == sorted(keys)
        assert second["next_cursor"] is None


async def test_empty_window_keeps_all_time_low_without_filling_prices(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        rate = quote("50", age=100)
        await persist_rates(db, h, [rate], "old")
        result = await history.get_offer_trend(db, h, rate.offer_key(), 90)
        assert result["points"] == [] and result["observation_count"] == 0
        assert result["last_observed_price"] is None and result["window_low"] is None
        assert result["historical_low"] == "50.0000"


async def test_unavailable_zero_and_points_only_do_not_become_cash_chart(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        rate = quote()
        await persist_rates(db, h, [rate], "cash")
        await persist_rates(db, h, [quote("0")], "zero")
        await persist_rates(db, h, [quote("1", availability=False)], "unavailable")
        result = await history.get_offer_trend(db, h, rate.offer_key(), 30)
        assert result["observation_count"] == 1
        points = quote(total_price=None, cash_price=None, points_price=20000, age=0.01)
        await persist_rates(db, h, [points], "points")
        empty = await history.get_offer_trend(db, h, points.offer_key(), 30)
        # Points-only data shares the old all-fees key convention, but cannot lower cash aggregates.
        assert empty["observation_count"] == 1


async def test_api_history_validation_isolation_and_readonly(sessions):
    async with sessions() as db, db.begin():
        h = await hotel(db)
        rate = quote()
        await persist_rates(db, h, [rate], "api")
        h_id = h.id

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            root = f"/api/hotels/{h_id}"
            assert (await client.get(root + f"/offers?check_in={rate.check_in}")).json()["offers"][0][
                "offer_key"
            ] == rate.offer_key()
            assert (await client.get(root + f"/trend?offer_key={rate.offer_key()}&days=7")).json()[
                "observation_count"
            ] == 1
            for query in (
                "check_in=bad",
                "check_in=2027-01-07&check_out=2027-01-06",
                "check_in=2027-01-07&limit=0",
                "check_in=2027-01-07&cursor=bad",
            ):
                assert (await client.get(root + "/offers?" + query)).status_code == 422
            for query in ("offer_key=bad", f"offer_key={rate.offer_key()}&days=91"):
                assert (await client.get(root + "/trend?" + query)).status_code == 422
            assert (await client.get(root + f"/trend?offer_key={'0' * 64}")).status_code == 404
            assert (
                await client.get(f"/api/hotels/missing/trend?offer_key={rate.offer_key()}")
            ).status_code == 404
    finally:
        app.dependency_overrides.clear()
