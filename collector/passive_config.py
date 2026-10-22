"""Configuration for passive observations, separate from the automatic browser runner."""

import math
import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from configuration import DetectionPolicy


@dataclass(frozen=True)
class WatchedHotel:
    hotel_id: str
    name: str = ""
    currency: str | None = None
    max_total_per_night: Decimal | None = None

    def __post_init__(self):
        if not re.fullmatch(r"[A-Z0-9]{5}", self.hotel_id):
            raise ValueError("A five-character Marriott hotel code is required")
        if self.currency is not None and not re.fullmatch(r"[A-Z]{3}", self.currency):
            raise ValueError("An explicit ISO currency is required")
        if self.max_total_per_night is not None:
            amount = Decimal(str(self.max_total_per_night))
            if not self.currency or not amount.is_finite() or amount <= 0:
                raise ValueError("An absolute cap requires a positive amount and explicit currency")
            object.__setattr__(self, "max_total_per_night", amount)


@dataclass(frozen=True)
class PassiveNotifications:
    channel: str = "console"  # console / bark / telegram; console never makes a network request
    bark_server: str = "https://api.day.app"
    bark_device_key_env: str = "BARK_DEVICE_KEY"
    telegram_token_env: str = "TELEGRAM_BOT_TOKEN"
    telegram_chat_id_env: str = "TELEGRAM_CHAT_ID"

    def __post_init__(self):
        if self.channel not in ("console", "bark", "telegram"):
            raise ValueError("Notification channel must be console, bark or telegram")
        for value in (self.bark_device_key_env, self.telegram_token_env, self.telegram_chat_id_env):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("Notification credentials must refer to environment variable names")


@dataclass(frozen=True)
class PassiveConfig:
    sqlite_path: Path = Path("data/passive-monitor.sqlite")
    captures_dir: Path = Path("../captures/marriott")
    scan_interval_seconds: float = 2
    max_capture_age_seconds: float = 7200
    future_tolerance_seconds: float = 5
    timezone: str = "Asia/Shanghai"
    days: int = 365
    detector: DetectionPolicy = field(
        default_factory=lambda: DetectionPolicy(
            history_days=30,
            min_history_samples=5,
            confirmation_delay_seconds=180,
            confirmation_max_age_seconds=21600,
        )
    )
    notifications: PassiveNotifications = field(default_factory=PassiveNotifications)
    hotels: tuple[WatchedHotel, ...] = ()  # empty = all Marriott hotels actually observed by the listener
    hotel_names: dict[str, str] = field(default_factory=dict)  # Display names; never restrict the watchlist.

    def __post_init__(self):
        if (
            type(self.days) is not int
            or not 1 <= self.days <= 365
            or not all(
                math.isfinite(v)
                for v in (
                    self.scan_interval_seconds,
                    self.max_capture_age_seconds,
                    self.future_tolerance_seconds,
                )
            )
            or self.scan_interval_seconds < 0.1
            or self.max_capture_age_seconds <= 0
            or not 0 <= self.future_tolerance_seconds <= 60
            or len({h.hotel_id for h in self.hotels}) != len(self.hotels)
        ):
            raise ValueError("Invalid passive monitor settings")
        ZoneInfo(self.timezone)
        if not isinstance(self.hotel_names, dict) or any(
            not re.fullmatch(r"[A-Z0-9]{5}", key)
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 200
            for key, value in self.hotel_names.items()
        ):
            raise ValueError("Hotel display names must map official codes to nonempty names")

    def hotel(self, hotel_id):
        return next((h for h in self.hotels if h.hotel_id == hotel_id), None)

    def hotel_name(self, hotel_id):
        hotel = self.hotel(hotel_id)
        return hotel.name if hotel and hotel.name else self.hotel_names.get(hotel_id, hotel_id)


def load_passive_config(path: Path) -> PassiveConfig:
    path = path.resolve()
    data = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Passive configuration must be a mapping")
    data = dict(data)
    defaults = PassiveConfig()
    data["detector"] = DetectionPolicy(**(defaults.detector.__dict__ | data.get("detector", {})))
    data["notifications"] = PassiveNotifications(**data.get("notifications", {}))
    data["hotels"] = tuple(WatchedHotel(**h) for h in data.get("hotels", []))
    for key in ("sqlite_path", "captures_dir"):
        data[key] = (path.parent / data.get(key, str(getattr(defaults, key)))).resolve()
    return PassiveConfig(**data)
