"""Minimal public country-response projections, not live coverage or price data."""

import copy
from urllib.parse import parse_qs, urlencode, urlparse

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_country_response import (
    LIMIT,
    OPERATION,
    parse_country_response,
)


def country_source(country, index=0):
    source = "https://www.marriott.com.cn/search/findHotels.mi?" + urlencode(
        {
            "destinationAddress.destination": {"CN": "中国", "TW": "中国台湾", "SG": "新加坡"}[country],
            "destinationAddress.country": country,
            "searchType": "InCity",
            "deviceType": "desktop-web",
        }
    )
    return source + (f"#/{index}/" if index else "")


def observed(*, country="TW", index=0, total=34):
    """Reduce real TW 34-result and CN 40-result request shapes to identities."""
    offset = index * LIMIT
    request = {
        "operationName": OPERATION,
        "variables": {
            "search": {
                "options": {"customerId": "", "numberInParty": 1},
                "facets": {
                    "terms": [
                        {
                            "type": kind,
                            "dimensions": (["CN", "TW", "MO", "HK"] if country == "CN" else [country])
                            if kind == "COUNTRIES"
                            else [],
                        }
                        for kind in (
                            "BRANDS",
                            "AMENITIES",
                            "PROPERTY_TYPES",
                            "ACTIVITIES",
                            "CITIES",
                            "STATES",
                            "COUNTRIES",
                            "HOTEL_SERVICE_TYPES",
                            "MEETINGS_EVENTS",
                            "TRANSPORTATION_TYPES",
                            "LEISURE_REGIONS",
                        )
                    ],
                    "ranges": [
                        {"type": kind, "dimensions": [], "endpoints": endpoints.copy()}
                        for kind, endpoints in (
                            ("PRICE", ["0", "100", "200", "overflow"]),
                            ("DISTANCE", ["0", "4830", "14520", "80470"]),
                        )
                    ],
                },
            },
            "limit": LIMIT,
            "offset": offset,
            "sort": {
                "fields": [
                    {"field": "DISTANCE", "direction": "ASC"},
                    {"field": "CITY", "direction": "ASC"},
                    {"field": "BRAND", "direction": "ASC"},
                    {"field": "PROPERTY_NAME", "direction": "ASC"},
                ]
            },
        },
    }
    has_next = offset + LIMIT < total
    data = {
        "total": total,
        "status": [{"code": "SUCCESS", "httpStatus": 200, "errors": [], "warnings": []}],
        "pageInfo": {
            "currentOffset": offset,
            "hasNextPage": has_next,
            "hasPreviousPage": offset > 0,
            "nextOffset": offset + LIMIT if has_next else None,
            "previousOffset": offset - LIMIT if offset else None,
        },
        "edges": [
            {"node": {"id": f"A{number:04d}", "basicInformation": {"name": f"酒店 {number}"}}}
            for number in range(offset + 1, min(offset + LIMIT, total) + 1)
        ],
    }
    response = {"data": {"search": {"properties": {"searchByLocation": data}}}}
    return request, response, data, country_source(country, index)


def test_single_country_page_keeps_all_identities_without_emitting_price_or_bookability():
    request, response, data, source = observed()
    data["edges"][0]["node"]["basicInformation"].update(bookable=False, currency="TWD")
    result = parse_country_response(request, response, source)
    assert len(result.hotels) == result.reported_total == 34
    assert result.page_complete is True and result.complete is True
    assert result.directory_urls == []
    assert all(hotel.country == "TW" for hotel in result.hotels)
    assert result.hotels[0].active is True  # Nonbookable is not a deleted hotel.
    assert result.hotels[0].default_currency is None
    assert not any("price" in key or "availability" in key for key in result.hotels[0].model_dump())
    for hotel in result.hotels:
        provider = MarriottBrowserProvider()
        provider.configure_hotel_url(str(hotel.official_url))
        assert provider._official_code == hotel.provider_hotel_id
        assert parse_qs(urlparse(str(hotel.official_url)).query, keep_blank_values=True) == {
            "isRateCalendar": ["true"],
            "propertyCode": [hotel.provider_hotel_id],
            "isSearch": ["true"],
            "currency": [""],
        }


