"""Real observed member-price fixture plus explicitly synthetic contract variants."""

import base64
import copy
import json
import socket
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adapters import marriott
from adapters.base import BrandBlocked, RateLimited, SamplesRequired
from adapters.marriott import MarriottAdapter, MarriottSampleError, parse_prices
from models import CancellationPolicy, PriceRecord
from storage import Storage

FIXTURE = Path(__file__).parent / "fixtures" / "marriott_observed.json"
CI, CO = "2026-10-07", "2026-10-08"


@pytest.fixture
def body():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["rates"]


def connection(body):
    return body["data"]["commerce"]["product"]["searchProductsByProperty"]


def node(body):
    return connection(body)["edges"][0]["node"]


def parse(body):
    return parse_prices(body, "NYCMQ", CI, CO)


def envelope(body, **changes):
    # Synthetic envelope matches capture_marriott.py's output and the existing
    # observed request contract. It is not a newly captured live request.
    return {
        "url": marriott.ROOT + marriott.RATES_PATH,
        "method": "POST",
        "status": 200,
        "request_post_data": json.dumps(
            {
                "operationName": marriott.RATES_OPERATION,
                "variables": {
                    "search": {
                        "propertyId": "NYCMQ",
                        "options": {
                            "startDate": CI,
                            "endDate": CO,
                            "quantity": 1,
                            "numberInParty": 2,
                            "childAges": [],
                        },
                    }
                },
            }
        ),
        "response_text": json.dumps(body),
        **changes,
    }


def write_sample(tmp_path, value, filename="sample.json"):
    path = tmp_path / filename
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_real_observed_member_sample_money_and_unknown_conditions(body):
    (record,) = parse(body)
    assert record.member_rate is True
    assert record.amount_excluding_tax == Decimal("1152.00")
    assert record.amount_including_tax == Decimal("1377.06")
    assert record.average_nightly_excluding_tax == Decimal("1152.00")
    assert record.mandatory_fees == Decimal("45.00")
    assert record.currency == "USD" and record.price_basis == "stay_total"
    assert record.room_code == "dbdb" and record.rate_code == "AP0K"
    assert record.cancellation_policy == CancellationPolicy()
    assert record.tax_amount is None  # 225.06 is NOT a verified tax-only amount.
    assert record.adults is None and record.captured_at is None
    assert record.source_kind == "SAMPLE" and len(record.response_hash) == 64
    assert PriceRecord.from_dict(record.to_dict()) == record


def test_public_and_member_same_room_and_plan_are_separate(body):
    public = copy.deepcopy(node(body))
    public["basicInformation"]["isMembersOnly"] = False
    # Misleading names must not override explicit eligibility.
    public["rates"]["name"] = "Member wording is not membership evidence"
    connection(body)["edges"].append({"node": public})
    connection(body)["total"] = 2
    records = parse(body)
    assert len(records) == 2
    assert {r.member_rate for r in records} == {True, False}
    assert records[0].series_key != records[1].series_key
    assert records[0].record_id != records[1].record_id


def test_deadline_is_preserved_not_inferred_as_refundability(body):
    # Synthetic schema variant: the existing query selects this field, but the
    # real fixture does not contain it. No live policy claim is made here.
    deadline = "2026-10-05T23:59:00-04:00"
    node(body)["basicInformation"]["freeCancellationUntil"] = deadline
    node(body)["rates"]["description"] = "Additional rate terms"
    (record,) = parse(body)
    assert record.cancellation_policy.free_cancellation_until == deadline
    assert record.cancellation_policy.refundable is None
    assert record.cancellation_policy.policy_text is None
    assert record.rate_description == "Additional rate terms"
    assert replace(record, cancellation_policy=CancellationPolicy()).series_key != record.series_key


