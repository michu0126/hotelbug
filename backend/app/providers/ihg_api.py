"""Observed IHG availability-v3 contract, captured from the official page.

Experimental, not registered in production. A website-supplied application key
is not an account credential and is deliberately not bundled here. Only a
matching actual request/response pair is normalized; no guessed rate codes.
"""

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import RateData, RateRequest

OFFERS_URL = "https://apis.ihg.com/availability/v3/hotels/offers"
OBSERVED_FIELDSET = "rateDetails,rateDetails.policies,rateDetails.bonusRates,rateDetails.upsells,rateDetails.packagesAdditionalInfo"


def invalid(message):
    return ProviderError(ErrorCode.INVALID_RESPONSE, "IHG API: " + message)


def positive_amount(value):
    try:
        if isinstance(value, bool):
            raise ValueError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0 or amount > Decimal("10000000000"):
            raise ValueError
        return amount
    except (ValueError, InvalidOperation):
        raise invalid("invalid monetary amount") from None


def guest_identity(product, search, *, response=False):
    guests = product["guestCounts"]
    if not isinstance(guests, list) or any(type(g.get("count")) is not int or g["count"] < 0 for g in guests):
        raise invalid("invalid guest counts")
    counts = {g["otaCode"]: g["count"] for g in guests}
    if len(counts) != len(guests) or counts.get("AQC10") != search.adults:
        raise invalid("adult count mismatch")
    if any(count for code, count in counts.items() if code != "AQC10"):
        raise invalid("children or unknown occupancy not supported")
    period = product.get("period") if response else {"start": product["startDate"], "end": product["endDate"]}
    if product["quantity"] != search.rooms or period != {
        "start": str(search.check_in),
        "end": str(search.check_out),
    }:
        raise invalid("product dates or room quantity mismatch")


def validate_request(request_body, search):
    if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "IHG API only verified for one room and one night")
    if (request_body["startDate"], request_body["endDate"]) != (str(search.check_in), str(search.check_out)):
        raise invalid("request dates mismatch")
    if request_body["hotelMnemonics"] != [search.provider_hotel_id]:
        raise invalid("request hotel mismatch")
    products = request_body["products"]
    if len(products) != 1 or products[0]["productCode"] != "SR":
        raise invalid("unsupported product request")
    guest_identity(products[0], search)


@dataclass(frozen=True)
class IhgObservedOffer:
    rate: RateData
    cancellation_description: str | None
    cancellation_deadline_local: str | None
    cancellation_timezone: None
    guarantee_type: str | None
    restrictions: dict


