import base64
import json
from copy import deepcopy
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest

from adapters.hilton_samples import SampleValidationError, parse_room_search
from scripts.analyze_hilton_har import analyze


@pytest.fixture
def sample():
    return json.loads(
        (Path(__file__).parent / "fixtures/hilton_room_card_sanitized.json").read_text(encoding="utf-8")
    )


def test_actual_room_minimum_schema_retains_dates_currency_tax_and_unknown_membership(sample):
    rows = parse_room_search(sample["variables"], sample["response"], expected_hotel_id="SHAQTWA")
    assert len(rows) == 2
    row = rows[0]
    assert row.hotel_id == "SHAQTWA" and row.requested_hotel_id == "SHAQT"
    assert row.check_in.isoformat() == "2026-11-11" and row.adults == row.rooms == 1
    assert row.currency == "CNY" and row.rate_amount == Decimal("2391.54")
    assert row.after_tax_display == Decimal("2535.03")
    assert row.membership_required is None and not row.eligible_for_alerts
    assert not row.service_charges_and_taxes_included
    assert not hasattr(row, "series_key")  # Cannot accidentally persist as a detector Quote.
    assert row.to_dict()["rate_type_verified"] is False


@pytest.mark.parametrize(
    "mutation",
    [
        "hotel",
        "unknown_alias",
        "date",
        "nights",
        "adults",
        "children",
        "rooms",
        "currency",
        "status",
        "empty",
        "duplicate",
        "base_price",
        "formatted",
        "tax_flag",
        "membership",
        "room_code",
        "errors",
    ],
)
def test_incomplete_mismatched_or_ambiguous_price_sample_is_not_accepted(sample, mutation):
    variables, response = sample["variables"], sample["response"]
    avail = response["data"]["hotel"]["shopAvail"]
    rate = avail["roomTypes"][0]["moreRatesFromRate"]
    if mutation == "hotel":
        response["data"]["hotel"]["ctyhocn"] = "NYCWAWA"
    elif mutation == "unknown_alias":
        variables["ctyhocn"] = "NYCWA"
    elif mutation == "date":
        variables.pop("arrivalDate")
    elif mutation == "nights":
        variables["departureDate"] = "2026-11-13"
    elif mutation in ("adults", "children", "rooms"):
        variables[{"adults": "numAdults", "children": "numChildren", "rooms": "numRooms"}[mutation]] = {
            "adults": True,
            "children": 1,
            "rooms": 2,
        }[mutation]
    elif mutation == "currency":
        avail["currencyCode"] = "JPY"
    elif mutation == "status":
        avail["statusCode"] = 1
    elif mutation == "empty":
        avail["roomTypes"] = []
    elif mutation == "duplicate":
        avail["roomTypes"].append(deepcopy(avail["roomTypes"][0]))
    elif mutation == "base_price":
        rate["rateAmount"] = 0
    elif mutation == "formatted":
        rate["fullAmountAfterTax"] = "¥2,500-3,000"
    elif mutation == "tax_flag":
        rate["ratePlan"].pop("serviceChargesAndTaxesIncluded")
    elif mutation == "membership":
        rate["ratePlan"]["hhonorsMembershipRequired"] = "false"
    elif mutation == "room_code":
        rate["roomTypeCode"] = "WRONG"
    else:
        response["errors"] = [{"message": "partial data"}]
    with pytest.raises(SampleValidationError):
        parse_room_search(variables, response)


def test_second_observed_date_changes_price_without_assuming_calendar_bulk_support(sample):
    variables, response = sample["variables"], sample["response"]
    variables.update(arrivalDate="2026-11-17", departureDate="2026-11-18")
    rate = response["data"]["hotel"]["shopAvail"]["roomTypes"][0]["moreRatesFromRate"]
    rate.update(
        rateAmount=2500.1, rateAmountFmt="¥2,501", amountAfterTaxFmt="¥2,651", fullAmountAfterTax="¥2,650.11"
    )
    row = parse_room_search(variables, response)[0]
    assert row.rate_amount == Decimal("2500.1") and row.after_tax_display == Decimal("2650.11")
    assert row.check_in.isoformat() == "2026-11-17" and not row.eligible_for_alerts


def har_entry(sample):
    return {
        "startedDateTime": "2026-10-18T14:00:00Z",
        "request": {
            "url": "https://www.hilton.com/graphql/customer?operationName=example",
            "method": "POST",
            "headers": [{"name": "Authorization", "value": "do-not-copy"}],
            "postData": {
                "text": json.dumps(
                    {
                        "operationName": "hotel_shopAvailOptions_shopPropAvail",
                        "variables": {
                            **sample["variables"],
                            "guestId": "do-not-copy",
                            "cacheId": "do-not-copy",
                        },
                    }
                )
            },
        },
        "response": {"status": 200, "content": {"text": json.dumps(sample["response"])}},
    }


def test_har_analyzer_deduplicates_reused_capture_and_drops_credentials(sample, tmp_path):
    entry = har_entry(sample)
    first, second = tmp_path / "one.har", tmp_path / "two.har"
    first.write_text(json.dumps({"log": {"entries": [entry]}}))
    duplicate = deepcopy(entry)
    duplicate["response"]["content"] = {
        "encoding": "base64",
        "text": base64.b64encode(json.dumps(sample["response"]).encode()).decode(),
    }
    second.write_text(json.dumps({"log": {"entries": [duplicate]}}))
    report = analyze([first, second])
    assert len(report["observations"]) == report["duplicate_observations"] == 2
    assert len(report["observations"][0]["sources"]) == 2
    assert "do-not-copy" not in json.dumps(report) and "guestId" not in json.dumps(report)
    assert report["network_requests"] == report["monitoring_history_writes"] == report["notifications"] == 0
    assert not report["rejected"]


def test_untrusted_hosts_and_failed_requests_are_not_imported(sample, tmp_path):
    failed = har_entry(sample)
    failed["response"]["status"] = 403
    evil = har_entry(sample)
    evil["request"]["url"] = "https://www.hilton.com.evil.invalid/graphql/customer"
    path = tmp_path / "bad.har"
    path.write_text(json.dumps({"log": {"entries": [failed, evil]}}))
    report = analyze([path])
    assert not report["observations"] and len(report["rejected"]) == 1


def test_request_personal_fields_never_enter_observation(sample):
    sample["variables"].update(guestId="private", cacheId="private", pnd="private")
    row = parse_room_search(sample["variables"], sample["response"])[0]
    assert "private" not in json.dumps(row.to_dict())
    assert "guestId" not in asdict(row)


@pytest.mark.parametrize("membership", [None, False, True])
def test_even_a_membership_flag_does_not_turn_a_minimum_card_into_a_complete_offer(sample, membership):
    for room in sample["response"]["data"]["hotel"]["shopAvail"]["roomTypes"]:
        room["moreRatesFromRate"]["ratePlan"]["hhonorsMembershipRequired"] = membership
    assert all(
        not row.eligible_for_alerts for row in parse_room_search(sample["variables"], sample["response"])
    )


@pytest.mark.parametrize("value", [None, [], "invalid"])
def test_non_object_payloads_are_explicit_validation_failures(sample, value):
    with pytest.raises(SampleValidationError):
        parse_room_search(value, sample["response"])
    with pytest.raises(SampleValidationError):
        parse_room_search(sample["variables"], value)
