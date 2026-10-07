"""Public GHA hotel candidates, excluding observed corporate/promotion sections."""

import re
from urllib.parse import urlparse

ROOT = "https://www.ghadiscovery.com"
# These are non-property sections actually present in the public sitemap, not
# a whitelist of hotel brands. New hotel brands must still be discoverable.
NON_HOTEL_SECTIONS = {
    "additional-brand-benefits",
    "blog",
    "booking",
    "buy-discovery-dollars",
    "buy-discovery-dollars-for-red-members",
    "click-the-crystal-ball-d-promotion",
    "complimentary-breakfast",
    "destination-guides",
    "destinations",
    "dine-with-d-at-the-wolseley-hospitality-group",
    "fast-track-to-higher-status",
    "kyros-promotion",
    "member",
    "our-partners",
    "promotions",
    "regional-brand-d-promotions",
    "scavenger-hunt-2026",
    "test-landing-page",
}
HOTEL_OFFER_SECTIONS = {"stay-offers", "local-offers", "experiences", "dining"}


def hotel_candidate_url(value: str) -> str | None:
    """A candidate only; the Worker must still verify public hotel/booking identity."""
    if not isinstance(value, str):
        return None
    url = urlparse(value)
    if url.scheme != "https" or url.netloc != "www.ghadiscovery.com" or url.query or url.fragment:
        return None
    parts = url.path.strip("/").split("/")
    if len(parts) > 2 and parts[2] in HOTEL_OFFER_SECTIONS:
        # A real property's offer advertises its parent detail page.
        parts = parts[:2]
    if (
        len(parts) != 2
        or parts[0] in NON_HOTEL_SECTIONS
        or not all(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", part) for part in parts)
        or re.fullmatch(r"(?:sweepstakes-)?terms-and-conditions[0-9]*", parts[1])
    ):
        return None
    return ROOT + "/" + "/".join(parts)


def candidate_progress(urls: list[str], cursor: int) -> dict[str, int]:
    """Count hotel candidates, not the old raw cursor's corporate entries."""
    candidates = [hotel_candidate_url(url) for url in urls]
    return {
        "hotel_candidates": len({url for url in candidates if url is not None}),
        "candidate_tasks_dispatched": len({url for url in candidates[: max(0, cursor)] if url is not None}),
        "excluded_non_hotel_entries": sum(url is None for url in candidates),
    }
