import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott import MarriottProvider, parse_hotel, parse_rates
from app.schemas.domain import RateRequest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "marriott_observed.json").read_text())
SEARCH = RateRequest(provider_hotel_id="NYCMQ", check_in=date(2026, 10, 7), check_out=date(2026, 10, 8))
DIGEST = "a" * 64


def test_observed_marriott_cash_rate_and_hotel():
    hotel = parse_hotel(FIXTURE["hotel"], "NYCMQ")
    rates = parse_rates(FIXTURE["rates"], SEARCH, DIGEST)
    assert hotel.hotel_name == "New York Marriott Marquis"
    assert hotel.city == "New York"
    assert str(hotel.official_url).endswith("nycmq-new-york-marriott-marquis")
    assert len(rates) == 1
    rate = rates[0]
    assert (rate.cash_price, rate.total_price, rate.tax) == (Decimal("1152.00"), Decimal("1377.06"), None)
    assert (rate.room_code, rate.rate_code, rate.member_rate) == ("dbdb", "AP0K", True)
    assert rate.points_price is None


def test_rejects_mismatched_or_incomplete_marriott_response():
    body = copy.deepcopy(FIXTURE["rates"])
    node = body["data"]["commerce"]["product"]["searchProductsByProperty"]
    node["total"] = 2
    with pytest.raises(ProviderError) as exc:
        parse_rates(body, SEARCH, DIGEST)
    assert exc.value.code == ErrorCode.INVALID_RESPONSE
    node["total"] = 1
    node["edges"][0]["node"]["totalPricing"]["rateModes"]["grandTotal"]["amount"]["currency"] = "EUR"
    with pytest.raises(ProviderError) as exc:
        parse_rates(body, SEARCH, DIGEST)
    assert exc.value.code == ErrorCode.INVALID_RESPONSE


@pytest.mark.asyncio
async def test_observed_marriott_operation_and_block_policy():
    seen = []

    def success(request):
        seen.append(request)
        return httpx.Response(200, json=FIXTURE["rates"])

    provider = MarriottProvider(httpx.AsyncClient(transport=httpx.MockTransport(success)))
    rates = await provider.search_rates(SEARCH)
    await provider.close()
    sent = seen[0]
    assert sent.url.path == "/mi/query/PhoenixBookDTTSearchProductsByProperty"
    assert sent.headers["graphql-operation-name"] == "PhoenixBookDTTSearchProductsByProperty"
    assert json.loads(sent.content)["variables"]["search"]["options"]["startDate"] == "2026-10-07"
    assert len(rates[0].raw_response_hash) == 64

    blocked = MarriottProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    )
    with pytest.raises(ProviderError) as exc:
        await blocked.search_rates(SEARCH)
    assert exc.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    await blocked.close()