@pytest.mark.parametrize("currency,places", [("JPY", 0), ("KWD", 3), ("EUR", 2)])
def test_explicit_currency_and_decimal_exponent(body, currency, places):
    for wrapper in [
        *node(body)["totalPricing"]["rateModes"].values(),
        node(body)["rates"]["rateModes"]["averageNightlyRatePerUnit"],
    ]:
        wrapper["amount"].update(currency=currency, decimalPoint=places)
    (record,) = parse(body)
    assert record.currency == currency
    assert record.amount_excluding_tax == Decimal(115200).scaleb(-places)


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.update(errors=[{"message": "partial data"}]),
        lambda b: connection(b).update(total=2),
        lambda b: node(b)["basicInformation"].pop("isMembersOnly"),
        lambda b: node(b)["basicInformation"].update(isMembersOnly="false"),
        lambda b: node(b)["basicInformation"].update(type="OTHER"),
        lambda b: node(b)["basicInformation"]["ratePlan"].append({"ratePlanCode": "X"}),
        lambda b: node(b)["basicInformation"].update(freeCancellationUntil={"unexpected": 1}),
        lambda b: node(b)["totalPricing"].update(quantity=2),
        lambda b: node(b)["totalPricing"]["rateModes"]["grandTotal"]["amount"].update(currency="EUR"),
        lambda b: node(b)["totalPricing"]["rateModes"]["grandTotal"]["amount"].update(amount=1),
        lambda b: node(b)["totalPricing"]["rateModes"]["subtotalPerQuantity"]["amount"].update(amount=True),
        lambda b: node(b)["totalPricing"]["rateModes"]["subtotalPerQuantity"]["amount"].update(
            decimalPoint=-1
        ),
        lambda b: node(b)["totalPricing"]["rateModes"]["subtotalPerQuantity"]["amount"].update(
            currency="usd"
        ),
        lambda b: node(b)["rates"]["rateModes"].update(__typename="FutureUnknownRateMode"),
    ],
)
def test_incomplete_or_ambiguous_samples_are_not_prices(body, change):
    change(body)
    with pytest.raises(MarriottSampleError):
        parse(body)


@pytest.mark.parametrize(
    "hotel,ci,co",
    [("NYCXR", CI, CO), ("NYCMQ", "2026-10-08", "2026-10-09"), ("bad", CI, CO), ("NYCMQ", CI, CI)],
)
def test_identity_and_input_validation(body, hotel, ci, co):
    with pytest.raises(MarriottSampleError):
        parse_prices(body, hotel, ci, co)


def test_points_do_not_become_cash_and_true_empty_is_distinct(body):
    node(body)["rates"]["rateModes"]["__typename"] = "HotelRoomRateModesPoints"
    with pytest.raises(MarriottSampleError, match="no verified cash"):
        parse(body)
    connection(body).update(total=0, edges=[])
    assert parse(body) == []


def test_multinight_totals_are_not_mislabeled_nightly(body):
    # Synthetic two-night contract test, not a live two-night acceptance result.
    value = node(body)
    value["id"] = base64.b64encode(b"NYCMQ|AP0K|DBDB|2026-10-07|2026-10-09|synthetic").decode()
    value["totalPricing"]["rateModes"]["subtotalPerQuantity"]["amount"]["amount"] *= 2
    value["totalPricing"]["rateModes"]["grandTotal"]["amount"]["amount"] *= 2
    (record,) = parse_prices(body, "NYCMQ", CI, "2026-10-09")
    assert record.price_basis == "stay_total"
    assert record.amount_excluding_tax == Decimal("2304.00")
    assert record.average_nightly_excluding_tax == Decimal("1152.00")


async def test_fetch_prices_contract_from_real_sample_has_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Sample adapter must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    adapter = MarriottAdapter([FIXTURE])
    (record,) = await adapter.fetch_prices("nycmq", date(2026, 10, 7), date(2026, 10, 8))
    assert record.amount_including_tax == Decimal("1377.06")
    (again,) = await adapter.fetch_prices("NYCMQ", CI, CO)
    assert record == again and record.record_id == again.record_id
    await adapter.close()


async def test_module_function_and_capture_script_envelope(tmp_path, body, monkeypatch):
    captured = envelope(body, captured_at="2026-09-28T09:00:00Z")
    write_sample(tmp_path, captured)
    write_sample(
        tmp_path, {"url": "https://analytics.invalid/events", "response_text": "{}"}, "analytics.json"
    )
    monkeypatch.setattr(marriott, "DEFAULT_SAMPLE_DIR", tmp_path)
    (record,) = await marriott.fetch_prices("NYCMQ", CI, CO)
    assert record.adults == 2
    assert record.captured_at == datetime(2026, 9, 28, 9, tzinfo=timezone.utc)


