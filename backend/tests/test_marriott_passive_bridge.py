import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_passive_bridge import (
    MarriottPassiveBridgeProvider,
    collector_modules,
    normalize_live_records,
)
from app.schemas.domain import RateRequest


def live_records():
    ui, _ = collector_modules()
    fixture = (
        Path(__file__).resolve().parents[2] / "collector/tests/fixtures/marriott_cn_NYCTE_sanitized.json"
    )
    document = json.loads(fixture.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    records = [replace(r, source_kind="LIVE", captured_at=now) for r in ui.parse_capture(document)]
    sample = records[0]
    request = RateRequest(
        provider_hotel_id=sample.hotel_id,
        check_in=sample.checkin,
        check_out=sample.checkout,
        adults=sample.adults,
        rooms=1,
    )
    return records, request, now


def test_observed_prices_preserve_member_tax_currency_and_terms():
    records, request, now = live_records()
    rates = normalize_live_records(records, request, started_at=now)
    assert len(rates) == len(records) > 0
    for original, rate in zip(records, rates):
        assert rate.member_rate == original.member_rate
        assert rate.currency == original.currency
        assert rate.cash_price == original.amount_excluding_tax
        assert rate.total_price == original.amount_including_tax
        assert rate.tax is None or rate.cash_price + rate.tax == rate.total_price
        assert rate.raw_response_hash == original.response_hash
    public = next(r for r in rates if r.member_rate is False)
    member = replace(records[0], member_rate=True)
    assert normalize_live_records([member], request, started_at=now)[0].offer_key() != public.offer_key()
    changed = replace(records[0], deposit_required=not bool(records[0].deposit_required))
    assert normalize_live_records([changed], request, started_at=now)[0].offer_key() != rates[0].offer_key()


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_kind", "SAMPLE"),
        ("adults", 3),
        ("hotel_id", "ABCDE"),
        ("captured_at", datetime(2020, 1, 1, tzinfo=timezone.utc)),
    ],
)
def test_rejects_stale_or_mismatching_observations(field, value):
    records, request, now = live_records()
    with pytest.raises(ProviderError) as error:
        normalize_live_records([replace(records[0], **{field: value})], request, started_at=now)
    assert error.value.code == ErrorCode.INVALID_RESPONSE


async def test_bridge_requires_real_response_and_never_calls_hybrid_api():
    records, request, now = live_records()
    provider = MarriottPassiveBridgeProvider()
    provider.configure_hotel_url(
        f"https://www.marriott.com.cn/hotels/{request.provider_hotel_id.lower()}-test/overview/"
    )
    driver = SimpleNamespace(
        query=AsyncMock(
            return_value={
                "status": "ok",
                "hotel_id": request.provider_hotel_id,
                "checkin": str(request.check_in),
                "checkout": str(request.check_out),
                "adults": request.adults,
                "source_site": "www.marriott.com.cn",
                "offers": len(records),
                "response_hash": records[0].response_hash,
                "records": records,
            }
        )
    )
    provider._collector_driver = driver
    assert len(await provider.search_rates(request)) == len(records)
    assert driver.query.call_args.kwargs == {"include_records": True}


async def test_pause_on_denial_without_retry_or_host_switch():
    _, request, _ = live_records()
    _, errors = collector_modules()
    provider = MarriottPassiveBridgeProvider()
    provider.configure_hotel_url(
        f"https://www.marriott.com.cn/hotels/{request.provider_hotel_id.lower()}-test/overview/"
    )
    provider._collector_driver = SimpleNamespace(query=AsyncMock(side_effect=errors.BrandBlocked()))
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(request)
    assert error.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    assert provider._collector_driver.query.await_count == 1


async def test_cleanup_closes_only_query_owned_tabs():
    provider = MarriottPassiveBridgeProvider()
    page = SimpleNamespace(is_closed=lambda: False, close=AsyncMock())
    driver = SimpleNamespace(_owned=[page], close=AsyncMock())
    provider._collector_driver = driver
    await provider.close()
    page.close.assert_awaited_once()
    driver.close.assert_awaited_once()
