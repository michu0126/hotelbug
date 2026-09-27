import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class ProviderName(StrEnum):
    MARRIOTT = "marriott"
    IHG = "ihg"
    HYATT = "hyatt"
    HILTON = "hilton"
    ACCOR = "accor"
    GHA = "gha"


class JobKind(StrEnum):
    DISCOVER_HOTELS = "DISCOVER_HOTELS"
    FETCH_RATE = "FETCH_RATE"
    FETCH_CALENDAR = "FETCH_CALENDAR"
    VERIFY_ANOMALY = "VERIFY_ANOMALY"
    PROVIDER_HEALTHCHECK = "PROVIDER_HEALTHCHECK"


class HotelData(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: ProviderName
    provider_hotel_id: str = Field(min_length=1, max_length=128)
    hotel_name: str = Field(min_length=1, max_length=300)
    brand: str | None = None
    country: str | None = None
    region: str | None = None
    city: str | None = None
    address: str | None = None
    latitude: Decimal | None = Field(default=None, ge=-90, le=90)
    longitude: Decimal | None = Field(default=None, ge=-180, le=180)
    default_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    official_url: HttpUrl
    active: bool = True


class RateRequest(BaseModel):
    provider_hotel_id: str
    check_in: date
    check_out: date
    adults: int = Field(default=2, ge=1, le=8)
    rooms: int = Field(default=1, ge=1, le=4)

    @model_validator(mode="after")
    def dates(self) -> Self:
        if not 1 <= (self.check_out - self.check_in).days <= 30:
            raise ValueError("stay must be 1–30 nights")
        return self


class RateData(RateRequest):
    model_config = ConfigDict(extra="forbid")
    provider: ProviderName
    room_type: str | None = None
    room_code: str | None = None
    rate_name: str | None = None
    rate_code: str | None = None
    cash_price: Decimal | None = Field(default=None, ge=0)
    tax: Decimal | None = Field(default=None, ge=0)
    total_price: Decimal | None = Field(default=None, ge=0)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    points_price: int | None = Field(default=None, ge=0)
    refundable: bool | None = None
    breakfast_included: bool | None = None
    member_rate: bool | None = None
    availability: bool
    price_basis: str = Field(default="stay_total", pattern=r"^(stay_total|nightly)$")
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source_url: HttpUrl
    raw_response_hash: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def amounts(self) -> Self:
        if (
            self.availability
            and self.cash_price is None
            and self.total_price is None
            and self.points_price is None
        ):
            raise ValueError("available rate must have an explicit amount")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")
        if self.cash_price is not None and self.tax is not None and self.total_price is not None:
            if abs(self.cash_price + self.tax - self.total_price) > Decimal("0.05"):
                raise ValueError("inconsistent tax and total")
        return self

    def offer_key(self) -> str:
        fields = (
            "provider",
            "provider_hotel_id",
            "check_in",
            "check_out",
            "room_type",
            "room_code",
            "rate_name",
            "rate_code",
            "currency",
            "adults",
            "rooms",
            "member_rate",
            "refundable",
            "breakfast_included",
            "price_basis",
        )
        data = self.model_dump(mode="json")
        return hashlib.sha256(json.dumps({k: data[k] for k in fields}, sort_keys=True).encode()).hexdigest()


class HealthResult(BaseModel):
    status: str = Field(pattern=r"^(ONLINE|DEGRADED|BLOCKED|BROKEN)$")
    message: str


class JobInput(BaseModel):
    provider: ProviderName
    kind: JobKind
    hotel_id: str | None = None
    check_in: date | None = None
    check_out: date | None = None
    priority: int = Field(default=50, ge=0, le=100)
    payload: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def required_dates(self) -> Self:
        if self.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
            if not self.hotel_id or not self.check_in or not self.check_out:
                raise ValueError("hotel and dates required")
            if not 1 <= (self.check_out - self.check_in).days <= 30:
                raise ValueError("invalid stay")
        return self
