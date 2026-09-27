"""Observed Marriott website operations. No browser cookies or guessed endpoints.

Browser evidence: docs/providers/marriott.md. Standalone HTTP remains experimental.
Only one-room, one-night cash quotes have been verified against the rendered page.
"""

import base64
import binascii
import hashlib
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx

from app.core.errors import ErrorCode, ProviderError
from app.crawler.http import request
from app.providers.base import HotelProvider
from app.schemas.domain import HealthResult, HotelData, RateData, RateRequest

ROOT = "https://www.marriott.com"
QUERIES = Path(__file__).with_name("queries")
OPERATIONS = {
    "rates": (
        "PhoenixBookDTTSearchProductsByProperty",
        "3f1389385982f058de948b5b51f2ee3db7aa5420920d0761083f139090bc1f9e",
    ),
    "hotel": (
        "PhoenixBookDTTHotelHeaderData",
        "de8681589d34d8ca42a5bff328faa659398979a341b688494f838f2721daa824",
    ),
}


def invalid(message: str) -> ProviderError:
    return ProviderError(ErrorCode.INVALID_RESPONSE, message)


def hotel_code(value: str) -> str:
    code = value.upper()
    if not re.fullmatch(r"[A-Z0-9]{5}", code):
        raise invalid("Marriott hotel code must contain five letters or digits")
    return code


def money(value: dict) -> tuple[Decimal, str]:
    # Marriott amounts are integers with an explicit decimal exponent, not dollars.
    amount, places, currency = value["amount"], value["decimalPoint"], value["currency"]
    if type(amount) is not int or type(places) is not int or not 0 <= places <= 6 or amount < 0:
        raise invalid("Invalid monetary amount")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise invalid("Invalid currency")
    return Decimal(amount).scaleb(-places), currency


def parse_rates(body: dict, search: RateRequest, response_hash: str) -> list[RateData]:
    """Fail closed on partial/unknown results instead of storing misleading prices."""
    try:
        if body.get("errors"):
            raise invalid("GraphQL errors; no prices persisted")
        connection = body["data"]["commerce"]["product"]["searchProductsByProperty"]
        if connection["__typename"] != "ProductSearchByPropertyConnection":
            raise invalid("Unexpected Marriott product response")
        edges = connection["edges"]
        if type(connection["total"]) is not int or connection["total"] != len(edges):
            raise invalid("Incomplete Marriott rate page; pagination not yet verified")
        result = []
        for edge in edges:
            node = edge["node"]
            if node["__typename"] != "HotelRoom":
                raise invalid("Unknown Marriott product type")
            basic, rate = node["basicInformation"], node["rates"]
            if rate["rateModes"]["__typename"] != "HotelRoomRateModesCash":
                # Points and cash+points must never silently become cash quotes.
                raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Non-cash Marriott rate not verified")
            plans = basic["ratePlan"]
            if len(plans) != 1:
                raise invalid("Ambiguous Marriott rate plan")
            plan = plans[0]
            encoded = node["id"]
            identity = (
                base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
                .decode()
                .split("|")
            )
            # The public product identifier binds property, plan, room and stay dates.
            expected = [
                hotel_code(search.provider_hotel_id),
                plan["ratePlanCode"],
                basic["type"].upper(),
                search.check_in.isoformat(),
                search.check_out.isoformat(),
            ]
            if len(identity) != 6 or identity[:5] != expected:
                raise invalid("Marriott product hotel, room, plan or date mismatch")
            pricing = node["totalPricing"]
            if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
                raise invalid("Only single-room single-night Marriott quotes are verified")
            if pricing["quantity"] != search.rooms:
                raise invalid("Room quantity mismatch")
            modes = pricing["rateModes"]
            cash, currency = money(modes["subtotalPerQuantity"]["amount"])
            total, total_currency = money(modes["grandTotal"]["amount"])
            nightly, nightly_currency = money(rate["rateModes"]["averageNightlyRatePerUnit"]["amount"])
            if currency != total_currency or currency != nightly_currency or cash != nightly or total < cash:
                raise invalid("Inconsistent Marriott price basis or currency")
            # Total - base includes fees, so it is NOT an independently verified tax.
            result.append(
                RateData(
                    **search.model_dump(exclude={"provider_hotel_id"}),
                    provider="marriott",
                    provider_hotel_id=hotel_code(search.provider_hotel_id),
                    room_type=", ".join(filter(None, (basic["name"], basic["description"]))),
                    room_code=basic["type"],
                    rate_name=rate["name"],
                    rate_code=plan["ratePlanCode"],
                    cash_price=cash,
                    tax=None,
                    total_price=total,
                    currency=currency,
                    member_rate=basic["isMembersOnly"],
                    refundable=None,
                    breakfast_included=None,
                    availability=True,
                    price_basis="stay_total",
                    source_url=ROOT + "/reservation/rateListMenu.mi",
                    raw_response_hash=response_hash,
                )
            )
        return result
    except ProviderError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error) as exc:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott response schema changed") from exc


