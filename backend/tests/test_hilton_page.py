from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import ProviderError
from app.providers.hilton_page import parse_public_rates
from app.schemas.domain import RateRequest

SEARCH = RateRequest(provider_hotel_id="LONCOCI", check_in=date(2026, 10, 20), check_out=date(2026, 10, 21))
ROOMS_URL = (
    "https://www.hilton.com/zh-hant/book/reservation/rooms/"
    "?ctyhocn=LONCOCI&arrivalDate=2026-10-20&departureDate=2026-10-21&room1NumAdults=2"
)
HOTEL = "倫敦聖詹姆斯康萊德酒店"
ROOM = "大床豪華客房"


def rendered_page():
    # Minimal projection of actual visible DOM, LONCOCI / two adults, 2026-09-30.
    html = f"""<button data-testid="search-edit-button" aria-label="編輯住宿詳情 {HOTEL}、
        2026年10月20日 星期二 至 2026年10月21日 星期三、1 間客房，2 位成人"></button>
        <span data-testid="hotelName">{HOTEL}</span>
        <button data-testid="roomSelectedLabel"><span class="flex-1">{ROOM}</span>
        <span data-testid="editTimelineRoom">更改客房<span>{ROOM}</span></span></button>
        <div>所示價格為每晚平均價格。</div><span>價格含稅</span><div class="grid">"""
    for code, name, price, member, terms in (
        ("LV0", "彈性房價", "595", "584", "在 10月 19, 2026 前更改或取消。僅客房。"),
        ("R3X", "優享價", "584", "554", "在 10月 15, 2026 前更改或取消。僅客房。"),
        ("PR09AP", "提前預付", "548", "521", "不得取消。即時付款。"),
        ("CX09AP", "提前預付、包早餐", "583", "554", "包含早餐。不得取消。即時付款。"),
    ):
        html += f'''<div data-testid="rateTableDescriptionCell"><h2 data-testid="rateNameText">{name}</h2>
        <div data-testid="rateDescriptionText">{terms}</div></div>
        <div data-testid="rateTableStandardCell"><div data-testid="standardRateBlock">
        <div data-testid="ratePrice"><p>£{price}</p><button id="{code}InfoModalTrigger">房價詳情</button></div>
        </div></div><div data-testid="rateTableHonorsCell"><div data-testid="honorsDiscountPrice">£{member}</div></div>'''
    return html + "</div>"


def parse(html=None, **kwargs):
    options = dict(rooms_url=ROOMS_URL, currency="GBP", hotel_name=HOTEL, room_name=ROOM)
    options.update(kwargs)
    return parse_public_rates(html or rendered_page(), SEARCH, **options)


def test_public_rows_pair_with_their_own_plan_not_cheaper_member_rows():
    rates = parse()
    assert [rate.total_price for rate in rates] == list(map(Decimal, ("595", "584", "548", "583")))
    assert [rate.rate_code for rate in rates] == [
        "public:LV0",
        "public:R3X",
        "public:PR09AP",
        "public:CX09AP",
    ]
    assert rates[2].rate_name == "提前預付" and rates[2].refundable is False
    assert rates[0].refundable is True and rates[0].breakfast_included is False
    assert rates[3].breakfast_included is True
    assert all(rate.member_rate is False and rate.price_basis == "nightly" for rate in rates)


@pytest.mark.parametrize(
    "old,new",
    [
        ("2026年10月21日", "2027年10月21日"),
        ("2 位成人", "1 位成人"),
        ("價格含稅", "價格未含稅"),
        ("所示價格為每晚平均價格。", "Starting from"),
        ("£548", "£548,50"),
        ("PR09APInfoModalTrigger", "LV0InfoModalTrigger"),
        ("rateTableStandardCell", "rateTableHonorsCell"),
    ],
)
def test_mismatch_or_list_reference_is_not_ingested(old, new):
    with pytest.raises(ProviderError):
        parse(rendered_page().replace(old, new))


@pytest.mark.parametrize(
    "field,value",
    [("room_name", "Wrong room"), ("hotel_name", "Wrong hotel"), ("currency", "£")],
)
def test_wrong_identity_and_missing_currency_rejected(field, value):
    with pytest.raises(ProviderError):
        parse(**{field: value})


@pytest.mark.parametrize(
    "old,new",
    [("LONCOCI", "ABEKZHX"), ("room1NumAdults=2", "room1NumAdults=1"), ("2026-10-21", "2027-10-21")],
)
def test_original_rooms_page_identity_required(old, new):
    with pytest.raises(ProviderError):
        parse(rooms_url=ROOMS_URL.replace(old, new))
