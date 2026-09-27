from datetime import timedelta
from decimal import Decimal

from test_domain import fixture_rate

from app.schemas.domain import HotelData
from app.services.calendar import get_calendar, month_dates
from app.services.rates import persist_rates, upsert_hotel


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
