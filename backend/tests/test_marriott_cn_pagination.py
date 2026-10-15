"""Offline projections of the observed CN directory pagination, not rate quotes."""

from urllib.parse import urlencode

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_cn_catalog import (
    ROOT,
    directory_url,
    parse_country,
    verify_directory_redirect,
)


def country_source(country="CN", index=0):
    source = "https://www.marriott.com.cn/search/findHotels.mi?" + urlencode(
        {
            "destinationAddress.destination": "中国" if country == "CN" else "新加坡",
            "destinationAddress.country": country,
            "searchType": "InCity",
            "deviceType": "desktop-web",
        }
    )
    return source + (f"#/{index}/" if index else "")


def card_html(number, *, code=None):
    code = code or f"A{number:04d}"
    return (
        '<div data-testid="cardContainer-testid">'
        f'<button class="title-container" aria-label="酒店 {number}">酒店 {number}</button>'
        '<a class="view-rates-button-container" '
        'href="/search/availabilityCalendar.mi?isRateCalendar=true'
        f'&amp;propertyCode={code}&amp;isSearch=true&amp;currency=">查看房价</a></div>'
    )


def page_html(start=1, end=40, total=855, *, active=1, next_state="enabled", mounted=None):
    counter = f'<div class="search-summary-container">{start} - {end} / {total} 结果</div>'
    cards = "".join(card_html(number) for number in range(start, (mounted or end) + 1))
    current = (
        f'<a class="shop-pagination-item active" aria-label="{active}">{active}</a>'
        if active is not None
        else ""
    )
    next_link = (
        f'<a class="shop-pagination-item {"disabled" if next_state == "disabled" else ""}" '
        'aria-label="NextPage">下一页</a>'
        if next_state != "missing"
        else ""
    )
    return counter + cards + current + next_link


def test_page_zero_is_canonical_first_page_but_positive_index_is_preserved():
    first = country_source()
    assert directory_url(ROOT + "#/0/") == ROOT
    assert directory_url(first + "#/0/") == first
    assert directory_url(first + "#/1/") == first + "#/1/"
    observed = first.replace("deviceType=desktop-web", "filterApplied=false") + "&view=list#/1/"
    assert directory_url(observed) == first + "#/1/"
    verify_directory_redirect(first + "#/0/", first)
    verify_directory_redirect(observed, first + "#/1/")


@pytest.mark.parametrize("fragment", ["/01/", "/-1/", "/1", "1", "/100000/", "/1/extra"])
def test_unobserved_page_fragments_are_rejected(fragment):
    with pytest.raises(ProviderError) as error:
        directory_url(country_source() + "#" + fragment)
    assert error.value.code == ErrorCode.INVALID_RESPONSE


def test_root_does_not_accept_country_page_index():
    with pytest.raises(ProviderError):
        directory_url(ROOT + "#/1/")


def test_redirect_must_preserve_the_requested_page_not_only_country():
    with pytest.raises(ProviderError, match="destination changed"):
        verify_directory_redirect(country_source(index=1), country_source())


@pytest.mark.parametrize(
    "start,end,total,index,next_state,next_index",
    [
        (1, 40, 855, 0, "enabled", 1),
        (41, 80, 855, 1, "enabled", 2),
        (841, 855, 855, 21, "disabled", None),
        (841, 855, 855, 21, "missing", None),
    ],
)
def test_first_middle_and_last_page_continue_only_published_next_control(
    start, end, total, index, next_state, next_index
):
    source = country_source(index=index)
    result = parse_country(page_html(start, end, total, active=index + 1, next_state=next_state), source)
    assert result.reported_total == total
    assert len(result.hotels) == end - start + 1
    assert result.page_complete is True
    assert result.complete is False  # A complete individual page is not complete country coverage.
    assert result.unparsed_hotel_links == 0
    assert [str(url) for url in result.directory_urls] == (
        [country_source(index=next_index)] if next_index is not None else []
    )


@pytest.mark.parametrize("active", [None, 1])
def test_single_page_can_prove_country_coverage_without_a_pagination_widget(active):
    source = country_source("SG")
    result = parse_country(page_html(1, 22, 22, active=active, next_state="missing"), source)
    assert result.complete is True and result.page_complete is True
    assert result.directory_urls == []
    assert all(hotel.country == "SG" for hotel in result.hotels)


@pytest.mark.parametrize("next_state", ["missing", "disabled"])
def test_incomplete_list_requires_an_enabled_normal_next_control(next_state):
    result = parse_country(page_html(next_state=next_state), country_source())
    assert len(result.hotels) == 40
    assert result.page_complete is False and result.complete is False
    assert result.directory_urls == []


def test_final_page_with_unexpected_enabled_next_is_not_complete_or_extended():
    result = parse_country(page_html(1, 22, 22), country_source("SG"))
    assert result.page_complete is False and result.complete is False
    assert result.directory_urls == []


@pytest.mark.parametrize(
    "source_index,start,end,total,active",
    [
        (1, 1, 40, 855, 2),
        (0, 41, 80, 855, 1),
        (0, 1, 39, 855, 1),
        (1, 41, 79, 855, 2),
        (21, 841, 854, 855, 22),
    ],
)
def test_requested_index_must_match_exact_published_range(source_index, start, end, total, active):
    with pytest.raises(ProviderError, match="page range mismatch") as error:
        parse_country(page_html(start, end, total, active=active), country_source(index=source_index))
    assert error.value.code == ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("active", [None, 1, 3])
def test_later_page_requires_correct_active_page_control(active):
    with pytest.raises(ProviderError, match="page index mismatch"):
        parse_country(page_html(41, 80, 855, active=active), country_source(index=1))


def test_conflicting_active_controls_cannot_prove_requested_page():
    html = page_html() + '<a class="shop-pagination-item active" aria-label="2">2</a>'
    with pytest.raises(ProviderError, match="page index mismatch"):
        parse_country(html, country_source())


def test_unmounted_cards_stay_partial_but_published_next_can_be_queued():
    result = parse_country(page_html(mounted=36), country_source())
    assert len(result.hotels) == 36 and result.reported_total == 855
    assert result.page_complete is False and result.complete is False
    assert result.unparsed_hotel_links == 0
    assert [str(url) for url in result.directory_urls] == [country_source(index=1)]


@pytest.mark.parametrize("change", ["missing_title", "invalid_link", "duplicate"])
def test_malformed_or_duplicate_card_does_not_claim_complete_current_page(change):
    html = page_html()
    if change == "missing_title":
        html = html.replace('class="title-container" aria-label="酒店 1"', 'class="title-container"')
    elif change == "invalid_link":
        html = html.replace("propertyCode=A0001", "propertyCode=invalid")
    else:
        html = html.replace("propertyCode=A0002", "propertyCode=A0001")
    result = parse_country(html, country_source())
    assert len(result.hotels) == 39 and result.unparsed_hotel_links == 1
    assert result.page_complete is False and result.complete is False
    assert [str(url) for url in result.directory_urls] == [country_source(index=1)]


def test_china_search_scope_does_not_mislabel_taiwan_macau_or_hong_kong_hotels():
    result = parse_country(page_html(1, 2, 2, next_state="disabled"), country_source())
    assert result.complete is True
    assert all(hotel.country is None for hotel in result.hotels)
    # The catalog contains identities only; its incidental directory amounts
    # must not become stay-dated price or availability evidence.
    assert all("price" not in hotel.model_dump() for hotel in result.hotels)
