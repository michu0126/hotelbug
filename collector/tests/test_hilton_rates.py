import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.hilton_rates import parse_public_rates
from adapters.hilton_samples import SampleValidationError
from adapters.hilton_urls import rooms_url
from models import Hotel, Quote, Stay
from scripts.analyze_hilton_har import analyze

HOTEL = Hotel("hilton", "SHAQTWA", "上海前滩华尔道夫酒店", "https://www.hilton.com/")
STAY = Stay(date(2026, 11, 17), date(2026, 11, 18), adults=1)
NOW = datetime(2026, 10, 18, 15, tzinfo=timezone.utc)


@pytest.fixture
def full_rates():
    return json.loads(
        (Path(__file__).parent / "fixtures/hilton_full_rates_sanitized.json").read_text(encoding="utf-8")
    )


def parse(sample, **kwargs):
    return parse_public_rates(
        sample["variables"],
        sample["response"],
        HOTEL,
        STAY,
        observed_at=NOW,
        source_url=rooms_url(HOTEL.hotel_id, STAY),
        **kwargs,
    )


def test_real_plans_keep_public_prices_separate_from_nested_member_discount(full_rates):
    result = parse(full_rates)
    assert [quote.rate_code for quote in result.quotes] == ["LV0", "PR09AP", "PR09B1"]
    assert {row["rate_code"] for row in result.excluded} == {"HPPRP1", "HPPAP4"}
    first, prepaid, package = result.quotes
    assert first.amount == Decimal("3598.1") and first.after_tax_display == Decimal("3813.99")
    assert first.tax_basis == "TAX_EXCLUDED" and not first.membership_required
    assert not first.non_refundable and not first.deposit_required
    assert prepaid.amount == Decimal("3058.39") and prepaid.non_refundable and prepaid.deposit_required
    assert package.rate_type == "PUBLIC_PACKAGE"
    assert "10.00" in first.price_note and "<b>" not in first.price_note
    assert all(Quote.from_dict(quote.to_dict()) == quote for quote in result.quotes)


def test_cancellation_deposit_room_and_rate_codes_are_part_of_comparison_key(full_rates):
    quote = parse(full_rates).quotes[0]
    assert replace(quote, rate_name="Renamed", room_type="Translated").series_key == quote.series_key
    for changes in (
        {"non_refundable": True},
        {"deposit_required": True},
        {"cancellation_policy": "OTHER"},
        {"rate_code": "OTHER"},
        {"room_code": "OTHER"},
        {"membership_required": True},
    ):
        assert replace(quote, **changes).series_key != quote.series_key


def test_repeated_full_rate_har_exports_are_not_independent_observations(full_rates, tmp_path):
    entry = {
        "startedDateTime": "2026-10-18T14:13:00Z",
        "request": {
            "method": "POST",
            "url": "https://www.hilton.com/graphql/customer",
            "postData": {
                "text": json.dumps(
                    {
                        "operationName": "hotel_shopAvailOptions_shopPropAvail",
                        "variables": full_rates["variables"],
                    }
                )
            },
        },
        "response": {"status": 200, "content": {"text": json.dumps(full_rates["response"])}},
    }
    path = tmp_path / "rates.har"
    path.write_text(json.dumps({"log": {"entries": [entry]}}))
    report = analyze([path, path])
    assert len(report["public_rate_quotes"]) == 3 and report["duplicate_public_rates"] == 3
    assert all(len(row["sources"]) == 2 for row in report["public_rate_quotes"])
    assert report["monitoring_history_writes"] == report["notifications"] == 0


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_stay",
        "wrong_room",
        "missing_groups",
        "missing_member",
        "member",
        "restricted",
        "missing_policy",
        "missing_flag",
        "wrong_code",
        "duplicate",
        "past_deadline",
        "missing_description",
        "currency",
        "special",
    ],
)
def test_invalid_or_restricted_plan_is_not_silently_labeled_public(full_rates, mutation):
    room = full_rates["response"]["data"]["hotel"]["shopAvail"]["roomTypes"][0]
    rate = room["roomOnlyRates"][0]
    if mutation == "wrong_stay":
        full_rates["variables"]["numAdults"] = 2
    elif mutation == "wrong_room":
        full_rates["variables"]["selectedRoomTypeCode"] = "WRONG"
    elif mutation == "missing_groups":
        room.pop("roomOnlyRates")
    elif mutation == "missing_member":
        rate["ratePlan"].pop("hhonorsMembershipRequired")
    elif mutation == "member":
        rate["ratePlan"]["hhonorsMembershipRequired"] = True
    elif mutation == "restricted":
        rate["ratePlan"]["confidentialRates"] = True
    elif mutation == "missing_policy":
        rate["guarantee"].pop("cxlPolicyCode")
    elif mutation == "missing_flag":
        rate["guarantee"].pop("nonRefundable")
    elif mutation == "wrong_code":
        rate["ratePlan"]["ratePlanCode"] = "OTHER"
    elif mutation == "duplicate":
        room["roomOnlyRates"].append(deepcopy(rate))
    elif mutation == "past_deadline":
        rate["guarantee"]["isAfterCxlDeadline"] = True
    elif mutation == "missing_description":
        rate["ratePlan"].pop("ratePlanDesc")
    elif mutation == "currency":
        full_rates["response"]["data"]["hotel"]["shopAvail"]["currencyCode"] = "USD"
    else:
        full_rates["variables"]["specialRates"]["aaa"] = True
    if mutation in ("missing_member", "member", "restricted", "past_deadline"):
        assert "LV0" not in {quote.rate_code for quote in parse(full_rates).quotes}
    else:
        with pytest.raises(SampleValidationError):
            parse(full_rates)
