"""Rendered Marriott Chinese booking cards, observed 2026-10-08.

The monthly calendar is a cheapest-price hint (often a member, pre-tax price),
not a room offer. Only the subsequent dated non-member room cards are quotes.
"""

import hashlib
import re
from decimal import Decimal
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from app.core.errors import ErrorCode, ProviderError
from app.schemas.domain import RateData, RateRequest

ROOT = "https://www.marriott.com.cn"


def verify_summary(html: str, search: RateRequest) -> None:
    soup = BeautifulSoup(html, "html.parser")
    labels = [b.get_text(" ", strip=True) for b in soup.select("form button, [role=search] button")]
    expected = [f"{d.year}年{d.month}月{d.day}日" for d in (search.check_in, search.check_out)]
    dates = [label for label in labels if label.startswith("住宿日期")]
    guests = [label for label in labels if re.search(r"\d+\s*间客房,", label)]
    if len(dates) != 1 or not all(value in dates[0] for value in expected):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese stay dates mismatch")
    if len(guests) != 1 or not re.search(
        rf"^{search.rooms}\s*间客房,\s*{search.adults}位宾客(?:\s|$|[\ue000-\uf8ff])", guests[0]
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese occupancy mismatch")
    if sum(label.startswith("常规优惠房价") for label in labels) != 1:
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese special rate is not public regular")
    rewards = soup.select_one('input[aria-label="useRewardsPoints"]')
    if rewards is not None and rewards.has_attr("checked"):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese rewards search is selected")


def parse_room_page(html: str, search: RateRequest) -> list[RateData]:
    from app.providers.marriott import hotel_code
    from app.providers.marriott_browser import PRICE, _product_identity

    if search.rooms != 1 or (search.check_out - search.check_in).days != 1:
        raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Chinese quotes require one room and one night")
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.select_one(".room-desc h3.room-name")
    detail = soup.select_one('.room-desc a[href*="roomPoolCode="]')
    if heading is None or detail is None or not heading.get_text(" ", strip=True):
        raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott Chinese room identity missing")
    code = hotel_code(search.provider_hotel_id)
    query = parse_qs(urlparse(detail["href"]).query)
    room = query.get("roomPoolCode", [""])[0]
    identity = _product_identity(detail["href"])
    if (
        query.get("marshaCode") != [code]
        or not room
        or len(identity) < 5
        or identity[0] != code
        or identity[2].lower() != room.lower()
        or identity[3:5] != [search.check_in.isoformat(), search.check_out.isoformat()]
    ):
        raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese room hotel or dates mismatch")
    result = []
    digest = hashlib.sha256(html.encode()).hexdigest()
    for offer in soup.select(".rate-details"):
        # An explicit public-select label is stronger than absence of 会员 text.
        public = offer.select('button[data-tracking-description="Non-Member Rate Select Button"]')
        if len(public) != 1 or offer.select_one(".member-icon"):
            continue
        title = offer.select_one(".rate-name")
        rule = offer.select_one('a[href*="rateProgramCode="]')
        amounts = offer.select('.room-rate[aria-hidden="false"]:not(.d-none)')
        units = offer.select_one(".currency-label")
        taxes = offer.select_one(".taxes-label:not(.d-none)")
        if (
            title is None
            or rule is None
            or len(amounts) != 1
            or units is None
            or taxes is None
            or taxes.get_text(" ", strip=True) != "含税及所有费用"
        ):
            raise ProviderError(ErrorCode.PROVIDER_CHANGED, "Marriott Chinese public quote incomplete")
        name = title.get_text(" ", strip=True)
        amount = amounts[0].get_text(" ", strip=True)
        currency = re.fullmatch(r"([A-Z]{3})\s*/\s*晚", units.get_text(" ", strip=True))
        url = urlparse(urljoin(ROOT, rule["href"]))
        params = parse_qs(url.query)
        plan = params.get("rateProgramCode", [""])[0]
        product = _product_identity(rule["href"])
        if (
            url.scheme != "https"
            or url.netloc != "www.marriott.com.cn"
            or url.path != "/reservation/ersViewRateRules.mi"
            or not name
            or "会员" in name
            or not PRICE.fullmatch(amount)
            or currency is None
            or not plan
            or len(product) < 5
            or product[:3] != [code, plan, room.upper()]
            or product[3:5] != [search.check_in.isoformat(), search.check_out.isoformat()]
        ):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott Chinese public offer identity mismatch")
        result.append(
            RateData(
                **search.model_dump(),
                provider="marriott",
                room_type=heading.get_text(" ", strip=True),
                room_code=room,
                rate_name=name + " · Non-Member Rate",
                rate_code=plan,
                total_price=Decimal(amount.replace(",", "")),
                currency=currency[1],
                availability=True,
                member_rate=False,
                price_basis="stay_total",
                source_url=ROOT + "/reservation/rateListMenu.mi",
                raw_response_hash=digest,
            )
        )
    return result
