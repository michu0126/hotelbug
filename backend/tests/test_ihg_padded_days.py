from datetime import date, timedelta

import pytest

from app.core.errors import ProviderError
from app.providers.ihg_page import verify_booking
from app.schemas.domain import RateRequest


def booking(request, day_in=None, day_out=None):
    return "https://www.ihg.com/holidayinn/hotels/us/en/find-hotels/select-roomrate?" + (
        f"qSlH={request.provider_hotel_id}&qRms=1&qAdlt=2&qChld=0&"
        f"qCiD={day_in if day_in is not None else request.check_in.strftime('%d')}&"
        f"qCiMy={request.check_in.month - 1:02d}{request.check_in.year}&"
        f"qCoD={day_out if day_out is not None else request.check_out.strftime('%d')}&"
        f"qCoMy={request.check_out.month - 1:02d}{request.check_out.year}&qRtP=6CBARC"
    )


@pytest.mark.parametrize("day", range(1, 10))
def test_official_zero_padded_early_month_days_are_same_stay(day):
    check_in = date(2026, 10, day)
    request = RateRequest(
        provider_hotel_id="YQUAB", check_in=check_in, check_out=check_in + timedelta(days=1)
    )
    verify_booking(booking(request), request)
    verify_booking(booking(request, str(day), str(day + 1)), request)


def test_padded_checkout_after_month_and_year_rollover():
    request = RateRequest(provider_hotel_id="ALGCT", check_in=date(2026, 12, 31), check_out=date(2027, 1, 1))
    verify_booking(booking(request), request)


@pytest.mark.parametrize(
    "value", ["0", "00", "005", "5.0", "5x", "%2B5", "%205", "-5", "", "06", "05&qCiD=05"]
)
def test_padding_does_not_allow_invalid_wrong_or_duplicate_days(value):
    request = RateRequest(provider_hotel_id="YQUAB", check_in=date(2026, 10, 5), check_out=date(2026, 10, 6))
    with pytest.raises(ProviderError):
        verify_booking(booking(request, value), request)


def test_error_diagnostics_do_not_disclose_unrelated_query_or_non_numeric_values():
    request = RateRequest(provider_hotel_id="YQUAB", check_in=date(2026, 10, 5), check_out=date(2026, 10, 6))
    url = booking(request, "private-value") + "&authToken=secret-token&cookie=secret-cookie"
    with pytest.raises(ProviderError) as caught:
        verify_booking(url, request)
    message = str(caught.value)
    assert "qCiD" in message
    assert not any(value in message for value in ("private-value", "secret-token", "secret-cookie"))
