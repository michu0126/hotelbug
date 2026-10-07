"""Parse observed IHG public room cards, never directory or member starting prices."""

import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import HotelData, RateData, RateRequest

# Real Mainland China directory links for HUALUXE omit the brand segment.
# Keep the four capture positions stable; missing brand remains unknown.
PROPERTY_PATH = re.compile(
    r"(?:/([a-z0-9-]+))?/hotels/([a-z]{2})/en/([a-z0-9-]+)/([a-z0-9]{5})/hoteldetail/?"
)
AMOUNT = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?")
WINDOW_CLOSED = "Rooms cannot be booked this far in advance. Please change dates."
NO_ROOMS = "No rooms available for selected dates"


def property_identity(url: str):
    parsed = urlparse(url)
    match = PROPERTY_PATH.fullmatch(parsed.path)
    if parsed.scheme != "https" or parsed.hostname != "www.ihg.com" or parsed.query or not match:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG requires an official English hotel detail URL")
    return match


def parse_hotel(html: str, url: str) -> HotelData:
    identity = property_identity(url)
    heading = BeautifulSoup(html, "html.parser").select_one("h1")
    if heading is None or not heading.get_text(" ", strip=True):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG hotel name not loaded")
    return HotelData(
        provider="ihg",
        provider_hotel_id=identity[4].upper(),
        hotel_name=heading.get_text(" ", strip=True),
        brand=identity[1],
        city=identity[3],
        official_url=url,
    )


def _verify_stay_query(url: str, request: RateRequest, *, selected_hotel=True):
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    # The observed public form pads days 1–9 (05/06); earlier pilots used
    # 20/21 and missed this. Normalize only a single valid calendar-day value,
    # not duplicate, signed, decimal, malformed or mismatching date fields.
    for key in ("qCiD", "qCoD"):
        values = query.get(key)
        if values and len(values) == 1 and re.fullmatch(r"0?[1-9]|[12][0-9]|3[01]", values[0]):
            query[key] = [str(int(values[0]))]
    expected = {
        "qSlH": request.provider_hotel_id.upper(),
        "qRms": str(request.rooms),
        "qAdlt": str(request.adults),
        "qChld": "0",
        "qCiD": str(request.check_in.day),
        # IHG's generated URLs use zero-based month followed by the full year.
        "qCiMy": f"{request.check_in.month - 1:02d}{request.check_in.year}",
        "qCoD": str(request.check_out.day),
        "qCoMy": f"{request.check_out.month - 1:02d}{request.check_out.year}",
        "qRtP": "6CBARC",
    }
    if not selected_hotel:
        # The observed unavailable response redirects to hotel-search and drops
        # qSlH. Its exact hotel code must instead be proven by the selected card.
        expected.pop("qSlH")
        if "qSlH" in query:
            expected["qSlH"] = request.provider_hotel_id.upper()
    mismatches = [key for key, value in expected.items() if query.get(key) != [value]]
    if mismatches:
        # Only these public stay/plan fields, never the complete query (which
        # can contain unrelated tracking or authentication information).
        def observed(key):
            values = query.get(key)
            if (
                key in {"qCiD", "qCoD", "qCiMy", "qCoMy"}
                and values
                and len(values) == 1
                and re.fullmatch(r"[0-9]{1,6}", values[0])
            ):
                return values[0]
            return "different"

        detail = ", ".join(f"{key}: expected={expected[key]}, observed={observed(key)}" for key in mismatches)
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG hotel, dates or occupancy mismatch; " + detail)


def verify_booking(url: str, request: RateRequest):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.ihg.com"
        or parsed.fragment
        or not parsed.path.endswith("/en/find-hotels/select-roomrate")
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG booking page identity invalid")
    _verify_stay_query(url, request)


def hotel_search_unavailable(
    html: str, url: str, request: RateRequest, hotel_name: str, dates: list[str], guests: str
) -> bool:
    """Only the target card's explicit no-room state, never another hotel's text.

    Caller additionally requires the target card and its exact message visible.
    Public list starting prices are not parsed as offers.
    """
    parsed = urlparse(url)
    if not parsed.path.endswith("/en/find-hotels/hotel-search"):
        return False
    if parsed.scheme != "https" or parsed.netloc != "www.ihg.com" or parsed.fragment:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG hotel search page identity invalid")
    soup = BeautifulSoup(html, "html.parser")
    code = request.provider_hotel_id.upper()
    cards = [
        card
        for card in soup.select('app-hotel-card-list-view[data-testid="hotel-card"]')
        if card.get("id") == code
    ]
    if len(cards) != 1:
        raise ProviderError(
            ErrorCode.INVALID_RESPONSE, "IHG target unavailable hotel card missing or duplicated"
        )
    card = cards[0]
    if NO_ROOMS not in card.get_text(" ", strip=True):
        return False
    headings = card.select('h2[data-testid="brandHotelNameSID"]')
    if len(headings) != 1 or headings[0].get_text(" ", strip=True) != hotel_name:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG unavailable hotel name mismatch")
    identities = set()
    for link in card.select("a[href]"):
        candidate = urlparse(link.get("href", ""))
        match = PROPERTY_PATH.fullmatch(candidate.path)
        if candidate.scheme == "https" and candidate.netloc == "www.ihg.com" and match:
            # This is identity evidence in the already-rendered card, not a
            # URL to navigate. Public website links may carry tracking query/
            # fragment fields; they don't change their explicit hotel path.
            selected = parse_qs(candidate.query, keep_blank_values=True).get("qSlH")
            if selected is not None and selected != [code]:
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "IHG unavailable hotel official link mismatch"
                )
            identities.add(match[4].upper())
    if identities != {code}:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG unavailable hotel official link mismatch")
    _verify_stay_query(url, request, selected_hotel=False)
    if dates != [
        f"{day.month:02d}/{day.day:02d}/{day.year}" for day in (request.check_in, request.check_out)
    ]:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG displayed stay dates mismatch")
    if guests != f"1 Room, {request.adults} Guest" + ("s" if request.adults != 1 else ""):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG displayed guest count mismatch")
    return True


