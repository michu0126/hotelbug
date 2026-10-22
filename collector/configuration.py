import math
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import yaml

from adapters.base import official_url
from models import Hotel, Stay


@dataclass(frozen=True)
class DateRules:
    start_offset_days: int = 0
    days: int = 365
    nights: int = 1
    adults: int = 2
    rooms: int = 1

    def __post_init__(self):
        if (
            any(type(value) is not int for value in self.__dict__.values())
            or self.start_offset_days < 0
            or not 1 <= self.days <= 365
            or self.nights < 1
            or self.start_offset_days + self.days + self.nights > 366
            or self.adults < 1
            or self.rooms < 1
        ):
            raise ValueError("Date rules must fit the next 365 days and use positive integer occupancy")

    def stays(self, today: date):
        for offset in range(self.start_offset_days, self.start_offset_days + self.days):
            start = today + timedelta(days=offset)
            yield Stay(start, start + timedelta(days=self.nights), self.adults, self.rooms)


@dataclass(frozen=True)
class DetectionPolicy:
    drop_fraction: Decimal = Decimal("0.35")
    min_history_samples: int = 3
    history_days: int = 90
    confirmation_delay_seconds: float = 60
    confirmation_max_age_seconds: float = 1800
    cooldown_hours: float = 24

    def __post_init__(self):
        drop = Decimal(str(self.drop_fraction))
        if (
            not drop.is_finite()
            or not 0 < drop < 1
            or self.min_history_samples < 1
            or self.history_days < 1
            or not all(
                math.isfinite(value)
                for value in (
                    self.confirmation_delay_seconds,
                    self.confirmation_max_age_seconds,
                    self.cooldown_hours,
                )
            )
            or self.confirmation_delay_seconds < 0
            or self.confirmation_max_age_seconds <= self.confirmation_delay_seconds
            or self.cooldown_hours < 24
        ):
            raise ValueError("Invalid detector policy (cooldown must be at least 24 hours)")
        object.__setattr__(self, "drop_fraction", drop)


@dataclass(frozen=True)
class RequestPolicy:
    concurrency_per_brand: int = 1
    delay_min_seconds: float = 1
    delay_max_seconds: float = 3
    max_attempts: int = 3
    backoff_base_seconds: float = 2
    backoff_max_seconds: float = 60
    timeout_seconds: float = 60

    def __post_init__(self):
        values = (
            self.delay_min_seconds,
            self.delay_max_seconds,
            self.backoff_base_seconds,
            self.backoff_max_seconds,
            self.timeout_seconds,
        )
        if (
            not all(math.isfinite(value) for value in values)
            or self.concurrency_per_brand != 1
            or not 1 <= self.max_attempts <= 5
            or self.delay_min_seconds < 0
            or self.delay_max_seconds < self.delay_min_seconds
            or self.backoff_base_seconds < 0
            or self.backoff_max_seconds < self.backoff_base_seconds
            or self.timeout_seconds <= 0
        ):
            raise ValueError("Invalid retry/delay policy; concurrency_per_brand must equal 1")


@dataclass(frozen=True)
class BarkSettings:
    enabled: bool = False
    server: str = "https://api.day.app"
    device_key_env: str = "BARK_DEVICE_KEY"
    group: str = "hotelbug"


@dataclass(frozen=True)
class BrowserSettings:
    headless: bool = False
    channel: str | None = "chrome"
    proxy: str | None = None
    max_rooms_per_search: int = 30
    navigation_timeout_ms: int = 45000

    def __post_init__(self):
        if (
            type(self.headless) is not bool
            or self.channel not in (None, "chrome", "chromium", "msedge")
            or type(self.max_rooms_per_search) is not int
            or not 1 <= self.max_rooms_per_search <= 100
            or type(self.navigation_timeout_ms) is not int
            or not 1000 <= self.navigation_timeout_ms <= 120000
        ):
            raise ValueError("Invalid browser settings")
        if self.proxy:
            from urllib.parse import urlsplit

            parsed = urlsplit(self.proxy)
            if (
                parsed.scheme not in ("http", "https", "socks5")
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError("Browser proxy must be an explicit proxy URL without embedded credentials")


@dataclass(frozen=True)
class Config:
    mode: str = "mock"
    sqlite_path: Path = Path("data/monitor.sqlite")
    loop_interval_seconds: float = 300
    dates: DateRules = field(default_factory=DateRules)
    detector: DetectionPolicy = field(default_factory=DetectionPolicy)
    requests: RequestPolicy = field(default_factory=RequestPolicy)
    bark: BarkSettings = field(default_factory=BarkSettings)
    browser: BrowserSettings = field(default_factory=BrowserSettings)
    watchlist: tuple[Hotel, ...] = ()


def load_config(path: Path) -> Config:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration must be a mapping")
    data = dict(data)
    for key, cls in (
        ("dates", DateRules),
        ("detector", DetectionPolicy),
        ("requests", RequestPolicy),
        ("bark", BarkSettings),
        ("browser", BrowserSettings),
    ):
        data[key] = cls(**data.get(key, {}))
    data["watchlist"] = tuple(Hotel(**item) for item in data.get("watchlist", []))
    for hotel in data["watchlist"]:
        official_url(hotel.brand, hotel.official_url)
    data["sqlite_path"] = (path.resolve().parent / data.get("sqlite_path", "data/monitor.sqlite")).resolve()
    result = Config(**data)
    if (
        result.mode not in ("mock", "live")
        or not math.isfinite(result.loop_interval_seconds)
        or result.loop_interval_seconds <= 0
    ):
        raise ValueError("Invalid mode or loop interval")
    if any(hotel.brand != "hilton" for hotel in result.watchlist):
        raise ValueError("Only Hilton is available in this first stage")
    if result.mode == "live" and (
        result.dates.rooms != 1
        or result.dates.nights != 1
        or any(not re.fullmatch(r"[A-Z0-9]{7}", hotel.hotel_id) for hotel in result.watchlist)
    ):
        raise ValueError("Live Hilton mode requires real seven-character hotel codes, one room and one night")
    return result
