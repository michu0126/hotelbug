"""Observed public worldwide directory response, not a standalone licensed API.

The normal directory page requests countsOnly=true. This response contains
country buckets, not hotel identities or dated offers. Never emit RateData.
"""

import re
from urllib.parse import urlencode

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_cn_catalog import COUNTRY, COUNTRY_PATH, DESTINATION, ORIGIN, ROOT, directory_url
from app.schemas.domain import CatalogPageData

OPERATION = "phoenixShopSearchPropertiesByLocationInput"
PATH = "/mi/query/" + OPERATION


def parse_worldwide_response(request: dict, response: dict) -> CatalogPageData:
    """Verify the unfiltered request and the full country-count partition."""
    invalid = ProviderError(ErrorCode.INVALID_RESPONSE, "Marriott worldwide directory response incomplete")
    try:
        search = request["variables"]["search"]
        if (
            request["operationName"] != OPERATION
            or set(search) != {"options", "facets"}
            or search["options"] != {"countsOnly": True}
            or search["options"]["countsOnly"] is not True
            or set(search["facets"]) != {"terms"}
            or not search["facets"]["terms"]
            or any(
                set(t) != {"type", "dimensions"} or t["dimensions"] != [] for t in search["facets"]["terms"]
            )
            or "COUNTRIES" not in {t["type"] for t in search["facets"]["terms"]}
            or response.get("errors")
        ):
            raise invalid
        data = response["data"]["search"]["properties"]["searchByLocation"]
        total = data["total"]
        statuses = data["status"]
        if (
            type(total) is not int
            or total < 1
            or not statuses
            or any(s["code"] != "SUCCESS" or s["httpStatus"] != 200 or s.get("errors") for s in statuses)
        ):
            raise invalid
        facets = [f for f in data["facets"] if f["type"]["code"] == "countries"]
        if len(facets) != 1:
            raise invalid
        facet = facets[0]
        buckets = facet["buckets"]
        if not buckets or type(facet["type"]["count"]) is not int or len(buckets) != facet["type"]["count"]:
            raise invalid
        countries = {}
        count = 0
        for bucket in buckets:
            code, label, size = bucket["code"], bucket["label"], bucket["count"]
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"[A-Z]{2}", code)
                or not isinstance(label, str)
                or not label.strip()
                or type(size) is not int
                or size < 1
                or code in countries
            ):
                raise invalid
            countries[code] = directory_url(
                ORIGIN
                + COUNTRY_PATH
                + "?"
                + urlencode(
                    {
                        DESTINATION: label,
                        COUNTRY: code,
                        "searchType": "InCity",
                        "deviceType": "desktop-web",
                    }
                )
            )
            count += size
        if count != total:
            raise invalid
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise invalid from exc
    return CatalogPageData(
        provider="marriott",
        source_url=ROOT,
        directory_urls=list(countries.values()),
        reported_total=total,
        complete=False,
        page_complete=True,
    )
