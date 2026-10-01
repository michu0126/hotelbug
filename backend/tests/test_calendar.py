from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from test_domain import fixture_rate

from app.models.tables import PriceHistory, Rate, StayScan, utcnow
from app.schemas.domain import HotelData
from app.services.calendar import get_calendar, month_dates
from app.services.rates import finalize_stay_scan, persist_rates, upsert_hotel


async def test_calendar_uses_same_offer_history_and_missing_days(sessions):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="marriott",
                provider_hotel_id="fixture-only",
                hotel_name="Fixture hotel",
                default_currency="USD",
                official_url="https://www.marriott.com/",
            ),
        )
        original = fixture_rate()
        await persist_rates(db, hotel, [original], "job1")
        cheaper = original.model_copy(
            update={
                "cash_price": Decimal("90.10"),
                "total_price": Decimal("100.11"),
                "captured_at": original.captured_at + timedelta(seconds=1),
            }
        )
        await persist_rates(db, hotel, [cheaper], "job2")
        result = await get_calendar(db, hotel, "2027-01")
        day = result["days"][6]
        assert (day["current_low"], day["historical_low"], day["drop_from_previous_percent"]) == (
            "100.1100",
            "100.1100",
            "9.08",
        )
        assert result["days"][0]["status"] == "NO_DATA"
        assert len(result["days"]) == 31


def test_calendar_month_validation():
    assert len(month_dates("2028-02")) == 29
    for bad in ("2027-13", "2027-1", "2027-01-01", "abc"):
        try:
            month_dates(bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted {bad}")


async def test_displayed_only_calendar_and_history_are_separate_from_total(sessions):
    async with sessions() as db, db.begin():
        original = fixture_rate()
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider=original.provider,
                provider_hotel_id=original.provider_hotel_id,
                hotel_name="Fixture hotel",
                official_url=original.source_url,
                default_currency="USD",
            ),
        )
        await persist_rates(db, hotel, [original], "all-fees-total")
        displayed = original.model_copy(
            update={
                "cash_price": Decimal("90"),
                "total_price": None,
                "tax": None,
                "captured_at": original.captured_at + timedelta(seconds=1),
            }
        )
        await persist_rates(db, hotel, [displayed], "displayed")
        cheaper = displayed.model_copy(
            update={
                "cash_price": Decimal("45"),
                "captured_at": original.captured_at + timedelta(seconds=2),
            }
        )
        await persist_rates(db, hotel, [cheaper], "displayed-cheaper")
        day = (await get_calendar(db, hotel, "2027-01"))["days"][6]
        assert day["status"] == "AVAILABLE" and day["price_field"] == "cash_price"
        assert Decimal(day["current_low"]) == Decimal("45")
        assert Decimal(day["historical_low"]) == Decimal("45")
        assert day["drop_from_previous_percent"] == "50.00"
        assert day["offer_key"] != original.offer_key()


async def test_empty_successful_scan_hides_stale_price_and_keeps_history(sessions):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="marriott",
                provider_hotel_id="fixture-only",
                hotel_name="Fixture hotel",
                default_currency="USD",
                official_url="https://www.marriott.com/",
            ),
        )
        rate = fixture_rate()
        await persist_rates(db, hotel, [rate], "priced-job")
        await finalize_stay_scan(
            db, hotel, rate.check_in, rate.check_out, rate.adults, rate.rooms, [rate], "priced-job"
        )
        assert (await get_calendar(db, hotel, "2027-01"))["days"][6]["status"] == "AVAILABLE"

        await finalize_stay_scan(
            db, hotel, rate.check_in, rate.check_out, rate.adults, rate.rooms, [], "empty-job"
        )
        day = (await get_calendar(db, hotel, "2027-01"))["days"][6]
        assert day == {"date": "2027-01-07", "status": "UNAVAILABLE"}
        current = await db.scalar(select(Rate))
        assert not current.availability and current.total_price is None
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert (await db.scalar(select(StayScan))).status == "UNAVAILABLE"

        renewed = rate.model_copy(
            update={
                "captured_at": utcnow() + timedelta(seconds=1),
                "cash_price": None,
                "tax": None,
                "total_price": Decimal("90"),
            }
        )
        await persist_rates(db, hotel, [renewed], "renewed-job")
        await finalize_stay_scan(
            db, hotel, rate.check_in, rate.check_out, rate.adults, rate.rooms, [renewed], "renewed-job"
        )
        restored = (await get_calendar(db, hotel, "2027-01"))["days"][6]
        assert restored["status"] == "AVAILABLE"
        assert Decimal(restored["current_low"]) == Decimal("90")


async def test_first_scan_without_rooms_is_distinct_from_no_data(sessions):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="gha",
                provider_hotel_id="fixture-only",
                hotel_name="Fixture hotel",
                official_url="https://www.ghadiscovery.com/example/hotel",
            ),
        )
        rate = fixture_rate()
        assert (await get_calendar(db, hotel, "2027-01"))["days"][6]["status"] == "NO_DATA"
        await finalize_stay_scan(
            db, hotel, rate.check_in, rate.check_out, rate.adults, rate.rooms, [], "empty-job"
        )
        assert (await get_calendar(db, hotel, "2027-01"))["days"][6]["status"] == "UNAVAILABLE"
