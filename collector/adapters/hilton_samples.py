"""Parse observed public room-card responses without replaying private traffic.

These are dated room minima, NOT verified public room/plan offers. They have no
conversion to Quote and cannot enter the detector/Bark path. HAR source fields
are whitelisted: cookies, guest identifiers and cache/session tokens are ignored.
"""

import re
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

ROOM_SEARCH_PATH = "/graphql/customer"
ROOM_SEARCH_OPERATION = "hotel_shopAvailOptions_shopPropAvail"
ROOM_PRICE_FIELD = "moreRatesFromRate"
# This alias was observed in both supplied files. Never assume every five-letter
# property prefix resolves to the requested full hotel identity.
OBSERVED_HOTEL_ALIASES = {"SHAQT": "SHAQTWA"}
CNY_FORMAT = re.compile(r"¥(?P<amount>(?:[1-9]\d{0,2}(?:,\d{3})+|\d+)\.\d{2})")


class SampleValidationError(ValueError):
    pass


@dataclass(frozen=True)
class HiltonRoomObservation:
    hotel_id: str
    requested_hotel_id: str
    check_in: date
    check_out: date
    adults: int
    rooms: int
    room_code: str
    room_name: str
    rate_plan_code: str
    currency: str
    rate_amount: Decimal
    after_tax_display: Decimal
    membership_required: bool | None
    service_charges_and_taxes_included: bool
    special_rates_requested: bool

    @property
    def eligible_for_alerts(self):
        return False  # A minimum card is not a complete public rate-plan offer.

    def to_dict(self):
        value = asdict(self)
        for key in ("check_in", "check_out"):
            value[key] = value[key].isoformat()
        for key in ("rate_amount", "after_tax_display"):
            value[key] = str(value[key])
        return {
            **value,
            "scope": "ONE_NIGHT_ROOM_CARD_MINIMUM",
            "eligible_for_alerts": False,
            "rate_type_verified": False,
        }


def positive_amount(value):
    if isinstance(value, bool):
        raise SampleValidationError("Cash amount is not numeric")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise SampleValidationError("Cash amount is not numeric") from None
    if not amount.is_finite() or amount <= 0:
        raise SampleValidationError("Cash amount must be finite and positive")
    return amount


def validate_search_context(variables: dict, response: dict, *, expected_hotel_id=None):
    """Shared public request/response identity checks for room and rate pages."""
    if not isinstance(variables, dict) or not isinstance(response, dict):
        raise SampleValidationError("Request variables and response must be objects")
    if response.get("errors"):
        raise SampleValidationError("GraphQL response contains errors; partial data not accepted")
    try:
        hotel = response["data"]["hotel"]
        requested_id = variables["ctyhocn"]
        hotel_id = hotel["ctyhocn"]
        check_in = date.fromisoformat(variables["arrivalDate"])
        check_out = date.fromisoformat(variables["departureDate"])
        adults, rooms, children = (variables[key] for key in ("numAdults", "numRooms", "numChildren"))
        availability = hotel["shopAvail"]
        currency = availability["currencyCode"]
        room_types = availability["roomTypes"]
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SampleValidationError("Hotel, dated stay, occupancy or room fields are missing") from None
    if not all(
        isinstance(code, str) and re.fullmatch(r"[A-Z0-9]{5,10}", code) for code in (requested_id, hotel_id)
    ):
        raise SampleValidationError("Hotel identity is invalid")
    if OBSERVED_HOTEL_ALIASES.get(requested_id, requested_id) != hotel_id or (
        expected_hotel_id and expected_hotel_id != hotel_id
    ):
        raise SampleValidationError("Requested and returned hotels do not match")
    if (check_out - check_in).days != 1:
        raise SampleValidationError("Only the observed one-night price basis is supported")
    if (
        type(adults) is not int
        or adults < 1
        or type(rooms) is not int
        or rooms != 1
        or type(children) is not int
        or children != 0
    ):
        raise SampleValidationError("Only a single room with explicit adults and zero children is supported")
    if currency != "CNY":
        raise SampleValidationError("Only the observed explicit CNY formatted-price basis is supported")
    if (
        type(availability.get("statusCode")) is not int
        or availability["statusCode"] != 0
        or not isinstance(room_types, list)
        or not room_types
    ):
        raise SampleValidationError("No confirmed room-price cards; not a sold-out result")
    special = variables.get("specialRates") or {}
    if not isinstance(special, dict):
        raise SampleValidationError("Special-rate search conditions are ambiguous")
    special_requested = any(value not in (None, False, "", 0) for value in special.values())
    return hotel_id, requested_id, check_in, check_out, adults, rooms, currency, room_types, special_requested


def parse_room_search(variables: dict, response: dict, *, expected_hotel_id=None):
    """Validate the room-card minimum, without promoting it into a full offer."""
    hotel_id, requested_id, check_in, check_out, adults, rooms, currency, room_types, special_requested = (
        validate_search_context(variables, response, expected_hotel_id=expected_hotel_id)
    )
    result, seen = [], set()
    for room in room_types:
        try:
            code, name = room["roomTypeCode"], room["roomTypeName"]
            minimum = room[ROOM_PRICE_FIELD]
            plan = minimum["ratePlan"]
            rate_code = minimum["ratePlanCode"]
            amount = positive_amount(minimum["rateAmount"])
            full_text = minimum["fullAmountAfterTax"]
        except (KeyError, TypeError):
            raise SampleValidationError("Room-card minimum or rate-plan metadata is missing") from None
        if (
            not isinstance(code, str)
            or not re.fullmatch(r"[A-Z0-9]{1,20}", code)
            or code in seen
            or minimum.get("roomTypeCode") != code
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(rate_code, str)
            or not re.fullmatch(r"[A-Z0-9]{1,30}", rate_code)
        ):
            raise SampleValidationError("Room or rate-plan identity is missing, duplicate or inconsistent")
        seen.add(code)
        match = CNY_FORMAT.fullmatch(full_text) if isinstance(full_text, str) else None
        if not match:
            raise SampleValidationError("Precise CNY after-tax display is missing or ambiguous")
        after_tax = positive_amount(match["amount"].replace(",", ""))
        if after_tax < amount:
            raise SampleValidationError("After-tax display is below the base amount")
        if not isinstance(plan, dict) or type(plan.get("serviceChargesAndTaxesIncluded")) is not bool:
            raise SampleValidationError("Tax inclusion flag is missing")
        membership = plan.get("hhonorsMembershipRequired")
        if membership is not None and type(membership) is not bool:
            raise SampleValidationError("Membership condition is invalid")
        result.append(
            HiltonRoomObservation(
                hotel_id,
                requested_id,
                check_in,
                check_out,
                adults,
                rooms,
                code,
                name,
                rate_code,
                currency,
                amount,
                after_tax,
                membership,
                plan["serviceChargesAndTaxesIncluded"],
                special_requested,
            )
        )
    return result
