"""Real sanitized .cn capture, optional selected-checkout evidence, no raw HAR."""

import json
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from adapters import marriott
from adapters.marriott import MarriottAdapter, MarriottSampleError
from adapters.marriott_checkout import apply_checkout_evidence
from models import CancellationPolicy, PriceRecord

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "marriott_cn_NYCTE_sanitized.json"
CHECKOUT = FIXTURES / "marriott_cn_checkout_selected_sanitized.json"


async def test_read_real_passive_capture_and_preserve_source_and_membership(monkeypatch):
    adapter = MarriottAdapter([SAMPLE])
    records = await adapter.fetch_prices("NYCTE", "2026-10-18", "2026-10-19")
    assert len(records) == 67
    assert sum(r.member_rate for r in records) == 22 and sum(not r.member_rate for r in records) == 45
    assert all(r.source_site == "www.marriott.com.cn" and r.adults == 1 for r in records)
    assert all(r.source_kind == "SAMPLE" and r.captured_at is not None for r in records)
    assert all(r.tax_amount is None and r.free_cancel_now() is None for r in records)
    record = records[0]
    assert (
        record.series_key
        != replace(record, source_url="https://www.marriott.com" + marriott.RATES_PATH).series_key
    )


async def test_default_capture_directory_and_selected_checkout_only(monkeypatch, tmp_path):
    monkeypatch.setattr(marriott, "DEFAULT_CAPTURE_DIR", SAMPLE.parent)
    monkeypatch.setattr(marriott, "DEFAULT_SAMPLE_DIR", tmp_path)
    # Explicit paths avoid treating unrelated fixture JSON as capture files.
    adapter = MarriottAdapter([SAMPLE], checkout_paths=[CHECKOUT])
    records = await adapter.fetch_prices("NYCTE", "2026-10-18", "2026-10-19")
    enriched = [r for r in records if r.tax_amount is not None]
    assert len(enriched) == 1
    record = enriched[0]
    assert record.room_code == "genr" and record.rate_code == "RMOC" and record.member_rate
    assert (
        record.amount_excluding_tax,
        record.mandatory_fees,
        record.tax_amount,
        record.amount_including_tax,
    ) == (Decimal("417.00"), Decimal("45.00"), Decimal("71.64"), Decimal("533.64"))
    assert record.cancellation_policy.non_refundable is False
    assert record.cancellation_policy.deadline_at == datetime(2026, 10, 16, 3, 59, tzinfo=timezone.utc)
    assert record.free_cancel_now(datetime(2026, 10, 15, 12, tzinfo=timezone.utc)) is True
    assert record.free_cancel_now(record.captured_at) is False
    assert PriceRecord.from_dict(record.to_dict()) == record
    assert all(r.tax_amount is None for r in records if r is not record)


async def test_module_function_reads_captures_without_raw_har(monkeypatch, tmp_path):
    captures = tmp_path / "captures"
    captures.mkdir()
    (captures / "capture.json").write_bytes(SAMPLE.read_bytes())
    monkeypatch.setattr(marriott, "DEFAULT_CAPTURE_DIR", captures)
    monkeypatch.setattr(marriott, "DEFAULT_SAMPLE_DIR", tmp_path / "no-samples")
    assert len(await marriott.fetch_prices("NYCTE", "2026-10-18", "2026-10-19")) == 67


@pytest.mark.parametrize(
    "change",
    [
        lambda d: d.update(source_site="evilmarriott.com"),
        lambda d: d.update(property_id="OTHER"),
        lambda d: d["variables"].update(offset=50),
        lambda d: d["response"]["data"]["commerce"]["product"]["searchProductsByProperty"].update(total=76),
        lambda d: d.update(captured_at="2026-10-18T16:22:20"),
    ],
)
async def test_mismatched_or_partial_capture_is_not_complete_availability(tmp_path, change):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    change(sample)
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(sample), encoding="utf-8")
    with pytest.raises(MarriottSampleError):
        await MarriottAdapter([path]).fetch_prices("NYCTE", "2026-10-18", "2026-10-19")


async def test_checkout_scope_and_consistency():
    records = await MarriottAdapter([SAMPLE]).fetch_prices("NYCTE", "2026-10-18", "2026-10-19")
    record = next(r for r in records if r.rate_code == "RMOC" and r.room_code == "genr" and r.member_rate)
    evidence = json.loads(CHECKOUT.read_text(encoding="utf-8"))
    wrong_member = replace(record, member_rate=False)
    assert apply_checkout_evidence(wrong_member, evidence) is wrong_member
    wrong_source = replace(record, source_url="https://www.marriott.com" + marriott.RATES_PATH)
    assert apply_checkout_evidence(wrong_source, evidence) is wrong_source
    another_snapshot = replace(record, response_hash="a" * 64)
    assert apply_checkout_evidence(another_snapshot, evidence) is another_snapshot
    evidence["totals"]["taxes"] = "99.00"
    with pytest.raises(MarriottSampleError):
        apply_checkout_evidence(record, evidence)


def test_raw_date_or_marketing_name_never_proves_free_cancellation():
    policy = CancellationPolicy(free_cancellation_until="2026-10-15T00:00:00Z")
    assert policy.free_cancel_now(datetime(2026, 10, 14, tzinfo=timezone.utc)) is None
    assert CancellationPolicy(non_refundable=True).free_cancel_now() is False
    deadline = datetime(2026, 10, 16, 3, 59, tzinfo=timezone.utc)
    assert CancellationPolicy(non_refundable=False, deadline_at=deadline).free_cancel_now(deadline) is False