async def test_reexport_deduplication_and_no_fake_availability(tmp_path, body):
    one = write_sample(tmp_path, envelope(body), "one.json")
    two = write_sample(tmp_path, envelope(body), "two.json")
    adapter = MarriottAdapter([one, two])
    assert len(await adapter.fetch_prices("NYCMQ", CI, CO)) == 1
    with pytest.raises(SamplesRequired):
        await adapter.fetch_prices("NYCXR", CI, CO)


@pytest.mark.parametrize(
    "status,error", [(403, BrandBlocked), (429, RateLimited), (500, MarriottSampleError)]
)
async def test_captured_denial_is_not_no_rooms(tmp_path, body, status, error):
    path = write_sample(tmp_path, envelope(body, status=status, response_text="Access denied"))
    with pytest.raises(error):
        await MarriottAdapter([path]).fetch_prices("NYCMQ", CI, CO)


async def test_empty_envelope_only_when_request_proves_identity(tmp_path, body):
    connection(body).update(total=0, edges=[])
    path = write_sample(tmp_path, envelope(body))
    assert await MarriottAdapter([path]).fetch_prices("NYCMQ", CI, CO) == []
    raw = write_sample(tmp_path, body, "raw.json")
    with pytest.raises(MarriottSampleError):
        await MarriottAdapter([raw]).fetch_prices("NYCMQ", CI, CO)


async def test_mixed_raw_identities_cannot_hide_an_incomplete_result(tmp_path, body):
    other = copy.deepcopy(node(body))
    other["id"] = base64.b64encode(b"NYCXR|AP0K|DBDB|2026-10-07|2026-10-08|synthetic").decode()
    connection(body)["edges"].append({"node": other})
    connection(body)["total"] = 2
    path = write_sample(tmp_path, body)
    with pytest.raises(MarriottSampleError, match="mixes"):
        await MarriottAdapter([path]).fetch_prices("NYCMQ", CI, CO)


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.update(request_post_data="not-json"),
        lambda value: value.update(response_text="not-json"),
        lambda value: value.update(captured_at="2026-09-28T09:00:00"),
        lambda value: value.update(method="GET"),
    ],
)
async def test_invalid_capture_envelopes_have_explicit_errors(tmp_path, body, change):
    value = envelope(body)
    change(value)
    path = write_sample(tmp_path, value)
    with pytest.raises(MarriottSampleError):
        await MarriottAdapter([path]).fetch_prices("NYCMQ", CI, CO)


def test_conflicting_duplicates_and_fee_currency_rejected(body):
    other = copy.deepcopy(node(body))
    other["totalPricing"]["rateModes"]["grandTotal"]["amount"]["amount"] += 1
    connection(body)["edges"].append({"node": other})
    connection(body)["total"] = 2
    with pytest.raises(MarriottSampleError, match="Conflicting"):
        parse(body)
    connection(body)["edges"].pop()
    connection(body)["total"] = 1
    node(body)["totalPricing"]["rateModes"]["totalMandatoryFeesPerQuantity"]["amount"]["currency"] = "EUR"
    with pytest.raises(MarriottSampleError, match="fee currency"):
        parse(body)


@pytest.mark.parametrize(
    "changes",
    [
        {"member_rate": None},
        {"amount_excluding_tax": None},
        {"amount_including_tax": None},
        {"currency": None},
        {"rooms": True},
        {"amount_including_tax": Decimal("NaN")},
        {"average_nightly_excluding_tax": Decimal("0")},
        {"source_kind": "LIVE"},
    ],
)
def test_model_cannot_turn_incomplete_sample_into_live_price(body, changes):
    (record,) = parse(body)
    with pytest.raises(ValueError):
        replace(record, **changes)


def test_public_member_storage_roundtrip_and_live_history_isolation(tmp_path, body):
    (member,) = parse(body)
    public = replace(member, member_rate=False)
    database = tmp_path / "sample.sqlite"
    storage = Storage(database)
    for record in [member, public, member]:
        storage.save_price_record(record)
    assert storage.price_records("marriott", "NYCMQ", CI, CO, member_rate=True) == [member]
    assert storage.price_records("marriott", "NYCMQ", CI, CO, member_rate=False) == [public]
    assert len(storage.price_records("marriott", "NYCMQ", CI, CO)) == 2
    assert storage.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    assert storage.db.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 0
    assert storage.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    with pytest.raises(TypeError):
        storage.save(member)
    storage.close()
    storage = Storage(database)
    assert storage.price_records("marriott", "NYCMQ", CI, CO, member_rate=True) == [member]
    storage.close()
