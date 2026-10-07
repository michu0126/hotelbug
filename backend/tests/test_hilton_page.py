from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import ProviderError
from app.providers.hilton_page import parse_public_rates, stay_label_expectations
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
    assert [rate.cash_price for rate in rates] == list(map(Decimal, ("595", "584", "548", "583")))
    assert all(rate.total_price is None for rate in rates)
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


ENGLISH_HOTEL = "Conrad London St. James"
ENGLISH_ROOM = "King Deluxe Room"
ENGLISH_ROOMS_URL = ROOMS_URL.replace("/zh-hant/", "/en/")


def english_rendered_page():
    # Projection of the normal homepage -> search -> rooms -> rates flow,
    # observed on 2026-10-01. No cookies, search tokens or hidden payloads.
    html = f"""<button data-testid="search-edit-button" aria-label="edit stay details
    {ENGLISH_HOTEL}, Tuesday, October 20, 2026 through Wednesday, October 21, 2026,
    1 room for 2 adults"></button><span data-testid="hotelName">{ENGLISH_HOTEL}</span>
    <button data-testid="roomSelectedLabel"><span class="flex-1">{ENGLISH_ROOM}</span>
    <span>Change Room<span>{ENGLISH_ROOM}</span></span></button>
    <p>Prices shown are average per night.</p><p>Prices include taxes in British Pound</p>
    <select id="selectCurrencyConverter"><option selected value="GBP">GBP</option></select>
    <div class="grid">"""
    for code, name, price, terms in (
        ("LV0", "Flexible Rate", "595", "Change or cancel by October 19th 2026. Room only."),
        ("R3X", "Semi-Flex", "584", "Change or cancel by October 15th 2026. Room only."),
        ("PR09AP", "Advance Purchase", "546", "No cancellations. Pay now."),
        (
            "PR09BB",
            "Breakfast Included",
            "631",
            "Change or cancel by October 19th 2026. Includes breakfast daily.",
        ),
        (
            "B3F",
            "Semi-Flex Breakfast Included",
            "619",
            "Change or cancel by October 15th 2026. Includes breakfast daily.",
        ),
        (
            "CX09AP",
            "Advance Purchase Breakfast Included",
            "581",
            "Breakfast included. No cancellations. Pay now.",
        ),
    ):
        html += f'''<div data-testid="rateTableDescriptionCell"><h2 data-testid="rateNameText">{name}</h2>
        <div data-testid="rateDescriptionText">{terms}</div></div>
        <div data-testid="rateTableStandardCell"><div data-testid="standardRateBlock">
        <div data-testid="ratePrice"><p>£{price}</p><button id="{code}InfoModalTrigger">Rate details</button></div>
        </div></div><div data-testid="rateTableHonorsCell"><div data-testid="honorsDiscountPrice">£519</div></div>'''
    return html + "</div>"


def parse_english(html=None, request=SEARCH, **kwargs):
    options = dict(
        rooms_url=ENGLISH_ROOMS_URL,
        currency="GBP",
        hotel_name=ENGLISH_HOTEL,
        room_name=ENGLISH_ROOM,
        room_code="K1D",
    )
    options.update(kwargs)
    return parse_public_rates(html or english_rendered_page(), request, **options)


def test_english_public_grid_keeps_all_six_plans_and_real_conditions():
    rates = parse_english()
    assert [r.cash_price for r in rates] == list(map(Decimal, ("595", "584", "546", "631", "619", "581")))
    assert all(r.total_price is None for r in rates)
    assert [r.rate_code for r in rates] == [
        "public:LV0",
        "public:R3X",
        "public:PR09AP",
        "public:PR09BB",
        "public:B3F",
        "public:CX09AP",
    ]
    assert [r.refundable for r in rates] == [True, True, False, True, True, False]
    # The advance-purchase room-only plan does not explicitly state breakfast;
    # preserve unknown rather than inventing a condition from its title.
    assert [r.breakfast_included for r in rates] == [False, False, None, True, True, True]
    assert all(r.member_rate is False and r.room_code == "K1D" for r in rates)
    assert all(r.price_basis == "nightly" and r.currency == "GBP" for r in rates)
    assert all(r.check_in == SEARCH.check_in and r.check_out == SEARCH.check_out for r in rates)
    assert Decimal("519") not in [r.cash_price for r in rates]


@pytest.mark.parametrize(
    "old,new",
    [
        ("October 21, 2026", "October 21, 2027"),
        ("1 room for 2 adults", "1 room for 1 adult"),
        ("Prices shown are average per night.", "Starting from"),
        ("£546", "£546,50"),
        ("PR09APInfoModalTrigger", "LV0InfoModalTrigger"),
    ],
)
def test_english_mismatches_do_not_create_quotes(old, new):
    with pytest.raises(ProviderError):
        parse_english(english_rendered_page().replace(old, new))


def test_english_singular_adult_label_is_matched_to_actual_request():
    request = SEARCH.model_copy(update={"adults": 1})
    assert "1 room for 1 adult" in stay_label_expectations(request)[1]
    html = english_rendered_page().replace("1 room for 2 adults", "1 room for 1 adult")
    rates = parse_english(
        html, request, rooms_url=ENGLISH_ROOMS_URL.replace("room1NumAdults=2", "room1NumAdults=1")
    )
    assert all(r.adults == 1 for r in rates)


@pytest.mark.parametrize(
    "url",
    [
        ENGLISH_ROOMS_URL.replace("https:", "http:"),
        ENGLISH_ROOMS_URL.replace("www.hilton.com", "other.example"),
        ENGLISH_ROOMS_URL.replace("/rooms/", "/rates/"),
        ENGLISH_ROOMS_URL + "&room2NumAdults=2",
        ENGLISH_ROOMS_URL + "&room1NumChildren=1",
    ],
)
def test_english_original_rooms_identity_and_occupancy_required(url):
    with pytest.raises(ProviderError):
        parse_english(rooms_url=url)


@pytest.mark.parametrize("english", [False, True])
def test_tax_excluded_display_amount_is_saved_without_an_invented_total(english):
    rates = (
        parse_english(
            english_rendered_page().replace("Prices include taxes in British Pound", "Prices exclude taxes")
        )
        if english
        else parse(rendered_page().replace("價格含稅", "價格未含稅"))
    )
    assert all(r.cash_price is not None and r.total_price is None for r in rates)