@pytest.mark.parametrize("index,count,next_index", [(0, 40, 1), (1, 40, 2), (21, 15, None)])
def test_cn_page_info_drives_validated_continuation_without_wrong_country_assignment(
    index, count, next_index
):
    request, response, _, source = observed(country="CN", index=index, total=855)
    result = parse_country_response(request, response, source)
    assert len(result.hotels) == count and result.reported_total == 855
    assert result.page_complete is True and result.complete is False
    assert all(hotel.country is None for hotel in result.hotels)
    assert [str(url) for url in result.directory_urls] == (
        [country_source("CN", next_index)] if next_index is not None else []
    )


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r, d: r.update(operationName="phoenixShopSearchPropertiesByLocationInput"),
        lambda r, d: r["variables"].update(limit=50),
        lambda r, d: r["variables"].update(offset=40),
        lambda r, d: r["variables"]["search"]["options"].update(customerId="member-id"),
        lambda r, d: r["variables"]["search"]["options"].update(numberInParty=True),
        lambda r, d: r["variables"]["search"]["options"].update(checkInDate="2026-10-30"),
        lambda r, d: r["variables"]["search"].update(location={"country": "TW"}),
        lambda r, d: r["variables"]["sort"]["fields"][0].update(direction="DESC"),
        lambda r, d: r["variables"]["search"]["facets"]["terms"][0].update(dimensions=["filter"]),
        lambda r, d: r["variables"]["search"]["facets"]["terms"].pop(),
        lambda r, d: r["variables"]["search"]["facets"]["ranges"][0].update(dimensions=["0"]),
        lambda r, d: r["variables"]["search"]["facets"]["ranges"][0].update(endpoints=["0", "100"]),
        lambda r, d: d.update(total=True),
        lambda r, d: d.update(total=0),
        lambda r, d: d["status"][0].update(code="PARTIAL"),
        lambda r, d: d["status"][0].update(httpStatus=403),
        lambda r, d: d["status"][0].update(errors=[{"message": "incomplete"}]),
        lambda r, d: d["pageInfo"].update(currentOffset=40),
        lambda r, d: d["pageInfo"].update(hasNextPage=True),
        lambda r, d: d["pageInfo"].update(hasPreviousPage=True),
        lambda r, d: d["pageInfo"].update(nextOffset=40),
        lambda r, d: d["pageInfo"].update(previousOffset=0),
        lambda r, d: d["edges"].pop(),
        lambda r, d: d["edges"].append(copy.deepcopy(d["edges"][0])),
        lambda r, d: d["edges"][0]["node"].update(id="bad-code"),
        lambda r, d: d["edges"][1]["node"].update(id=d["edges"][0]["node"]["id"]),
        lambda r, d: d["edges"][0]["node"]["basicInformation"].update(name=" "),
    ],
)
def test_filtered_wrong_page_partial_and_invalid_identity_responses_do_not_claim_coverage(mutation):
    request, response, data, source = observed()
    mutation(request, data)
    with pytest.raises(ProviderError) as error:
        parse_country_response(request, response, source)
    assert error.value.code == ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("dimensions", [["CN"], ["TW"], ["CN", "TW", "MO"], ["CN", "TW", "MO", "HK", "SG"]])
def test_cn_requires_the_actual_published_broader_country_partition(dimensions):
    request, response, _, source = observed(country="CN", total=855)
    next(term for term in request["variables"]["search"]["facets"]["terms"] if term["type"] == "COUNTRIES")[
        "dimensions"
    ] = dimensions
    with pytest.raises(ProviderError):
        parse_country_response(request, response, source)


def test_single_country_request_cannot_be_relabelled_as_another_country_source():
    request, response, _, _ = observed()
    with pytest.raises(ProviderError):
        parse_country_response(request, response, country_source("SG"))


def test_country_request_cannot_be_relabelled_as_another_pagination_index():
    request, response, _, _ = observed(country="CN", index=1, total=855)
    with pytest.raises(ProviderError):
        parse_country_response(request, response, country_source("CN", index=2))


def test_response_graphql_errors_cannot_be_hidden_by_complete_looking_edges():
    request, response, _, source = observed()
    response["errors"] = [{"message": "resolver failure"}]
    with pytest.raises(ProviderError):
        parse_country_response(request, response, source)


@pytest.mark.parametrize("index,total", [(0, 34), (0, 855), (1, 855), (21, 855)])
def test_current_official_distance_only_sort_keeps_complete_page_and_offsets(index, total):
    # Normal Chrome BT directory on 2026-10-18 returned two real identities
    # with this one-field sort, not the historical four-field tie-break chain.
    request, response, _, source = observed(country="CN" if total == 855 else "TW", index=index, total=total)
    request["variables"]["sort"] = {"fields": [{"field": "DISTANCE", "direction": "ASC"}]}
    result = parse_country_response(request, response, source)
    assert result.page_complete is True
    assert len(result.hotels) == min(40, total - index * 40)
    assert len(result.directory_urls) == int((index + 1) * 40 < total)


@pytest.mark.parametrize(
    "sort",
    [
        {"fields": [{"field": "DISTANCE", "direction": "DESC"}]},
        {"fields": [{"field": "PRICE", "direction": "ASC"}]},
        {"fields": []},
        {"fields": [{"field": "DISTANCE", "direction": "ASC"}], "country": "SG"},
    ],
)
def test_unobserved_or_filter_bearing_sort_still_rejected(sort):
    request, response, _, source = observed()
    request["variables"]["sort"] = sort
    with pytest.raises(ProviderError):
        parse_country_response(request, response, source)


@pytest.mark.parametrize(
    "field,value", [("nextOffset", 79), ("previousOffset", 1), ("hasPreviousPage", False)]
)
def test_middle_page_needs_exact_next_and_previous_offset_contract(field, value):
    request, response, data, source = observed(country="CN", index=1, total=855)
    data["pageInfo"][field] = value
    with pytest.raises(ProviderError):
        parse_country_response(request, response, source)
