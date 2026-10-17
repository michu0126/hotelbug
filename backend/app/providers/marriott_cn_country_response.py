"""Hotel identities from the normal CN directory's observed public response.

This parses a response issued by the page, not a standalone API request. The
directory has no stay dates: neither cash quotes nor availability are emitted.
Only the observed unfiltered country scope and 40-result pagination are supported.
"""

import re
from urllib.parse import parse_qs, urlencode, urlparse

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_cn_catalog import COUNTRY, COUNTRY_PATH, ORIGIN, directory_url
from app.schemas.domain import CatalogPageData, HotelData

OPERATION = "phoenixShopSearchPropertiesByLocation"
PATH = "/mi/query/" + OPERATION
LIMIT = 40
TERM_TYPES = {
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
}
RANGES = {
    "PRICE": ["0", "100", "200", "overflow"],
    "DISTANCE": ["0", "4830", "14520", "80470"],
}
SORT = {
    "fields": [
        {"field": field, "direction": "ASC"} for field in ("DISTANCE", "CITY", "BRAND", "PROPERTY_NAME")
    ]
}
DISTANCE_SORT = {"fields": [{"field": "DISTANCE", "direction": "ASC"}]}


def parse_country_response(request: dict, response: dict, source: str) -> CatalogPageData:
    """Require exact scope, page offsets and complete distinct hotel identities.

    CN's actual request includes CN/TW/MO/HK. It is a search scope, not address
    evidence, so that broader result never assigns ``country='CN'`` to every hotel.
    ``bookable=False`` does not remove an identity or imply date availability.
    """
    source = directory_url(source)
    invalid = ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott country directory response incomplete")
    try:
        parsed = urlparse(source)
        params = parse_qs(parsed.query)
        if parsed.path != COUNTRY_PATH or COUNTRY not in params:
            raise invalid
        country = params[COUNTRY][0]
        index = int(parsed.fragment.strip("/") or "0")
        offset = index * LIMIT
        variables = request["variables"]
        search = variables["search"]
        options = search["options"]
        if (
            request["operationName"] != OPERATION
            or set(variables) != {"search", "limit", "offset", "sort"}
            or type(variables["limit"]) is not int
            or variables["limit"] != LIMIT
            or type(variables["offset"]) is not int
            or variables["offset"] != offset
            # The normal directory now sends just DISTANCE ASC (observed
            # 2026-10-18); older sessions sent the four-field tie-break chain.
            # Both are ordering only, not a country/brand/date filter.
            or variables["sort"] not in (SORT, DISTANCE_SORT)
            or set(search) != {"options", "facets"}
            or options != {"customerId": "", "numberInParty": 1}
            or type(options["numberInParty"]) is not int
            or set(search["facets"]) != {"terms", "ranges"}
            or response.get("errors")
        ):
            raise invalid
        terms = search["facets"]["terms"]
        countries = ["CN", "TW", "MO", "HK"] if country == "CN" else [country]
        if (
            not isinstance(terms, list)
            or len(terms) != len(TERM_TYPES)
            or {term["type"] for term in terms} != TERM_TYPES
            or any(
                set(term) != {"type", "dimensions"}
                or term["dimensions"] != (countries if term["type"] == "COUNTRIES" else [])
                for term in terms
            )
        ):
            raise invalid
        ranges = search["facets"]["ranges"]
        if (
            not isinstance(ranges, list)
            or len(ranges) != len(RANGES)
            or {item["type"] for item in ranges} != set(RANGES)
            or any(
                set(item) != {"type", "dimensions", "endpoints"}
                or item["dimensions"] != []
                or item["endpoints"] != RANGES[item["type"]]
                for item in ranges
            )
        ):
            raise invalid
        data = response["data"]["search"]["properties"]["searchByLocation"]
        statuses = data["status"]
        total = data["total"]
        if (
            type(total) is not int
            or not offset < total
            or not isinstance(statuses, list)
            or not statuses
            or any(
                status["code"] != "SUCCESS" or status["httpStatus"] != 200 or status.get("errors")
                for status in statuses
            )
        ):
            raise invalid
        page = data["pageInfo"]
        has_next = offset + LIMIT < total
        if (
            type(page["currentOffset"]) is not int
            or page["currentOffset"] != offset
            or page["hasNextPage"] is not has_next
            or page["hasPreviousPage"] is not (offset > 0)
            or page["nextOffset"] != (offset + LIMIT if has_next else None)
            or page["previousOffset"] != (offset - LIMIT if offset else None)
            or (has_next and type(page["nextOffset"]) is not int)
            or (offset and type(page["previousOffset"]) is not int)
        ):
            raise invalid
        edges = data["edges"]
        if not isinstance(edges, list) or len(edges) != min(LIMIT, total - offset):
            raise invalid
        hotels = {}
        for edge in edges:
            node = edge["node"]
            code, name = node["id"], node["basicInformation"]["name"]
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Z0-9]{5}", code)
                or code in hotels
                or not isinstance(name, str)
                or not 1 <= len(name.strip()) <= 300
            ):
                raise invalid
            # This is the published link structure observed on real cards. A
            # directory identity link is not proof that any dated rate exists.
            official_url = (
                ORIGIN
                + "/search/availabilityCalendar.mi?"
                + urlencode(
                    {"isRateCalendar": "true", "propertyCode": code, "isSearch": "true", "currency": ""}
                )
            )
            hotels[code] = HotelData(
                provider="marriott",
                provider_hotel_id=code,
                hotel_name=name.strip(),
                country=None if country == "CN" else country,
                official_url=official_url,
            )
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise invalid from exc
    return CatalogPageData(
        provider="marriott",
        source_url=source,
        hotels=list(hotels.values()),
        reported_total=total,
        directory_urls=[source.split("#", 1)[0] + f"#/{index + 1}/"] if has_next else [],
        complete=offset == 0 and not has_next,
        page_complete=True,
    )
