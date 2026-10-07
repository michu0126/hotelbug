"""Hyatt Chinese rendered booking DOM, observed in ordinary Chrome 2026-10-04.

No network endpoints or browser credentials are used. Unselected carousel slides
can repeat another room's prices: only the selected slide is a quote source.
"""

import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.providers.hyatt_catalog import property_identity
from app.schemas.domain import HotelData, RateData, RateRequest

ROOT = "https://www.hyatt.com"
PROPERTY_URL = ROOT + "/caption-by-hyatt/zh-CN/shacp-caption-by-hyatt-zhongshan-park-shanghai"


def hotel_code(value: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9]{3,12}", value):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt hotel code is invalid")
    return value.lower()


def rooms_url(request: RateRequest) -> str:
    return (
        ROOT
        + "/zh-CN/shop/rooms/"
        + hotel_code(request.provider_hotel_id)
        + "?"
        + urlencode(
            {
                "checkinDate": request.check_in.isoformat(),
                "checkoutDate": request.check_out.isoformat(),
                "rooms": request.rooms,
                "adults": request.adults,
                "kids": 0,
                "rate": "Standard",
                "accessibilityCheck": "false",
            }
        )
    )


def verify_url(url: str, request: RateRequest, *, room: str | None = None, plan: str | None = None):
    parsed = urlparse(url)
    path = "/zh-CN/shop/rooms/" + hotel_code(request.provider_hotel_id)
    if room is not None and plan is not None:
        path += "/" + room + "/" + plan
    query = parse_qs(parsed.query, keep_blank_values=True)
    expected = parse_qs(urlparse(rooms_url(request)).query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "www.hyatt.com"
        or parsed.path != path
        or parsed.fragment
        or any(query.get(key) != value for key, value in expected.items())
        or set(query) - set(expected) - {"hpesrId", "hotelCurrencyCode"}
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt booking URL does not match stay")


def verify_stay(html: str, url: str, request: RateRequest, expected_name: str | None = None) -> str:
    verify_url(url, request)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one('[data-locator="hotel-name"] h1')
    name = title.get_text(" ", strip=True) if title else ""
    if not name or (expected_name is not None and name != expected_name):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt displayed hotel changed or is missing")
    date_nodes = soup.select('main [data-locator="dates"]')
    expected = [f"{d.year}年{d.month}月{d.day}日" for d in (request.check_in, request.check_out)]
    # The mobile and desktop headers must agree, not merely contain two dates
    # somewhere in a long calendar or a hidden modal.
    if not date_nodes or any(
        re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", node.get_text()) != expected for node in date_nodes
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt displayed dates do not match")
    guests = soup.select('main [data-locator="rooms-guests"]')
    if not guests or any(
        re.findall(r"(\d+)\s*客房", node.get_text()) != [str(request.rooms)]
        or re.findall(r"(\d+)\s*宾客", node.get_text()) != [str(request.adults)]
        for node in guests
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt displayed rooms or guests do not match")
    return name


def parse_hotel(html: str, url: str) -> HotelData:
    code, brand = property_identity(url)
    soup = BeautifulSoup(html, "html.parser")
    title = soup.select_one("h1")
    if title is None or not title.get_text(strip=True):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt hotel heading is missing")
    return HotelData(
        provider="hyatt",
        provider_hotel_id=code,
        hotel_name=title.get_text(" ", strip=True),
        official_url=url,
        # A brand URL slug is explicit; address/city/country/currency are not
        # inferred from a hotel name, locale or an ambiguous currency symbol.
        brand=brand,
    )


def active_room(html: str, room_name: str):
    soup = BeautifulSoup(html, "html.parser")
    frames = soup.select('li.slide.selected > [role="region"].room-rates-frame-container')
    if len(frames) != 1 or frames[0].get("aria-label") != room_name:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt active room slide does not match")
    frame = frames[0]
    title = frame.select_one("h2.title")
    if title is None or title.get_text(" ", strip=True) != room_name:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt modal room heading changed")
    return frame


def quote_currency(html: str, request: RateRequest, room_code: str, room_name: str) -> str:
    """Read the explicit ISO currency in the displayed member-booking link.

    This is a DOM read of a public navigation link, not a login request. A symbol
    such as ¥ cannot distinguish CNY from JPY, so it is never a currency source.
    """
    frame = active_room(html, room_name)
    currencies = set()
    for link in frame.select('a[data-locator="member-discount-signIn"][href]'):
        parsed = urlparse(link["href"])
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.hyatt.com"
            or parsed.path != "/zh-CN/member/sign-in"
        ):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt displayed booking link is invalid")
        returns = parse_qs(parsed.query).get("returnUrl", [])
        if len(returns) != 1:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt booking link identity is ambiguous")
        selected = frame.select_one('[role="radio"][aria-checked="true"] input[name="rate"]')
        if selected is None:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt selected plan is missing")
        verify_url(returns[0], request, room=room_code, plan=selected.get("value"))
        values = parse_qs(urlparse(returns[0]).query).get("hotelCurrencyCode", [])
        if len(values) != 1 or not re.fullmatch(r"[A-Z]{3}", values[0]):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt explicit quote currency is missing")
        currencies.add(values[0])
    if len(currencies) != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt quote currency is missing or ambiguous")
    return currencies.pop()


def parse_selected_rate(
    html: str,
    request: RateRequest,
    *,
    room_code: str,
    room_name: str,
    plan_code: str,
    currency: str,
    hotel_name: str,
) -> RateData:
    if request.rooms != 1 or (request.check_out - request.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Hyatt page parser supports one room, one night")
    if not all(re.fullmatch(r"[A-Z0-9]+", v) for v in (room_code, plan_code)) or not re.fullmatch(
        r"[A-Z]{3}", currency
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt quote identity or currency is invalid")
    frame = active_room(html, room_name)
    heading = frame.select_one(".room-rates-details-link-section__header h3")
    if heading is None or heading.get_text(" ", strip=True) != hotel_name:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt modal hotel heading changed")
    dates = frame.select_one("fieldset.rates_selector_fieldset")
    expected = [f"{d.year}年{d.month}月{d.day}日" for d in (request.check_in, request.check_out)]
    if dates is None or re.findall(r"\d{4}年\d{1,2}月\d{1,2}日", dates.get_text()) != expected:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt modal dates changed")
    selectors = frame.select('[role="radio"][aria-checked="true"]')
    if len(selectors) != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt selected rate is ambiguous")
    selected = selectors[0]
    radio = selected.select_one('input[name="rate"]')
    title = selected.select_one(f'[id="{plan_code}-name"]')
    price = selected.select_one(f'[id="{plan_code}-rateValue"] [data-locator="cash"]')
    description = selected.select_one(f'[id="{plan_code}-rate-description"][aria-hidden="false"]')
    if radio is None or radio.get("value") != plan_code or not title or not price or not description:
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Hyatt selected plan fields are incomplete")
    values = [
        n.get_text(" ", strip=True)
        for n in frame.select('.room_rates_modal__rates--static .rate_value [data-locator="cash"]')
    ]
    amount_text = price.get_text(" ", strip=True)
    if values != [amount_text]:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt selected price has not updated")
    nightly = frame.select_one('.room_rates_modal__rates--static [data-locator="per-night-label"]')
    if nightly is None or nightly.get_text(strip=True) != "平均/晚":
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt nightly price basis is missing")
    amount = re.fullmatch(r"[^\d\s]+\s*([0-9]+(?:,[0-9]{3})*(?:\.[0-9]{1,2})?)", amount_text)
    if not amount:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hyatt cash price is unreadable")
    policy = frame.select_one('.room_rates_modal__rates--static [data-locator="penalty-description"]')
    policy_text = policy.get_text(" ", strip=True) if policy else ""
    terms = description.get_text(" ", strip=True)
    return RateData(
        **request.model_dump(exclude={"provider_hotel_id"}),
        provider="hyatt",
        provider_hotel_id=hotel_code(request.provider_hotel_id).upper(),
        room_type=room_name,
        room_code=room_code,
        rate_name=title.get_text(" ", strip=True),
        rate_code=plan_code,
        cash_price=Decimal(amount[1].replace(",", "")),
        currency=currency,
        member_rate="selectors-selector--member" in selected.parent.get("class", []),
        refundable=False
        if "不可退款" in policy_text or "不得取消" in policy_text
        else True
        if "取消，以免支付" in policy_text
        else None,
        breakfast_included=True if "早餐" in terms else None,
        availability=True,
        price_basis="nightly",
        source_url=rooms_url(request),
        raw_response_hash=hashlib.sha256(html.encode()).hexdigest(),
    )
