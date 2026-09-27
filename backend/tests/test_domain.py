import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.domain import RateData, RateRequest
from app.services.jobs import backoff, tier


def fixture_rate() -> RateData:
    return RateData.model_validate_json((Path(__file__).parent / "fixtures/rate.json").read_text())


def test_normalizer_decimal_currency_and_offer_identity():
    rate = fixture_rate()
    assert rate.total_price == Decimal("110.11")
    changed = rate.model_copy(update={"cash_price": Decimal("90.10"), "total_price": Decimal("100.11")})
    assert changed.offer_key() == rate.offer_key()
    for key, value in [
        ("currency", "EUR"),
        ("rate_code", "MEMBER"),
        ("room_code", "SUITE"),
        ("member_rate", True),
        ("refundable", None),
        ("adults", 1),
        ("price_basis", "nightly"),
    ]:
        assert rate.model_copy(update={key: value}).offer_key() != rate.offer_key()


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "$"},
        {"cash_price": "-1"},
        {"total_price": "999"},
        {"captured_at": "2026-09-28T00:00:00"},
        {"check_out": "2027-01-06"},
    ],
)
def test_reject_invalid_quote(changes):
    data = json.loads(fixture_rate().model_dump_json())
    data.update(changes)
    with pytest.raises(ValidationError):
        RateData.model_validate(data)


def test_points_not_cash():
    data = fixture_rate().model_dump()
    data.update(cash_price=None, tax=None, total_price=None, points_price=20000)
    result = RateData.model_validate(data)
    assert result.cash_price is None and result.points_price == 20000


def test_dates_and_tiers():
    RateRequest(provider_hotel_id="x", check_in="2026-12-31", check_out="2027-01-01")
    assert [tier(n) for n in (0, 30, 31, 90, 91, 365)] == ["HOT", "HOT", "WARM", "WARM", "COLD", "COLD"]
    with pytest.raises(ValueError):
        tier(366)
    assert backoff(3, 2000) >= 2000
