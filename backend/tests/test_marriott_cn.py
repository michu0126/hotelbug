import base64
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider, parse_hotel_page
from app.providers.marriott_cn import parse_room_page, verify_summary
from app.providers.registry import create_provider
from app.schemas.domain import RateRequest

SEARCH = RateRequest(provider_hotel_id="SINNV", check_in=date(2026, 10, 30), check_out=date(2026, 10, 31))
URL = "https://www.marriott.com.cn/hotels/travel/sinnv-aloft-singapore-novena"


def product(plan="REGC", code="SINNV", room="QUEN", check_in="2026-10-30"):
    return base64.urlsafe_b64encode(f"{code}|{plan}|{room}|{check_in}|2026-10-31|sample".encode()).decode()


def card():
    return f"""<div class="room-desc"><h3 class="room-name">呼风乐窝, 客房, 1 大床, 东翼</h3>
    <a href="/reservation/ersViewRoomPool.mi?marshaCode=SINNV&roomPoolCode=quen&productId={product("RMOK")}">房间详情</a></div>
    <div class="rate-details"><span class="rate-name">会员房价 灵活退改房价</span><span class="member-icon"></span>
    <button data-tracking-description="Member Rate Select Button">选择</button></div>
    <div class="rate-details"><span class="rate-name">灵活退改房价</span>
    <a href="/reservation/ersViewRateRules.mi?rateProgramCode=REGC&productId={product()}">房价详情</a>
    <span class="room-rate" aria-hidden="false">224</span>
    <span class="room-rate d-none" aria-hidden="false">187</span>
    <span class="currency-label">SGD / 晚</span><div class="taxes-label">含税及所有费用</div>
    <button data-tracking-description="Non-Member Rate Select Button">选择</button></div>"""


def summary():
    return '<form role="search"><button>住宿日期 (1 晚) 2026年10月30日, 周五 2026年10月31日, 周六</button><button>1 间客房, 2位宾客</button><button>常规优惠房价</button></form>'


def test_actual_chinese_card_public_tax_inclusive_not_calendar_or_member():
    rates = parse_room_page(card(), SEARCH)
    assert len(rates) == 1
    rate = rates[0]
    assert (rate.total_price, rate.cash_price, rate.currency) == (Decimal("224"), None, "SGD")
    assert (rate.room_code, rate.rate_code, rate.member_rate) == ("quen", "REGC", False)
    assert (rate.refundable, rate.breakfast_included) == (None, None)
    assert str(rate.source_url).startswith("https://www.marriott.com.cn/")


@pytest.mark.parametrize(
    "old,new",
    [
        (product(), product(check_in="2026-10-29")),
        (product(), product(code="NYCMQ")),
        (product(), product(room="KING")),
        (product(), product(plan="RMOK")),
        ('class="taxes-label"', 'class="taxes-label d-none"'),
        ('class="room-rate d-none"', 'class="room-rate"'),
        ("SGD / 晚", "SGD / 住宿"),
        (">灵活退改房价<", ">会员房价 灵活退改房价<"),
        (
            'href="/reservation/ersViewRateRules.mi',
            'href="https://example.org/reservation/ersViewRateRules.mi',
        ),
    ],
)
def test_incomplete_or_wrong_offer_is_not_saved(old, new):
    with pytest.raises(ProviderError):
        parse_room_page(card().replace(old, new), SEARCH)


def test_missing_public_identity_is_not_inferred_from_title():
    assert (
        parse_room_page(card().replace("Non-Member Rate Select Button", "Member Rate Select Button"), SEARCH)
        == []
    )


def test_summary_dates_and_guests_verified():
    verify_summary(summary(), SEARCH)


@pytest.mark.parametrize(
    "old,new",
    [
        ("2026年10月30日", "2026年10月29日"),
        ("2位宾客", "1位宾客"),
        ("1 间客房", "2 间客房"),
    ],
)
def test_wrong_search_summary_rejected(old, new):
    with pytest.raises(ProviderError):
        verify_summary(summary().replace(old, new), SEARCH)


