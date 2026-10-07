"""Parse Hilton's rendered English/Traditional Chinese public rate grids, not list prices.

Selectors and labels verified in ordinary Chrome on 2026-09-30/2026-10-01. A valid rooms
page must precede the rates page, whose URL no longer includes the stay identity.
The browser adapter passes the verified rooms-page identity into this parser.
"""

import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import RateData, RateRequest

MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def stay_label_expectations(request: RateRequest) -> list[list[str]]:
    days = (request.check_in, request.check_out)
    return [
        [f"{day.year}年{day.month}月{day.day}日" for day in days] + [f"1 間客房，{request.adults} 位成人"],
        [f"{MONTHS[day.month - 1]} {day.day}, {day.year}" for day in days]
        + [f"1 room for {request.adults} {'adult' if request.adults == 1 else 'adults'}"],
    ]


def verify_rooms_url(page_url: str, request: RateRequest) -> None:
    url = urlparse(page_url)
    params = parse_qs(url.query)
    expected = {
        "ctyhocn": request.provider_hotel_id.upper(),
        "arrivalDate": request.check_in.isoformat(),
        "departureDate": request.check_out.isoformat(),
        "room1NumAdults": str(request.adults),
    }
    if (
        url.scheme != "https"
        or url.hostname != "www.hilton.com"
        or url.path not in {"/zh-hant/book/reservation/rooms/", "/en/book/reservation/rooms/"}
        or any(params.get(key) != [value] for key, value in expected.items())
        or any(key.startswith("room2") for key in params)
        or params.get("room1NumChildren", ["0"]) != ["0"]
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton rooms URL does not match stay")


def parse_public_rates(
    html: str,
    request: RateRequest,
    *,
    rooms_url: str,
    currency: str,
    hotel_name: str,
    room_name: str,
    room_code: str | None = None,
) -> list[RateData]:
    if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Hilton page parser supports one room, one night")
    verify_rooms_url(rooms_url, request)
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton selected currency is not readable")
    soup = BeautifulSoup(html, "html.parser")
    verified_hotel = verify_displayed_stay(html, request, hotel_name)
    selected_room = soup.select_one('[data-testid="roomSelectedLabel"] .flex-1')
    if (
        not hotel_name
        or not room_name
        or selected_room is None
        or verified_hotel != hotel_name
        or selected_room.get_text(" ", strip=True) != room_name
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton displayed hotel or room changed")
    text = soup.get_text(" ", strip=True)
    nightly = "所示價格為每晚平均價格。" in text or "Prices shown are average per night." in text
    if not nightly:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton nightly price basis not confirmed")
    digest = hashlib.sha256(html.encode()).hexdigest()
    room_key = hashlib.sha256(room_name.encode()).hexdigest()[:24]
    rates = []
    seen = set()
    for cell in soup.select('[data-testid="rateTableStandardCell"]'):
        # The enclosing grid contains ALL plans; using its first heading is wrong.
        description = cell.find_previous_sibling(attrs={"data-testid": "rateTableDescriptionCell"})
        if description is None:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hilton public plan description is missing")
        title = description.select_one('[data-testid="rateNameText"]')
        terms_node = description.select_one('[data-testid="rateDescriptionText"]')
        price = cell.select_one('[data-testid="standardRateBlock"] [data-testid="ratePrice"] p')
        info = cell.select_one('[data-testid="ratePrice"] button[id]')
        if not title or not terms_node or not price or not info:
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hilton public plan fields are incomplete")
        amount = re.fullmatch(r"[^\d]*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)", price.get_text(strip=True))
        code = re.fullmatch(r"([A-Z0-9]+)InfoModalTrigger", info.get("id", ""))
        if not amount or not code or code[1] in seen:
            raise ProviderError(
                ErrorCode.INVALID_RESPONSE, "Hilton public amount or plan identity is ambiguous"
            )
        seen.add(code[1])
        terms = terms_node.get_text(" ", strip=True)
        refundable = (
            False
            if "不得取消" in terms or "No cancellations." in terms
            else True
            if "前更改或取消" in terms or "Change or cancel by" in terms
            else None
        )
        breakfast = (
            True
            if "早餐" in terms or "breakfast" in terms.lower()
            else False
            if "僅客房" in terms or "Room only." in terms
            else None
        )
        rates.append(
            RateData(
                **request.model_dump(),
                provider="hilton",
                room_type=room_name,
                room_code=room_code or "name:" + room_key,
                rate_name=title.get_text(" ", strip=True),
                rate_code="public:" + code[1],
                # "Prices include taxes" does not prove that every mandatory
                # resort/destination fee is included globally. Preserve the
                # actual public display amount without inventing a final total.
                cash_price=Decimal(amount[1].replace(",", "")),
                currency=currency,
                refundable=refundable,
                breakfast_included=breakfast,
                member_rate=False,
                availability=True,
                price_basis="nightly",
                source_url=rooms_url,
                raw_response_hash=digest,
            )
        )
    if not rates:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hilton public rate grid is absent")
    return rates


def verify_displayed_stay(html: str, request: RateRequest, expected_hotel: str | None = None) -> str:
    soup = BeautifulSoup(html, "html.parser")
    header = soup.select_one('[data-testid="search-edit-button"]')
    label = header.get("aria-label", "") if header else ""
    if not any(all(value in label for value in expected) for expected in stay_label_expectations(request)):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton displayed stay dates or guests do not match")
    hotel = soup.select_one('[data-testid="hotelName"]')
    name = hotel.get_text(" ", strip=True) if hotel else ""
    if not name or name not in label or (expected_hotel is not None and name != expected_hotel):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hilton displayed hotel changed")
    return name
