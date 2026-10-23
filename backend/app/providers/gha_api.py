"""Experimental GHA official JSON reader; deliberately not in provider registry.

Observed anonymous HTTP 200 for an Avani/SynXis property on 2026-10-23.
This does not establish support for every GHA reservation engine. The caller
must supply an identity mapping observed on the official page/CMS: the public
website hotel ID and the reservation system hotel ID are NOT interchangeable.
"""

import asyncio
import hashlib
import json
import re
from decimal import Decimal, InvalidOperation

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import RateData, RateRequest

RATES_URL = "https://oscp.ghadiscovery.com/api/v3/booking/hotel/rooms/rates"


class GHABookingIdentity(BaseModel):
    """Explicit observed mapping, never derived from a guessed URL or name."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    public_hotel_id: str = Field(pattern=r"^[0-9]{1,12}$")
    hotel_code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    brand_code: str = Field(pattern=r"^[A-Za-z0-9_-]{1,32}$")
    chain_id: str = Field(pattern=r"^[0-9]{1,12}$")
    reservation_hotel_id: str = Field(pattern=r"^[0-9]{1,12}$")


def build_payload(identity: GHABookingIdentity, request: RateRequest) -> dict:
    if request.provider_hotel_id != identity.public_hotel_id:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA website/reservation identity mismatch")
    if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "GHA JSON evidence covers one room, one night")
    return {
        "numberOfRooms": 1,
        "numberOfAdults": request.adults,
        "startDate": request.check_in.isoformat(),
        "endDate": request.check_out.isoformat(),
        "hotelCode": identity.hotel_code,
        "brandCode": identity.brand_code,
        "chainId": identity.chain_id,
        "hotelId": identity.reservation_hotel_id,
        "numberOfChildren": 0,
        "childAges": [],
        "content": "full",
        "primaryChannel": "SYDC",
        "secondaryChannel": "DSCVRYLYLTY",
        "loyaltyProgram": "GHA",
        "loyaltyLevel": "RED",
    }


def _invalid(message: str):
    raise ProviderError(ErrorCode.INVALID_RESPONSE, message)


def _money(value) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        _invalid("GHA amount must be numeric")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        _invalid("GHA amount must be numeric")
    if not amount.is_finite() or amount <= 0:
        _invalid("GHA amount must be positive and finite")
    return amount


def parse_rates(body: dict, identity: GHABookingIdentity, request: RateRequest) -> list[RateData]:
    """Keep public/member offers separate and preserve unknown policy fields.

    The response does not echo dates/occupancy/hotel identity; this parser must
    be coupled to the exact response of the supplied request. A bare unrelated
    JSON file cannot prove those conditions. No room-summary/from value is used.
    """
    build_payload(identity, request)
    if not isinstance(body, dict) or body.get("errors") or not isinstance(body.get("rooms"), list):
        _invalid("GHA room response missing or contains errors")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = []
    keys = set()
    for room in body["rooms"]:
        if not isinstance(room, dict) or not isinstance(room.get("rates"), list):
            _invalid("GHA room/rate structure changed")
        room_code, room_name = room.get("roomCode"), room.get("roomName")
        if not all(isinstance(v, str) and v.strip() for v in (room_code, room_name)):
            _invalid("GHA room identity missing")
        for offer in room["rates"]:
            if not isinstance(offer, dict):
                _invalid("GHA rate structure changed")
            code, name, currency = offer.get("rateCode"), offer.get("rateName"), offer.get("currency")
            if not all(isinstance(v, str) and v.strip() for v in (code, name)):
                _invalid("GHA rate identity missing")
            if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
                _invalid("GHA currency missing or invalid")
            if type(offer.get("memberRate")) is not bool or type(offer.get("taxInclusive")) is not bool:
                _invalid("GHA membership/tax inclusion missing or ambiguous")
            total = _money(offer.get("amountWithTaxesFees"))
            # Do not invent a tax amount by subtracting prices: that difference
            # can include fees. Do not promote dailyPrice to a base/tax total.
            cash = _money(offer.get("amount")) if offer["taxInclusive"] is False else None
            if cash is not None and total < cash:
                _invalid("GHA inclusive amount is below base amount")
            rate = RateData(
                **request.model_dump(),
                provider="gha",
                room_type=room_name,
                room_code=room_code,
                rate_name=name,
                rate_code=code,
                cash_price=cash,
                total_price=total,
                currency=currency,
                member_rate=offer["memberRate"],
                availability=True,
                price_basis="stay_total",
                source_url=RATES_URL,
                raw_response_hash=digest,
                # Cancellation/breakfast are absent in the observed quote JSON.
                refundable=None,
                breakfast_included=None,
                tax=None,
            )
            key = rate.offer_key()
            if key in keys:
                _invalid("GHA duplicate offer identity")
            keys.add(key)
            result.append(rate)
    return result


class GHAJsonClient:
    """One-at-a-time, single-attempt experimental client; no retry or login.

    No Cookie/Authorization/application key is needed for the verified sample.
    Access rejection pauses this instance. Production registration must wire
    persistent provider pause and mapping discovery before enabling scheduling.
    """

    def __init__(self, *, proxy: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.proxy = proxy
        self.transport = transport
        self.paused = False
        self._lock = asyncio.Lock()

    async def fetch_prices(self, identity: GHABookingIdentity, request: RateRequest) -> list[RateData]:
        payload = build_payload(identity, request)
        async with self._lock:
            if self.paused:
                raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "GHA JSON source paused")
            try:
                async with httpx.AsyncClient(
                    proxy=self.proxy, transport=self.transport, timeout=30, follow_redirects=False
                ) as client:
                    response = await client.post(RATES_URL, json=payload)
            except httpx.TimeoutException as exc:
                raise ProviderError(ErrorCode.TIMEOUT, "GHA JSON query timed out") from exc
            except httpx.HTTPError as exc:
                raise ProviderError(ErrorCode.NETWORK_ERROR, "GHA JSON query failed") from exc
            if response.status_code in (401, 403, 429):
                self.paused = True
                code = ErrorCode.RATE_LIMITED if response.status_code == 429 else ErrorCode.BLOCKED_BY_ANTIBOT
                raise ProviderError(code, f"GHA JSON access rejected HTTP {response.status_code}")
            if response.status_code != 200:
                raise ProviderError(ErrorCode.HTTP_ERROR, f"GHA JSON HTTP {response.status_code}")
            if "application/json" not in response.headers.get("content-type", "").lower():
                self.paused = True
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "GHA JSON expected; possible verification page"
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "GHA JSON malformed") from exc
            if isinstance(body, dict) and any("captcha" in str(key).casefold() for key in body):
                self.paused = True
                raise ProviderError(ErrorCode.BLOCKED_BY_ANTIBOT, "GHA verification requested")
            return parse_rates(body, identity, request)