def parse_hotel(body: dict, code: str) -> HotelData:
    try:
        if body.get("errors"):
            raise invalid("GraphQL errors in hotel response")
        hotel = body["data"]["catalog"]["propertyById"]
        if hotel["__typename"] != "Hotel" or hotel["id"] != hotel_code(code):
            raise invalid("Hotel identity mismatch")
        basic = hotel["basicInformation"]
        address = hotel["contactInformation"]["address"]
        nickname = hotel["seoNickname"]
        if not re.fullmatch(r"[a-z0-9-]+", nickname) or not nickname.startswith(code.lower() + "-"):
            raise invalid("Unexpected hotel URL slug")
        return HotelData(
            provider="marriott",
            provider_hotel_id=hotel["id"],
            hotel_name=basic["name"],
            brand=basic["brand"]["id"],
            country=address["country"]["code"],
            region=address["stateProvince"]["description"],
            city=address["city"],
            address=", ".join(filter(None, (address.get(k) for k in ("line1", "line2", "line3")))),
            latitude=basic["latitude"],
            longitude=basic["longitude"],
            default_currency=basic["currency"],
            official_url=f"{ROOT}/hotels/travel/{nickname}",
        )
    except ProviderError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott hotel schema changed") from exc


class MarriottProvider(HotelProvider):
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.client = client or httpx.AsyncClient(timeout=30, follow_redirects=False)

    async def _query(self, kind: str, variables: dict) -> tuple[dict, str]:
        operation, signature = OPERATIONS[kind]
        response = await request(
            self.client,
            "POST",
            f"{ROOT}/mi/query/{operation}",
            headers={
                "accept": "*/*",
                "accept-language": "en-US",
                "content-type": "application/json",
                "apollographql-client-name": "phoenix_book",
                "apollographql-client-version": "1",
                "application-name": "book",
                "graphql-operation-name": operation,
                "graphql-operation-signature": signature,
                "graphql-force-safelisting": "true",
                "graphql-require-safelisting": "true",
                "referer": ROOT + "/reservation/rateListMenu.mi",
            },
            json={
                "operationName": operation,
                "variables": variables,
                "query": (QUERIES / f"marriott_{kind}.graphql").read_text(encoding="utf-8"),
            },
        )
        if response.is_redirect:
            raise ProviderError(ErrorCode.TOKEN_EXPIRED, "Marriott session redirect")
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("object required")
        except ValueError as exc:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Expected Marriott JSON response") from exc
        return data, hashlib.sha256(response.content).hexdigest()

    async def search_hotels(self, query: dict) -> list[HotelData]:
        # Explicit property lookup, not a false claim of global/city discovery.
        code = query.get("provider_hotel_id")
        if not isinstance(code, str):
            raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Discovery currently requires provider_hotel_id")
        return [await self.get_hotel_details(code)]

    async def get_hotel_details(self, hotel_id: str) -> HotelData:
        code = hotel_code(hotel_id)
        data, _ = await self._query("hotel", {"propertyId": code})
        return parse_hotel(data, code)

    async def search_rates(self, search: RateRequest) -> list[RateData]:
        if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
            raise invalid("Only single-room single-night Marriott quotes are verified")
        data, digest = await self._query(
            "rates",
            {
                "search": {
                    "propertyId": hotel_code(search.provider_hotel_id),
                    "options": {
                        "startDate": search.check_in.isoformat(),
                        "endDate": search.check_out.isoformat(),
                        "quantity": search.rooms,
                        "numberInParty": search.adults,
                        "childAges": [],
                        "productRoomType": ["ALL"],
                        "productStatusType": ["AVAILABLE"],
                        "rateRequestTypes": [
                            {"value": "", "type": t} for t in ("STANDARD", "PREPAY", "PACKAGES")
                        ]
                        + [{"value": "MRM", "type": "CLUSTER"}],
                        "isErsProperty": False,
                        "disabilityRequest": "ACCESSIBLE_AND_NON_ACCESSIBLE",
                    },
                },
                "offset": 0,
                "limit": 150,
            },
        )
        return parse_rates(data, search, digest)

    async def get_calendar_rates(self, search: RateRequest) -> list[RateData]:
        # A calendar is filled by independently throttled nightly jobs, not a burst loop.
        return await self.search_rates(search)

    async def health_check(self) -> HealthResult:
        day = date.today() + timedelta(days=10)
        rates = await self.search_rates(
            RateRequest(provider_hotel_id="NYCMQ", check_in=day, check_out=day + timedelta(days=1))
        )
        return HealthResult(
            status="ONLINE" if rates else "DEGRADED",
            message="Verified cash quotes" if rates else "No quotes to verify",
        )

    async def close(self) -> None:
        await self.client.aclose()
