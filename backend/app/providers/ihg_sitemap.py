"""IHG's robots-declared public hotel-detail maps; never a price interface."""

import re
from urllib.parse import urlparse
from xml.etree import ElementTree

from app.providers.ihg_page import PROPERTY_PATH

ROOT = "https://www.ihg.com/bin/sitemapindex.xml"
STATE_KEY = "catalog:ihg-sitemap"


def property_code(value: str) -> str | None:
    if not isinstance(value, str):
        return None
    url = urlparse(value)
    identity = PROPERTY_PATH.fullmatch(url.path)
    if url.scheme != "https" or url.netloc != "www.ihg.com" or url.query or url.fragment or not identity:
        return None
    return identity[4].upper()


def sitemap_links(content: bytes, index: bool = False) -> list[str]:
    root = ElementTree.fromstring(content)
    links = set()
    identities = {}
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc":
            continue
        value = (node.text or "").strip()
        url = urlparse(value)
        if url.scheme != "https" or url.netloc != "www.ihg.com" or url.query or url.fragment:
            continue
        if index:
            # One actually advertised English map per brand. Translations,
            # reviews, promotions and reservation pages are not hotel sources.
            if re.fullmatch(r"/bin/sitemap\.[a-z0-9-]+\.en-us\.hoteldetail\.xml", url.path):
                links.add(value)
        else:
            code = property_code(value)
            if code:
                normalized = value.rstrip("/")
                # Several official brand/market aliases can name one property.
                # Deduplicate identity; don't infer hotel name/brand from a map.
                identities[code] = min(normalized, identities.get(code, normalized))
    return sorted(links if index else identities.values())
