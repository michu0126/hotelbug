"""Observed Hilton rates-page public plans, distinct from honors discounts/minima."""

import re
from dataclasses import dataclass
from datetime import datetime
from html import unescape

from adapters.hilton_samples import (
    CNY_FORMAT,
    SampleValidationError,
    positive_amount,
    validate_search_context,
)
from models import Hotel, Quote, Stay

PUBLIC_GROUPS = ("roomOnlyRates", "packageRates")


@dataclass(frozen=True)
class ParsedRates:
    quotes: tuple[Quote, ...]
    excluded: tuple[dict, ...]


def parse_public_rates(
    variables: dict, response: dict, hotel: Hotel, stay: Stay, *, observed_at: datetime, source_url: str
) -> ParsedRates:
    _, _, check_in, check_out, adults, rooms, currency, room_types, special = validate_search_context(
        variables, response, expected_hotel_id=hotel.hotel_id
    )
    if hotel.brand != "hilton" or Stay(check_in, check_out, adults, rooms) != stay:
        raise SampleValidationError("Returned stay does not match the requested task")
    selected = variables.get("selectedRoomTypeCode")
    if not isinstance(selected, str) or not re.fullmatch(r"[A-Z0-9]{1,20}", selected):
        raise SampleValidationError("A selected rates-page room identity is required")
    if (
        len(room_types) != 1
        or not isinstance(room_types[0], dict)
        or room_types[0].get("roomTypeCode") != selected
    ):
        raise SampleValidationError("Selected and returned room identities differ")
    if special:
        raise SampleValidationError("Special-rate requests are outside the verified public-price basis")
    room = room_types[0]
    if not isinstance(room.get("roomTypeName"), str) or not room["roomTypeName"].strip():
        raise SampleValidationError("Room name is missing")
    quotes, excluded, seen = [], [], set()
    for group in PUBLIC_GROUPS:
        rates = room.get(group)
        if not isinstance(rates, list):
            raise SampleValidationError("Complete public rate groups are missing")
        for rate in rates:
            if not isinstance(rate, dict):
                raise SampleValidationError("Invalid public rate record")
            plan, guarantee = rate.get("ratePlan"), rate.get("guarantee")
            code = rate.get("ratePlanCode")
            if (
                not isinstance(plan, dict)
                or not isinstance(guarantee, dict)
                or not isinstance(code, str)
                or not re.fullmatch(r"[A-Z0-9]{1,30}", code)
                or plan.get("ratePlanCode") != code
                or rate.get("roomTypeCode") != selected
                or code in seen
            ):
                raise SampleValidationError("Rate-plan identity is missing, duplicate or inconsistent")
            seen.add(code)
            child = rate.get("hhonorsDiscountRate")
            if child:
                if not isinstance(child, dict):
                    raise SampleValidationError("Honors discount record is malformed")
                excluded.append(
                    {
                        "room_code": selected,
                        "rate_code": child.get("ratePlanCode"),
                        "reason": "MEMBER_DISCOUNT_CHILD",
                    }
                )
            # A nested honors discount is member-only even when its own plan
            # omits hhonorsMembershipRequired (as in the supplied live sample).
            if plan.get("hhonorsMembershipRequired") is not False:
                excluded.append({"room_code": selected, "rate_code": code, "reason": "MEMBERSHIP_NOT_PUBLIC"})
                continue
            if plan.get("confidentialRates") is not False or plan.get("specialRateType") is not None:
                excluded.append({"room_code": selected, "rate_code": code, "reason": "RESTRICTED_RATE"})
                continue
            if any(type(guarantee.get(key)) is not bool for key in ("nonRefundable", "depositRequired")):
                raise SampleValidationError("Refund and deposit conditions are not explicit")
            if guarantee.get("isAfterCxlDeadline") is True:
                excluded.append(
                    {"room_code": selected, "rate_code": code, "reason": "CANCELLATION_DEADLINE_PASSED"}
                )
                continue
            if any(
                not isinstance(guarantee.get(key), str) or not guarantee[key]
                for key in ("guarPolicyCode", "cxlPolicyCode")
            ):
                raise SampleValidationError("Payment or cancellation policy identity is missing")
            if any(
                not isinstance(plan.get(key), str) or not plan[key].strip()
                for key in ("ratePlanName", "ratePlanDesc")
            ):
                raise SampleValidationError("Rate name or description is missing")
            included = plan.get("serviceChargesAndTaxesIncluded")
            if type(included) is not bool:
                raise SampleValidationError("Tax basis is not explicit")
            amount = positive_amount(rate.get("rateAmount"))
            full = rate.get("fullAmountAfterTax")
            match = CNY_FORMAT.fullmatch(full) if isinstance(full, str) else None
            if not match:
                raise SampleValidationError("Precise after-tax reference is missing")
            after_tax = positive_amount(match["amount"].replace(",", ""))
            note = rate.get("serviceChargeDesc") or ""
            if not isinstance(note, str):
                raise SampleValidationError("Service-charge description is malformed")
            quotes.append(
                Quote(
                    hotel=hotel,
                    stay=stay,
                    rate_type="PUBLIC_PACKAGE" if group == "packageRates" else "PUBLIC_ROOM_ONLY",
                    room_type=room["roomTypeName"],
                    amount=amount,
                    currency=currency,
                    tax_basis="TAX_INCLUDED" if included else "TAX_EXCLUDED",
                    source_url=source_url,
                    observed_at=observed_at,
                    room_code=selected,
                    rate_code=code,
                    rate_name=plan["ratePlanName"],
                    offer_group=group,
                    cancellation_policy=guarantee["cxlPolicyCode"],
                    guarantee_policy=guarantee["guarPolicyCode"],
                    non_refundable=guarantee["nonRefundable"],
                    deposit_required=guarantee["depositRequired"],
                    membership_required=False,
                    rate_description=plan["ratePlanDesc"],
                    after_tax_display=after_tax,
                    price_note=unescape(re.sub(r"<[^>]+>", "", note)),
                )
            )
    if not seen:
        raise SampleValidationError("No complete public cash-rate records; not a sold-out result")
    return ParsedRates(tuple(quotes), tuple(excluded))
