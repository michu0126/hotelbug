import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.gha_api import GHABookingIdentity, GHAJsonClient, build_payload, parse_rates
from app.schemas.domain import RateRequest

# Exact two-rate subset from auxiliary Playwright quote-20ff85b1ad902de0.json,
# 2026-10-23, public hotel 10624 (Avani Sukhumvit), 2026-11-09/10, 1 room/2 adults.
# Full sanitized serialized JSON SHA256 (before Windows CRLF conversion):
# 20ff85b1ad902de066d6afba600b06966cb5a0a1c8241f8a18cfc6fcb65a47e3
IDENTITY = GHABookingIdentity(
    public_hotel_id="10624",
    hotel_code="AVVSUK",
    brand_code="AV",
    chain_id="18336",
    reservation_hotel_id="8188",
)
REQUEST = RateRequest(provider_hotel_id="10624", check_in=date(2026, 11, 9), check_out=date(2026, 11, 10))
BODY = json.loads((Path(__file__).parent / "fixtures/gha_api_quote.json").read_text(encoding="utf-8"))


def test_real_sample_preserves_member_public_tax_and_unknown_policy():
    member, public = parse_rates(BODY, IDENTITY, REQUEST)
    assert member.member_rate is True and public.member_rate is False
    assert member.total_price == Decimal("5910.89")
    assert public.total_price == Decimal("6567.66")
    assert public.cash_price == Decimal("5580.0")
    assert public.tax is None and public.refundable is None and public.breakfast_included is None
    assert public.currency == "THB" and public.room_code == "1H6XK"
    assert public.rate_code == "S20APDRE" and public.price_basis == "stay_total"
    assert member.offer_key() != public.offer_key()


def test_booking_ids_are_not_interchangeable():
    payload = build_payload(IDENTITY, REQUEST)
    assert payload["hotelId"] == "8188" and payload["hotelCode"] == "AVVSUK"
    assert payload["numberOfAdults"] == 2 and payload["startDate"] == "2026-11-09"
    with pytest.raises(ProviderError):
        build_payload(IDENTITY, REQUEST.model_copy(update={"provider_hotel_id": "8188"}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("memberRate", "false"),
        ("taxInclusive", None),
        ("currency", "thb"),
        ("amountWithTaxesFees", "NaN"),
        ("amountWithTaxesFees", 0),
        ("amountWithTaxesFees", 100),
        ("rateCode", ""),
    ],
)
def test_ambiguous_quote_rejected(field, value):
    body = copy.deepcopy(BODY)
    body["rooms"][0]["rates"][0][field] = value
    with pytest.raises(ProviderError):
        parse_rates(body, IDENTITY, REQUEST)


def test_missing_inclusive_total_not_guessed_from_daily_price():
    body = copy.deepcopy(BODY)
    body["rooms"][0]["rates"][0].pop("amountWithTaxesFees")
    with pytest.raises(ProviderError):
        parse_rates(body, IDENTITY, REQUEST)


def test_no_room_response_does_not_create_zero_quote():
    assert parse_rates({"rooms": []}, IDENTITY, REQUEST) == []
    with pytest.raises(ProviderError):
        parse_rates({"errors": [{"message": "failure"}]}, IDENTITY, REQUEST)


@pytest.mark.asyncio
async def test_http_sends_one_observed_payload_without_credentials():
    calls = []

    def handle(request):
        calls.append(request)
        assert "authorization" not in request.headers and "cookie" not in request.headers
        assert json.loads(request.content) == build_payload(IDENTITY, REQUEST)
        return httpx.Response(200, json=BODY)

    client = GHAJsonClient(transport=httpx.MockTransport(handle))
    assert len(await client.fetch_prices(IDENTITY, REQUEST)) == 2
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [(401, ErrorCode.BLOCKED_BY_ANTIBOT), (403, ErrorCode.BLOCKED_BY_ANTIBOT), (429, ErrorCode.RATE_LIMITED)],
)
async def test_rejection_pauses_and_no_second_request(status, code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, json={"error": "access rejected"})

    client = GHAJsonClient(transport=httpx.MockTransport(handle))
    with pytest.raises(ProviderError) as first:
        await client.fetch_prices(IDENTITY, REQUEST)
    assert first.value.code == code and client.paused
    with pytest.raises(ProviderError):
        await client.fetch_prices(IDENTITY, REQUEST)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_html_does_not_become_prices_and_pauses():
    client = GHAJsonClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="Verification required"))
    )
    with pytest.raises(ProviderError):
        await client.fetch_prices(IDENTITY, REQUEST)
    assert client.paused