def verify_displayed_stay(html: str, request: RateRequest, hotel_name: str, dates: list[str], guests: str):
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one('[data-testid="hotelDetailsInfoHotelName"]')
    if not heading or heading.get_text(" ", strip=True) != hotel_name:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG displayed hotel mismatch")
    if dates != [
        f"{day.month:02d}/{day.day:02d}/{day.year}" for day in (request.check_in, request.check_out)
    ]:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG displayed stay dates mismatch")
    if guests != f"1 Room, {request.adults} Guest" + ("s" if request.adults != 1 else ""):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG displayed guest count mismatch")


def booking_window_closed(
    html: str, request: RateRequest, hotel_name: str, dates: list[str], guests: str
) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if WINDOW_CLOSED not in soup.get_text(" ", strip=True):
        return False
    # IHG removes URL query parameters for this response. Still require the
    # rendered full-year dates, hotel and occupancy before recording its status.
    verify_displayed_stay(html, request, hotel_name, dates, guests)
    identities = []
    for link in soup.select('a[href*="/hoteldetail/hotel-reviews"]'):
        url = urlparse(link.get("href", ""))
        identity = PROPERTY_PATH.fullmatch(url.path.removesuffix("/hotel-reviews"))
        if url.scheme == "https" and url.hostname == "www.ihg.com" and identity:
            identities.append(identity[4].upper())
    if set(identities) != {request.provider_hotel_id.upper()}:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG booking-window hotel code not confirmed")
    return True


def parse_room(html: str, request: RateRequest, url: str, room_code: str) -> list[RateData]:
    verify_booking(url, request)
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one('[data-testid="roomNameTestId"]')
    if (
        not re.fullmatch(r"[A-Z0-9]+", room_code)
        or not heading
        or heading.get("id") != f"room-card-title-{room_code}"
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG room identity mismatch")
    toggle = soup.select_one('input[role="switch"]')
    if toggle is None or toggle.get("aria-checked") != "false":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG member discount is not confirmed off")
    rates = []
    seen = set()
    for card in soup.select('[data-testid="rateCard"]'):
        title = card.select_one('[data-testid="rateNameOrPolicy"]')
        amount = card.select_one('[data-testid="priceSID"] .cash')
        currency = card.select_one('[data-testid="priceSID"] .currency')
        button = card.select_one('[data-testid="select-btn"]')
        basis = card.select_one('[data-testid="guest-count-info"]')
        if not all((title, amount, currency, button, basis)):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG public quote fields missing")
        value = amount.get_text(strip=True)
        unit = currency.get_text(strip=True)
        identity = button.get("data-slnm-ihg", "")
        prefix = "roomRate" + room_code
        if (
            not AMOUNT.fullmatch(value)
            or not re.fullmatch(r"[A-Z]{3}", unit)
            or not identity.startswith(prefix)
            or not re.fullmatch(r"[A-Z0-9]+", identity[len(prefix) :])
            or basis.get_text(" ", strip=True) != "per night"
            or "IHG One Rewards Discount" in card.get_text(" ", strip=True)
        ):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG public quote identity or amount invalid")
        code = identity[len(prefix) :]
        if code in seen:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "IHG duplicate public rate plan")
        seen.add(code)
        terms = card.get_text(" ", strip=True)
        refundable = (
            True
            if card.select_one('[data-testid="refundable"]')
            else (False if card.select_one('[data-testid="non-refundable"]') else None)
        )
        rates.append(
            RateData(
                **request.model_dump(),
                provider="ihg",
                room_type=heading.get_text(" ", strip=True),
                room_code=room_code,
                rate_name=title.get_text(" ", strip=True),
                rate_code=code,
                # VAT Included is not proof that all mandatory fees are included globally.
                cash_price=Decimal(value.replace(",", "")),
                currency=unit,
                refundable=refundable,
                breakfast_included=True if "Includes breakfast" in terms else None,
                member_rate=False,
                availability=True,
                price_basis="nightly",
                source_url=url,
                raw_response_hash=hashlib.sha256(str(card).encode()).hexdigest(),
            )
        )
    if not rates:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "IHG public rate cards not loaded")
    return rates