def parse_offers(body, request_body, search: RateRequest, response_hash):
    """Includes room/rate identity and unknown policy fields, not a list minimum.

    Local cancellation deadlines are retained without fabricating a timezone or
    calculating free_cancel_now. Includes package prices as their own plans.
    """
    try:
        validate_request(request_body, search)
        expected = (str(search.check_in), str(search.check_out))
        if (request_body["startDate"], request_body["endDate"]) != expected or (
            body["startDate"],
            body["endDate"],
        ) != expected:
            raise invalid("response or request dates mismatch")
        if request_body["hotelMnemonics"] != [search.provider_hotel_id]:
            raise invalid("request hotel mismatch")
        requested_products = request_body["products"]
        if len(requested_products) != 1 or requested_products[0]["productCode"] != "SR":
            raise invalid("unsupported product request")
        guest_identity(requested_products[0], search)
        hotels = body["hotels"]
        if len(hotels) != 1 or hotels[0]["hotelMnemonic"] != search.provider_hotel_id:
            raise invalid("response hotel mismatch")
        hotel = hotels[0]
        definitions, products = hotel["ratePlanDefinitions"], hotel["productDefinitions"]
        plans = {p["code"]: p for p in definitions}
        rooms = {p["inventoryTypeCode"]: p for p in products}
        if len(plans) != len(definitions) or len(rooms) != len(products):
            raise invalid("duplicate plan or room definitions")
        currency = hotel["propertyCurrency"]
        results, identities = [], set()
        for offer in hotel["rateDetails"]["offers"]:
            if offer.get("availableStatus") != "AVAILABLE":
                continue
            main_products = [p for p in offer["productUses"] if p.get("isMainProduct") is True]
            if len(main_products) != 1:
                raise invalid("ambiguous main room")
            product = main_products[0]
            guest_identity(product, search, response=True)
            room_code, plan_code = product["inventoryTypeCode"], offer["ratePlanCode"]
            room, plan = rooms[room_code], plans[plan_code]
            if type(plan.get("isLoyaltyIdRequired")) is not bool:
                raise invalid("membership requirement unknown")
            identity = (room_code, plan_code)
            if identity in identities:
                raise invalid("duplicate offer identity")
            identities.add(identity)
            pricing = offer["totalRate"]
            cash = positive_amount(pricing["amountBeforeTax"])
            total = positive_amount(pricing["amountAfterTax"])
            fees_tax = positive_amount(pricing["feeTaxTotal"])
            if total <= 0 or total < cash or abs(total - cash - fees_tax) > Decimal("0.05"):
                raise invalid("inconsistent offer total")
            # FeeTaxTotal may combine fees and taxes. Tax is explicit only when
            # every subtotal identifies TAX; do not label fee differences taxes.
            subtotals = pricing.get("feeTaxSubTotals", [])
            tax = fees_tax if subtotals and all(v.get("otaCodeType") == "TAX" for v in subtotals) else None
            policies = offer.get("policies", {})
            refundable = policies.get("isRefundable")
            if refundable is not None and type(refundable) is not bool:
                raise invalid("invalid refundable flag")
            cancellation = policies.get("cancellationNoShow") or {}
            descriptions = plan.get("additionalDescriptions") or {}
            rate = RateData(
                **search.model_dump(),
                provider="ihg",
                room_type=room["inventoryTypeName"],
                room_code=room_code,
                rate_code=plan_code,
                rate_name=descriptions.get("longRateName") or plan["name"],
                cash_price=cash,
                total_price=total,
                tax=tax,
                currency=currency,
                member_rate=plan["isLoyaltyIdRequired"],
                refundable=refundable,
                breakfast_included=None,
                availability=True,
                price_basis="stay_total",
                source_url=OFFERS_URL,
                raw_response_hash=response_hash,
            )
            results.append(
                IhgObservedOffer(
                    rate=rate,
                    cancellation_description=cancellation.get("description"),
                    cancellation_deadline_local=(cancellation.get("deadline") or {}).get("dateTime"),
                    cancellation_timezone=None,
                    guarantee_type=(policies.get("guarantee") or {}).get("type"),
                    restrictions={
                        key: plan.get(key)
                        for key in (
                            "isGroupRatePlan",
                            "isPackage",
                            "isVoucherRequired",
                            "isIdRequired",
                            "areAmountsConfidential",
                            "isTentative",
                        )
                    },
                )
            )
        return results
    except ProviderError:
        raise
    except (KeyError, ValueError, TypeError, AttributeError):
        raise invalid("unrecognized response schema") from None


async def fetch_once(client: httpx.AsyncClient, request_body, search, *, application_key):
    """One observed query. Caller owns throttling/pause state; never retries."""
    # Validate identity/occupancy before emitting any request.
    try:
        validate_request(request_body, search)
    except (KeyError, TypeError, AttributeError):
        raise invalid("unrecognized request schema") from None
    if (
        not isinstance(application_key, str)
        or not application_key
        or not application_key.isascii()
        or any(ord(c) < 32 or ord(c) == 127 for c in application_key)
    ):
        raise invalid("website application key required")
    try:
        response = await client.post(
            OFFERS_URL,
            params={"fieldset": OBSERVED_FIELDSET},
            json=request_body,
            headers={"Content-Type": "application/json", "X-IHG-API-KEY": application_key},
            follow_redirects=False,
            timeout=25,
        )
    except httpx.HTTPError:
        raise ProviderError(ErrorCode.NETWORK_ERROR, "IHG API request failed") from None
    if response.status_code in (401, 403):
        raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "IHG API denied request")
    if response.status_code == 429:
        raise ProviderError(ErrorCode.RATE_LIMITED, "IHG API rate limited")
    if response.status_code != 200:
        raise ProviderError(ErrorCode.HTTP_ERROR, "IHG API did not return HTTP 200")
    try:
        body = response.json()
    except ValueError:
        raise invalid("expected JSON") from None
    digest = hashlib.sha256(response.content).hexdigest()
    return parse_offers(body, request_body, search, digest)