def test_chinese_property_configured_by_registry_not_denial_fallback():
    provider = create_provider("marriott", "Aloft by Marriott Singapore Novena", URL)
    assert isinstance(provider, MarriottBrowserProvider)
    assert provider._root == "https://www.marriott.com.cn"
    assert provider._official_code == "SINNV"


@pytest.mark.parametrize("extra", ["", "&showFullPrice=false"])
def test_directory_calendar_identity_can_schedule_quotes_without_inventing_slug(extra):
    url = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=SINNV&isSearch=true&currency="
    provider = create_provider("marriott", "Aloft by Marriott Singapore Novena", url + extra)
    assert provider._official_code == "SINNV" and provider._root == "https://www.marriott.com.cn"


@pytest.mark.parametrize("extra", ["&propertyCode=NYCMQ", "&currency=USD", "&useRewardsPoints=true"])
def test_ambiguous_or_modified_directory_calendar_rejected(extra):
    url = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=SINNV&isSearch=true&currency="
    with pytest.raises(ProviderError):
        MarriottBrowserProvider().configure_hotel_url(url + extra)


@pytest.mark.parametrize(
    "url",
    [
        "http://www.marriott.com.cn/hotels/travel/sinnv-aloft-singapore-novena",
        URL + "?token=private",
        URL.replace("www.marriott.com.cn", "www.marriott.com.cn.example.org"),
        URL.replace("www.marriott.com.cn", "person@www.marriott.com.cn"),
    ],
)
def test_non_property_or_untrusted_configuration_rejected(url):
    with pytest.raises(ProviderError):
        MarriottBrowserProvider().configure_hotel_url(url)


def test_chinese_hotel_identity_keeps_observed_host():
    html = '<a href="/hotels/travel/sinnv-aloft-singapore-novena"><h3 class="hotel-name">Aloft by Marriott Singapore Novena</h3></a>'
    assert str(parse_hotel_page(html, "SINNV", "https://www.marriott.com.cn").official_url) == URL


@pytest.mark.asyncio
@pytest.mark.parametrize("initial,reported", [(1, 2), (2, 2)])
async def test_chinese_all_published_rooms_mounted_before_quotes(monkeypatch, initial, reported):
    provider = create_provider("marriott", "Aloft by Marriott Singapore Novena", URL)
    rooms = MagicMock()
    mounted = initial
    rooms.count = AsyncMock(side_effect=lambda: mounted)
    rooms.last.scroll_into_view_if_needed = AsyncMock()
    rooms.nth.return_value.inner_html = AsyncMock(return_value=card())
    counter = SimpleNamespace(inner_text=AsyncMock(return_value=f"{reported} 可用客房"))
    visible_taxes = MagicMock()
    visible_taxes.first.wait_for = AsyncMock()
    label = SimpleNamespace(click=AsyncMock())
    checkbox = SimpleNamespace(is_checked=AsyncMock(return_value=False))
    page = MagicMock()
    page.get_by_test_id.return_value = checkbox
    page.content = AsyncMock(return_value=summary())
    page.locator.side_effect = lambda selector: {
        ".room-rate-wrapper": rooms,
        ".available-room-label": counter,
        ".rate-details .taxes-label:not(.d-none)": visible_taxes,
        'label[for="显示税项及费用"]': label,
    }[selector]

    async def mount(*args, **kwargs):
        nonlocal mounted
        mounted = reported

    page.wait_for_function = AsyncMock(side_effect=mount)
    checked = AsyncMock()
    monkeypatch.setattr("playwright.async_api.expect", lambda locator: SimpleNamespace(to_be_checked=checked))
    result = await provider._collect_rates(page, SEARCH)
    assert len(result) == reported
    assert rooms.last.scroll_into_view_if_needed.await_count == (initial < reported)
    label.click.assert_awaited_once()
    checked.assert_awaited_once()


def test_special_rate_summary_not_accepted_as_regular_public_search():
    with pytest.raises(ProviderError):
        verify_summary(summary().replace("常规优惠房价", "公司优惠房价"), SEARCH)
