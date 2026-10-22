from typing import Protocol
from urllib.parse import urlsplit

from models import Hotel, Quote, Stay

OFFICIAL_DOMAINS = {
    "hilton": ("hilton.com",),
    "marriott": ("marriott.com", "marriott.com.cn"),
    "ihg": ("ihg.com", "ihg.com.cn"),
    "hyatt": ("hyatt.com",),
    "gha": ("ghadiscovery.com",),
    "accor": ("accor.com",),
}


class SamplesRequired(NotImplementedError):
    pass


class BrandBlocked(Exception):
    """403 or visible challenge: record and pause, never retry around it."""


class RateLimited(Exception):
    def __init__(self, retry_after: float = 3600):
        self.retry_after = retry_after
        super().__init__("Official site rate limit")


class TransientFailure(Exception):
    pass


def official_url(brand: str, url: str) -> str:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not any(host == domain or host.endswith("." + domain) for domain in OFFICIAL_DOMAINS[brand])
    ):
        raise ValueError("Use an HTTPS official hotel-group URL")
    return url


class Adapter(Protocol):
    async def fetch_rates(self, hotel: Hotel, stay: Stay, *, confirmation: bool = False) -> list[Quote]: ...

    async def close(self) -> None: ...


class PendingAdapter:
    brand = ""

    async def fetch_rates(self, hotel: Hotel, stay: Stay, *, confirmation: bool = False) -> list[Quote]:
        raise SamplesRequired(f"{self.brand}: public-page samples must be reviewed before live collection")

    async def close(self) -> None:
        pass
