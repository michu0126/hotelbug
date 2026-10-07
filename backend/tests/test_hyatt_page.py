"""Offline projections of the SHACP DOM read on 2026-10-04, not live access tests."""

from datetime import date
from decimal import Decimal
from urllib.parse import quote

import pytest

from app.core.errors import ProviderError
from app.providers.hyatt_page import (
    PROPERTY_URL,
    parse_hotel,
    parse_selected_rate,
    quote_currency,
    rooms_url,
    verify_stay,
    verify_url,
)
from app.schemas.domain import RateRequest
from app.services.offer_policy import classify_offer

SEARCH = RateRequest(
    provider_hotel_id="SHACP", check_in=date(2026, 10, 6), check_out=date(2026, 10, 7), adults=1
)
HOTEL = "上海中山公园凯悦嘉荟酒店"
ROOM = "客房（1 张特大床）"
PLANS = {
    "MYHI": ("会员房价", "487", True, "“凯悦天地”会员专属房价。"),
    "2XPTS": ("双倍积分", "497", True, "凯悦天地会员专属优惠。"),
    "RACK": ("标准房价", "497", False, "定期公布的房价"),
    "MYHIBK": ("会员甜梦美馔", "525", True, "凯悦天地会员专属房价（含早餐）。"),
    "PKGAWA": ("甜梦美馔", "552", False, "所有登记客人可享每日早餐"),
}


def modal_html(plan="MYHI", room_code="KING", room_name=ROOM):
    # Minimal projection: all identities/labels/amounts below were displayed by
    # the public page. Selection variants model normal radio-control changes.
    dates = "2026年10月6日周二 - 2026年10月7日周三"
    html = f'<main><a data-locator="hotel-name" href="{PROPERTY_URL}"><h1>{HOTEL}</h1></a>'
    html += f'<div data-locator="dates">{dates}</div><li data-locator="dates">日期 {dates}</li>'
    html += '<li data-locator="rooms-guests">1 客房, 1 宾客</li>'
    html += f'<div role="dialog"><ul><li class="slide selected previous"><div role="region" class="room-rates-frame-container" aria-label="{room_name}">'
    html += f'<h2 class="title">{room_name}</h2><div class="room-rates-details-link-section__header"><h3>{HOTEL}</h3></div>'
    html += f'<fieldset class="rates_selector_fieldset">显示价格 {dates}<ul role="radiogroup">'
    for code, (name, amount, member, terms) in PLANS.items():
        selected = code == plan
        html += f'<li class="selectors-selector {"selectors-selector--member" if member else ""}">'
        html += f'<label role="radio" aria-checked="{str(selected).lower()}"><div id="{code}-name"><input name="rate" type="radio" value="{code}">{name}</div>'
        html += f'<div id="{code}-rateValue"><span data-locator="cash">¥{amount}</span></div>'
        html += f'<div id="{code}-rate-description" class="selected_rate_description" aria-hidden="{str(not selected).lower()}">{terms}</div></label></li>'
    html += '</ul></fieldset><div class="room_rates_modal__rates--static"><div class="rate_value">'
    html += f'<span data-locator="cash">¥{PLANS[plan][1]}</span><div data-locator="per-night-label">平均/晚</div></div>'
    html += '<div data-locator="penalty-description">提前 1 天于酒店当地时间晚上 11:59 前取消，以免支付 1 晚房费/须提供信用卡担保</div></div>'
    if PLANS[plan][2]:
        return_url = (
            rooms_url(SEARCH).replace("/shacp?", f"/shacp/{room_code}/{plan}?") + "&hotelCurrencyCode=CNY"
        )
        html += f'<a data-locator="member-discount-signIn" href="https://www.hyatt.com/zh-CN/member/sign-in?returnUrl={quote(return_url, safe="")}">登录并预订</a>'
    return html + "</div></li></ul></div></main>"


def parse(html=None, plan="RACK"):
    html = modal_html(plan) if html is None else html
    return parse_selected_rate(
        html, SEARCH, room_code="KING", room_name=ROOM, plan_code=plan, currency="CNY", hotel_name=HOTEL
    )


def test_real_public_and_member_price_projection():
    public, member = parse(), parse(plan="MYHI")
    assert public.cash_price == Decimal("497") and member.cash_price == Decimal("487")
    assert public.total_price is None and public.tax is None
    assert public.refundable is True and public.breakfast_included is None
    assert not public.member_rate and member.member_rate
    assert public.adults == 1 and public.rooms == 1 and public.price_basis == "nightly"
    assert public.room_code == "KING" and public.rate_code == "RACK"
    assert str(public.source_url) == rooms_url(SEARCH)
    assert public.offer_key() != member.offer_key()
    assert classify_offer(public).alert_eligible and not classify_offer(member).alert_eligible


@pytest.mark.parametrize("code", list(PLANS))
def test_plan_fields_and_explicit_breakfast(code):
    rate = parse(plan=code)
    assert rate.rate_name == PLANS[code][0]
    assert rate.member_rate == PLANS[code][2]
    assert rate.breakfast_included is (True if code in {"MYHIBK", "PKGAWA"} else None)


