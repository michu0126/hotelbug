"""Settings for the serial, ordinary-page Marriott query scheduler."""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import yaml

from passive_config import load_passive_config


@dataclass(frozen=True)
class AutomaticHotel:
    hotel_id: str
    hotel_url: str
    name: str = ""

    def __post_init__(self):
        parsed = urlsplit(self.hotel_url)
        if (
            not re.fullmatch(r"[A-Z0-9]{5}", self.hotel_id)
            or parsed.scheme != "https"
            or parsed.hostname != "www.marriott.com.cn"
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or not parsed.path.startswith(("/hotels/", "/en-us/hotels/"))
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Use a Marriott hotel overview URL, not a booking or API URL")
        from adapters.marriott_ui import validate_hotel_url

        validate_hotel_url(self.hotel_url, self.hotel_id)


@dataclass(frozen=True)
class AutomaticConfig:
    hotels: tuple[AutomaticHotel, ...]
    sqlite_path: Path = Path("data/automatic-queue.sqlite")
    passive_config: Path = Path("config.passive.yaml")
    profile: Path = Path(".profiles/marriott-passive")
    port: int = 9222
    chrome: str | None = None
    timezone: str = "Asia/Shanghai"
    days: int = 365
    nights: int = 1
    adults: int = 1
    min_delay_seconds: float = 15
    max_delay_seconds: float = 30
    revisit_seconds: float = 86400
    timeout_seconds: float = 90
    capture_timeout_seconds: float = 30
    ingestion_timeout_seconds: float = 20
    max_attempts: int = 3
    backoff_seconds: float = 60
    max_backoff_seconds: float = 900
    idle_seconds: float = 15

    def __post_init__(self):
        if (
            not self.hotels
            or len({h.hotel_id for h in self.hotels}) != len(self.hotels)
            or type(self.port) is not int
            or not 1024 <= self.port <= 65535
            or type(self.days) is not int
            or not 1 <= self.days <= 365
            or type(self.nights) is not int
            or not 1 <= self.nights <= min(30, self.days)
            or type(self.adults) is not int
            or not 1 <= self.adults <= 4
            or type(self.max_attempts) is not int
            or not 1 <= self.max_attempts <= 5
        ):
            raise ValueError("Invalid automatic hotel/date/occupancy settings")
        durations = (
            self.min_delay_seconds,
            self.max_delay_seconds,
            self.revisit_seconds,
            self.timeout_seconds,
            self.capture_timeout_seconds,
            self.ingestion_timeout_seconds,
            self.backoff_seconds,
            self.max_backoff_seconds,
            self.idle_seconds,
        )
        if (
            not all(type(x) in (int, float) and math.isfinite(x) and x > 0 for x in durations)
            or self.max_delay_seconds < self.min_delay_seconds
            or self.min_delay_seconds < 1
            or self.backoff_seconds < 1
            or self.max_backoff_seconds < self.backoff_seconds
        ):
            raise ValueError("Invalid automatic delays or retry limits")
        ZoneInfo(self.timezone)


def load_automatic_config(path: Path) -> AutomaticConfig:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Automatic configuration must be a mapping")
    data = dict(data)
    data["hotels"] = tuple(AutomaticHotel(**item) for item in data.get("hotels", []))
    for name, default in (
        ("sqlite_path", "data/automatic-queue.sqlite"),
        ("passive_config", "config.passive.yaml"),
        ("profile", ".profiles/marriott-passive"),
    ):
        data[name] = (path.parent / data.get(name, default)).resolve()
    settings = AutomaticConfig(**data)
    passive = load_passive_config(settings.passive_config)
    if settings.sqlite_path == passive.sqlite_path:
        raise ValueError("Automatic queue and passive observations must use separate databases")
    if settings.timezone != passive.timezone:
        raise ValueError("Automatic and passive monitor timezones must match")
    if settings.days > passive.days:
        raise ValueError("Passive monitor must cover the automatic date horizon")
    if passive.hotels and not {h.hotel_id for h in settings.hotels}.issubset(
        {h.hotel_id for h in passive.hotels}
    ):
        raise ValueError("Passive watchlist excludes an automatically queried hotel")
    return settings
