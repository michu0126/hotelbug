"""Actual official-response projection; negatives mutate the same captured pair."""

import copy
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.ihg_api import OFFERS_URL, fetch_once, parse_offers
from app.schemas.domain import RateRequest

FIXTURE = json.loads((Path(__file__).parent / "fixtures/ihg_api_observed.json").read_text(encoding="utf-8"))
SEARCH = RateRequest(provider_hotel_id="LONLS", check_in=date(2026, 11, 10), check_out=date(2026, 11, 11))


def parse(value=None):
    value = value or FIXTURE
    return parse_offers(value["response"], value["request"], SEARCH, "a" * 64)


def test_original_captured_pair_retains_public_member_tax_policy_and_unknowns():
    assert FIXTURE["provenance"]["original_offers"] == 205
    assert FIXTURE["provenance"]["projected_offers"] == 2
    assert (
        FIXTURE["provenance"]["raw_response_sha256"]
        == "547f8ebf61de15b868edd5134d9985a55a1744df39afb5143bd21fc859b732d8"
    )
    offers = parse()
    assert len(offers) == 2
    member = next(o for o in offers if o.rate.member_rate is True)
    public = next(o for o in offers if o.rate.member_rate is False)
    assert member.rate.rate_code == "IKME6" and public.rate.rate_code == "IGCOR"
    assert member.rate.currency == "GBP"
    assert (member.rate.cash_price, member.rate.tax, member.rate.total_price) == (
        Decimal("341.92"),
        Decimal("68.39"),
        Decimal("410.31"),
    )
    assert public.rate.total_price == Decimal("413.00")
    assert member.rate.refundable is True
    assert member.cancellation_deadline_local == "2026-11-09T16:00:00"
    assert "Canceling your reservation" in member.cancellation_description
    assert member.cancellation_timezone is None
    assert member.guarantee_type == "STANDARD_GUARANTEE"
    assert member.rate.breakfast_included is None
    assert member.restrictions["isPackage"] is True
    assert public.rate.offer_key() != member.rate.offer_key()


@pytest.mark.parametrize(
    "mutation",
    [
        lambda v: v["response"].update(startDate="2026-11-12"),
        lambda v: v["response"]["hotels"][0].update(hotelMnemonic="OTHER"),
        lambda v: v["request"]["products"][0]["guestCounts"][0].update(count=1),
        lambda v: v["response"]["hotels"][0]["rateDetails"]["offers"][0]["productUses"][0]["guestCounts"][
            0
        ].update(count=1),
        lambda v: v["response"]["hotels"][0]["rateDetails"]["offers"][0]["totalRate"].update(
            amountAfterTax="999"
        ),
        lambda v: v["response"]["hotels"][0]["rateDetails"]["offers"][0]["totalRate"].update(
            amountBeforeTax="NaN"
        ),
        lambda v: v["response"]["hotels"][0]["ratePlanDefinitions"][0].update(isLoyaltyIdRequired=None),
        lambda v: v["response"]["hotels"][0]["rateDetails"]["offers"].append(
            copy.deepcopy(v["response"]["hotels"][0]["rateDetails"]["offers"][0])
        ),
    ],
)
def test_rejects_mismatched_identity_context_or_amounts(mutation):
    value = copy.deepcopy(FIXTURE)
    mutation(value)
    with pytest.raises(ProviderError):
        parse(value)


def test_fee_differences_do_not_become_tax_and_missing_prices_not_zero():
    value = copy.deepcopy(FIXTURE)
    value["response"]["hotels"][0]["rateDetails"]["offers"][0]["totalRate"]["feeTaxSubTotals"][0][
        "otaCodeType"
    ] = "FEE"
    assert parse(value)[0].rate.tax is None
    value["response"]["hotels"][0]["rateDetails"]["offers"][0]["totalRate"].pop("amountAfterTax")
    with pytest.raises(ProviderError):
        parse(value)


@pytest.mark.parametrize(
    "status,code",
    [(401, ErrorCode.BLOCKED_BY_ANTIBOT), (403, ErrorCode.BLOCKED_BY_ANTIBOT), (429, ErrorCode.RATE_LIMITED)],
)
async def test_request_denial_has_one_call_no_retry_or_credentials_in_error(status, code):
    calls = []

    def handle(request):
        calls.append(request)
        assert request.url.host == "apis.ihg.com" and str(request.url).startswith(OFFERS_URL)
        assert "cookie" not in request.headers and "authorization" not in request.headers
        return httpx.Response(status, text="PRIVATE")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(ProviderError) as exc:
            await fetch_once(
                client, FIXTURE["request"], SEARCH, application_key="synthetic-public-client-key"
            )
    assert len(calls) == 1 and exc.value.code == code
    assert "PRIVATE" not in str(exc.value) and "synthetic-public-client-key" not in str(exc.value)


async def test_invalid_request_never_leaves_client():
    def reject(_):
        raise AssertionError("No network expected")

    body = copy.deepcopy(FIXTURE["request"])
    body["startDate"] = "2026-11-12"
    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(ProviderError):
            await fetch_once(client, body, SEARCH, application_key="synthetic-key")