def test_currency_is_explicit_not_inferred_from_yen_symbol():
    assert quote_currency(modal_html(), SEARCH, "KING", ROOM) == "CNY"
    with pytest.raises(ProviderError):
        quote_currency(modal_html("RACK"), SEARCH, "KING", ROOM)
    with pytest.raises(ProviderError):
        quote_currency(modal_html(), SEARCH, "TWIN", ROOM)


@pytest.mark.parametrize(
    "old,new",
    [
        ("CNY", "JPY"),
        ("CNY", "USD"),
    ],
)
def test_iso_currency_from_public_link_not_hardcoded(old, new):
    assert quote_currency(modal_html().replace(old, new), SEARCH, "KING", ROOM) == new


@pytest.mark.parametrize(
    "old,new",
    [
        ("CNY", "¥"),
        ("shacp%2FKING", "shacp%2FTWIN"),
        ("2026-10-06", "2026-10-08"),
        ("adults%3D1", "adults%3D2"),
        ("www.hyatt.com%2Fzh", "example.com%2Fzh"),
    ],
)
def test_currency_link_must_match_exact_stay_and_room(old, new):
    with pytest.raises(ProviderError):
        quote_currency(modal_html().replace(old, new), SEARCH, "KING", ROOM)


def test_displayed_header_and_property_metadata():
    assert verify_stay(modal_html(), rooms_url(SEARCH), SEARCH, HOTEL) == HOTEL
    hotel = parse_hotel(f"<h1>{HOTEL}</h1>", PROPERTY_URL)
    assert hotel.provider_hotel_id == "SHACP" and hotel.brand == "caption-by-hyatt"
    assert hotel.city is None and hotel.country is None and hotel.default_currency is None


@pytest.mark.parametrize(
    "old,new",
    [
        ("2026年10月6日", "2026年10月8日"),
        ("2026年10月7日", "2027年10月7日"),
        ("1 客房", "2 客房"),
        ("1 宾客", "2 宾客"),
        (HOTEL, "其他酒店"),
        ('data-locator="dates"', 'data-locator="calendar"'),
        ('data-locator="rooms-guests"', 'data-locator="other"'),
    ],
)
def test_wrong_or_missing_rendered_stay_rejected(old, new):
    with pytest.raises(ProviderError):
        verify_stay(modal_html().replace(old, new), rooms_url(SEARCH), SEARCH, HOTEL)


@pytest.mark.parametrize(
    "old,new",
    [
        ("adults=1", "adults=2"),
        ("rooms=1", "rooms=2"),
        ("kids=0", "kids=1"),
        ("rate=Standard", "rate=Corporate"),
        ("2026-10-06", "2026-10-07"),
        ("www.hyatt.com", "example.com"),
        ("shacp?", "sydph?"),
        ("accessibilityCheck=false", "accessibilityCheck=true"),
        ("adults=1", "adults=1&adults=2"),
    ],
)
def test_wrong_booking_query_rejected(old, new):
    with pytest.raises(ProviderError):
        verify_url(rooms_url(SEARCH).replace(old, new), SEARCH)


def test_transient_user_search_id_is_not_saved_in_quote():
    verify_stay(modal_html(), rooms_url(SEARCH) + "&hpesrId=transient-search-id", SEARCH)
    assert "hpesrId" not in str(parse().source_url)


@pytest.mark.parametrize(
    "old,new",
    [
        ('class="slide selected previous"', 'class="slide previous"'),
        ('value="RACK"', 'value="OTHER"'),
        ('id="RACK-name"', 'id="missing-name"'),
        ('id="RACK-rateValue"', 'id="missing-price"'),
        ('id="RACK-rate-description"', 'id="missing-description"'),
        ('aria-checked="true"', 'aria-checked="false"'),
        ("平均/晚", "起价"),
        ("¥497", "¥497,50"),
    ],
)
def test_changed_or_unselected_modal_not_accepted(old, new):
    with pytest.raises(ProviderError):
        parse(modal_html("RACK").replace(old, new))


def test_stale_selected_price_not_accepted():
    html = modal_html("RACK").replace(
        '<span data-locator="cash">¥497</span><div data-locator="per-night-label">',
        '<span data-locator="cash">¥487</span><div data-locator="per-night-label">',
    )
    with pytest.raises(ProviderError):
        parse(html)


def test_hidden_carousel_room_copy_cannot_become_a_quote():
    html = modal_html("RACK")
    hidden = html[html.index('<li class="slide selected previous">') : html.index("</ul></div></main>")]
    hidden = (
        hidden.replace("slide selected previous", "slide").replace(ROOM, "其他房型").replace("¥497", "¥1")
    )
    assert parse(html.replace("</ul></div></main>", hidden + "</ul></div></main>")).cash_price == Decimal(
        "497"
    )
    with pytest.raises(ProviderError):
        parse_selected_rate(
            html,
            SEARCH,
            room_code="TWIN",
            room_name="其他房型",
            plan_code="RACK",
            currency="CNY",
            hotel_name=HOTEL,
        )


def test_unclear_policy_does_not_invent_refundability():
    html = modal_html("RACK").replace("取消，以免支付", "请咨询酒店")
    assert parse(html).refundable is None
