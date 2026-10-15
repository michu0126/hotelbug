"""Calendar minima have their own typed, bounded, non-offer contract."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.schemas import calendar_hints
from app.schemas.calendar_hints import CalendarHintBatch, CalendarHintDay, CalendarHintRequest, month_window
from app.schemas.domain import RateData

SOURCE = (
    "https://www.marriott.com.cn/search/availabilityCalendar.mi"
    "?isRateCalendar=true&propertyCode=PBHMD&isSearch=true&currency="
)


def request_data(**changes):
    return {
        "provider_hotel_id": "PBHMD",
        "start_date": date(2026, 10, 14),
        "end_date": date(2026, 11, 1),
        **changes,
    }


def batch_data(**changes):
    return {
        **request_data(),
        "currency": "USD",
        "source_url": SOURCE,
        "raw_response_hash": "a" * 64,
        "days": [{"check_in": date(2026, 10, 15), "displayed_price": "617.50"}],
        **changes,
    }


@pytest.mark.parametrize(
    ("start", "end"),
    [
        (date(2026, 10, 1), date(2026, 11, 1)),
        (date(2026, 10, 31), date(2026, 11, 1)),
        (date(2026, 10, 14), date(2026, 10, 16)),
        (date(2026, 12, 1), date(2027, 1, 1)),
        (date(2028, 2, 1), date(2028, 3, 1)),
    ],
)
def test_request_accepts_one_natural_month_including_exclusive_next_month(start, end):
    request = CalendarHintRequest(**request_data(start_date=start, end_date=end))
    assert (request.start_date, request.end_date, request.adults, request.rooms) == (start, end, 2, 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"start_date": date(2026, 11, 1)},
        {"start_date": date(2026, 11, 2)},
        {"end_date": date(2026, 11, 2)},
        {"start_date": date(2026, 10, 1), "end_date": date(2026, 11, 2)},
        {"adults": 1},
        {"adults": 3},
        {"rooms": 2},
        {"provider_hotel_id": ""},
        {"provider_hotel_id": "pbhmd"},
        {"provider_hotel_id": "PBHMD/"},
        {"check_in": date(2026, 10, 14)},
    ],
)
def test_request_rejects_cross_month_invalid_stay_and_unverified_occupancy(changes):
    with pytest.raises(ValidationError):
        CalendarHintRequest(**request_data(**changes))


def test_batch_is_explicitly_not_a_rate_and_retains_precise_positive_minimum():
    batch = CalendarHintBatch(**batch_data())
    assert batch.provider == "marriott"
    assert batch.basis == "CALENDAR_MINIMUM_TAX_UNCONFIRMED"
    assert batch.days[0].displayed_price == Decimal("617.50")
    assert batch.captured_at.utcoffset() == timedelta(0)
    assert not isinstance(batch, RateData)
    assert "availability" not in batch.model_dump()
    assert "total_price" not in batch.model_dump()
    assert CalendarHintBatch.model_validate_json(batch.model_dump_json()) == batch


@pytest.mark.parametrize("amount", ["0", "-0.01", "NaN", "Infinity", "-Infinity"])
def test_hint_amount_is_finite_positive_not_a_zero_or_unavailable_offer(amount):
    with pytest.raises(ValidationError):
        CalendarHintDay(check_in=date(2026, 10, 14), displayed_price=amount)


@pytest.mark.parametrize(
    "changes",
    [
        {"provider": "ihg"},
        {"currency": "$"},
        {"currency": "usd"},
        {"currency": "US"},
        {"basis": "stay_total"},
        {"captured_at": datetime(2026, 10, 14)},
        {"raw_response_hash": "a" * 63},
        {"raw_response_hash": "z" * 64},
        {"days": []},
        {"availability": True},
    ],
)
def test_batch_rejects_missing_evidence_or_offer_semantics(changes):
    with pytest.raises(ValidationError):
        CalendarHintBatch(**batch_data(**changes))


@pytest.mark.parametrize("day", [date(2026, 10, 13), date(2026, 11, 1), date(2027, 1, 1)])
def test_batch_day_must_fall_inside_exclusive_requested_window(day):
    with pytest.raises(ValidationError, match="outside requested window"):
        CalendarHintBatch(**batch_data(days=[{"check_in": day, "displayed_price": "100"}]))


def test_duplicate_dates_are_not_independent_observations():
    with pytest.raises(ValidationError, match="duplicate calendar hint date"):
        CalendarHintBatch(
            **batch_data(
                days=[
                    {"check_in": "2026-10-15", "displayed_price": "100"},
                    {"check_in": "2026-10-15", "displayed_price": "90"},
                ]
            )
        )


@pytest.mark.parametrize(
    "source",
    [
        SOURCE.replace("https://", "http://"),
        SOURCE.replace("www.marriott.com.cn", "www.marriott.com"),
        SOURCE.replace("www.marriott.com.cn", "www.marriott.com.cn.evil.example"),
        SOURCE.replace("www.marriott.com.cn", "user:password@www.marriott.com.cn"),
        SOURCE.replace("www.marriott.com.cn", "www.marriott.com.cn:8443"),
        SOURCE.replace("availabilityCalendar.mi", "rateListMenu.mi"),
        SOURCE.replace("PBHMD", "PBHPR"),
        SOURCE.replace("propertyCode=PBHMD", "propertyCode="),
        SOURCE + "&propertyCode=PBHMD",
        SOURCE.replace("isRateCalendar=true", "isRateCalendar=false"),
        SOURCE.replace("isSearch=true", "isSearch=false"),
        SOURCE.replace("currency=", "currency=USD"),
        SOURCE.replace("&isSearch=true", ""),
        SOURCE + "&checkInDate=2026-10-16",
        SOURCE + "&accessToken=unknown",
        SOURCE + "&showFullPrice=true",
        SOURCE + "#calendar",
    ],
)
def test_batch_source_requires_observed_official_calendar_and_same_hotel(source):
    with pytest.raises(ValidationError, match="same official Marriott CN hotel"):
        CalendarHintBatch(**batch_data(source_url=source))


def test_observed_other_dates_calendar_link_remains_supported():
    batch = CalendarHintBatch(**batch_data(source_url=SOURCE + "&showFullPrice=false"))
    assert "showFullPrice=false" in str(batch.source_url)


@pytest.mark.parametrize(
    "month", [None, "2026-1", "26-10", "2026-00", "2026-13", "0000-01", "bad", "2026-10 "]
)
def test_month_window_rejects_invalid_month(month):
    with pytest.raises(ValueError, match="YYYY-MM"):
        month_window(month, date(2026, 10, 14))


def test_month_window_clips_current_and_last_month_not_the_entire_month_job():
    today = date(2026, 10, 14)
    assert month_window("2026-10", today) == (today, date(2026, 11, 1))
    assert month_window("2027-10", today) == (date(2027, 10, 1), date(2027, 10, 14))
    assert month_window("2026-09", today) is None
    assert month_window("2027-11", today) is None


def test_month_window_counts_exactly_365_checkins_across_thirteen_month_labels():
    today = date(2026, 10, 14)
    windows = [
        month_window(month, today)
        for month in ["2026-10", "2026-11", "2026-12", *(f"2027-{month:02}" for month in range(1, 11))]
    ]
    assert all(windows)
    assert sum((end - start).days for start, end in windows) == 365
    assert all(windows[index][1] == windows[index + 1][0] for index in range(len(windows) - 1))


def test_month_window_handles_leap_day_with_365_days_not_calendar_year():
    assert month_window("2028-02", date(2027, 3, 1)) == (date(2028, 2, 1), date(2028, 2, 29))
    assert month_window("2028-02", date(2027, 3, 2)) == (date(2028, 2, 1), date(2028, 3, 1))


def test_default_month_window_uses_builtin_shanghai_date_even_before_utc_midnight(monkeypatch):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 14, 16, 30, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(calendar_hints, "datetime", FixedDatetime)
    assert month_window("2026-10") == (date(2026, 10, 15), date(2026, 11, 1))
