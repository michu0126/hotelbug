"""Comparable cash quotes; no website-specific field guesses."""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import uuid4

BRANDS = ("marriott", "ihg", "hyatt", "gha", "hilton", "accor")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Observation timestamps must include a timezone")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class Hotel:
    brand: str
    hotel_id: str
    name: str
    official_url: str

    def __post_init__(self):
        if self.brand not in BRANDS or not self.hotel_id.strip() or not self.name.strip():
            raise ValueError("Brand, hotel ID and hotel name are required")


@dataclass(frozen=True)
class Stay:
    check_in: date
    check_out: date
    adults: int = 2
    rooms: int = 1

    def __post_init__(self):
        if self.check_out <= self.check_in or self.adults < 1 or self.rooms < 1:
            raise ValueError("Stay dates and occupancy must be positive")


@dataclass(frozen=True)
class Quote:
    hotel: Hotel
    stay: Stay
    rate_type: str
    room_type: str
    amount: Decimal
    currency: str
    tax_basis: str  # e.g. TAX_INCLUDED / TAX_EXCLUDED; do not compare different bases
    source_url: str
    is_mock: bool = False
    observed_at: datetime = field(default_factory=utcnow)
    observation_id: str = field(default_factory=lambda: str(uuid4()))
    room_code: str | None = None
    rate_code: str | None = None
    rate_name: str | None = None
    offer_group: str | None = None
    cancellation_policy: str | None = None
    guarantee_policy: str | None = None
    non_refundable: bool | None = None
    deposit_required: bool | None = None
    membership_required: bool | None = None
    rate_description: str | None = None
    after_tax_display: Decimal | None = None
    price_note: str | None = None

    def __post_init__(self):
        amount = Decimal(str(self.amount))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Cash quote must be finite and positive; no-room is not zero")
        if (
            len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
            or not self.currency.isalpha()
        ):
            raise ValueError("Explicit ISO currency is required")
        if (
            not self.rate_type.strip()
            or not self.room_type.strip()
            or self.tax_basis not in ("TAX_INCLUDED", "TAX_EXCLUDED")
        ):
            raise ValueError("Explicit rate type, room type and tax basis are required")
        object.__setattr__(self, "amount", amount)
        object.__setattr__(self, "observed_at", utc(self.observed_at))
        for key in ("non_refundable", "deposit_required", "membership_required"):
            if getattr(self, key) is not None and type(getattr(self, key)) is not bool:
                raise ValueError("Offer condition flags must be explicit booleans")
        if self.after_tax_display is not None:
            after_tax = Decimal(str(self.after_tax_display))
            if not after_tax.is_finite() or after_tax < amount:
                raise ValueError("After-tax reference must be finite and at least the base amount")
            object.__setattr__(self, "after_tax_display", after_tax)

    @property
    def series_key(self) -> str:
        identity = [
            self.hotel.brand,
            self.hotel.hotel_id,
            str(self.stay.check_in),
            str(self.stay.check_out),
            self.stay.adults,
            self.stay.rooms,
            self.rate_code or self.rate_type,
            self.room_code or self.room_type,
            self.currency,
            self.tax_basis,
            self.is_mock,
        ]
        # Keep legacy/mock keys unchanged, but never mix changed cancellation or
        # deposit conditions when a real, code-identified offer is available.
        if self.rate_code or self.room_code:
            identity.extend(
                [
                    self.offer_group,
                    self.cancellation_policy,
                    self.guarantee_policy,
                    self.non_refundable,
                    self.deposit_required,
                    self.membership_required,
                ]
            )
        return sha256(json.dumps(identity, ensure_ascii=True).encode()).hexdigest()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["amount"] = str(self.amount)
        if self.after_tax_display is not None:
            result["after_tax_display"] = str(self.after_tax_display)
        result["observed_at"] = self.observed_at.isoformat()
        result["stay"]["check_in"] = self.stay.check_in.isoformat()
        result["stay"]["check_out"] = self.stay.check_out.isoformat()
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "Quote":
        data = dict(value)
        data["hotel"] = Hotel(**data["hotel"])
        stay = dict(data["stay"])
        for key in ("check_in", "check_out"):
            stay[key] = date.fromisoformat(stay[key])
        data["stay"] = Stay(**stay)
        data["observed_at"] = datetime.fromisoformat(data["observed_at"])
        return cls(**data)


@dataclass(frozen=True)
class CancellationPolicy:
    """Only explicit evidence; a rate name is not a refund contract."""

    free_cancellation_until: str | None = None
    refundable: bool | None = None
    policy_text: str | None = None
    non_refundable: bool | None = None
    deadline_at: datetime | None = None

    def __post_init__(self):
        if self.refundable is not None and type(self.refundable) is not bool:
            raise ValueError("Refundability must be explicit or unknown")
        if self.non_refundable is not None and type(self.non_refundable) is not bool:
            raise ValueError("Non-refundability must be explicit or unknown")
        if self.deadline_at is not None:
            object.__setattr__(self, "deadline_at", utc(self.deadline_at))
        for value in (self.free_cancellation_until, self.policy_text):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError("Cancellation evidence must be nonempty text or unknown")

    def to_dict(self):
        result = asdict(self)
        result["deadline_at"] = self.deadline_at.isoformat() if self.deadline_at else None
        return result

    @classmethod
    def from_dict(cls, value):
        data = dict(value)
        if data.get("deadline_at"):
            data["deadline_at"] = datetime.fromisoformat(data["deadline_at"])
        return cls(**data)

    def comparison_dict(self):
        result = self.to_dict()
        # Preserve pre-existing keys for records without the new evidence.
        for key in ("non_refundable", "deadline_at"):
            if result[key] is None:
                result.pop(key)
        return result

    def free_cancel_now(self, as_of: datetime | None = None) -> bool | None:
        if self.non_refundable is True:
            return False
        if self.deadline_at is None:
            return None
        return utc(as_of or utcnow()) < self.deadline_at


@dataclass(frozen=True)
class PriceRecord:
    """Explicit sample/live stay totals, isolated from automatic Quote monitoring."""

    brand: str
    hotel_id: str
    checkin: date
    checkout: date
    room_code: str
    room_name: str
    rate_code: str
    rate_name: str
    member_rate: bool
    amount_excluding_tax: Decimal
    amount_including_tax: Decimal
    currency: str
    cancellation_policy: CancellationPolicy
    source_url: str
    response_hash: str
    adults: int | None = None
    rooms: int = 1
    price_basis: str = "stay_total"
    source_kind: str = "SAMPLE"
    captured_at: datetime | None = None
    average_nightly_excluding_tax: Decimal | None = None
    mandatory_fees: Decimal | None = None
    tax_amount: Decimal | None = None
    rate_description: str | None = None
    deposit_required: bool | None = None

    def __post_init__(self):
        if (
            self.brand not in BRANDS
            or not self.hotel_id
            or type(self.checkin) is not date
            or type(self.checkout) is not date
            or self.checkout <= self.checkin
            or type(self.member_rate) is not bool
            or type(self.rooms) is not int
            or self.rooms != 1
            or self.amount_excluding_tax is None
            or self.amount_including_tax is None
            or (self.adults is not None and (type(self.adults) is not int or self.adults < 1))
            or self.price_basis != "stay_total"
            or self.source_kind not in ("SAMPLE", "LIVE")
            or (self.source_kind == "LIVE" and (self.captured_at is None or self.adults is None))
            or not isinstance(self.cancellation_policy, CancellationPolicy)
            or (self.deposit_required is not None and type(self.deposit_required) is not bool)
            or any(
                not isinstance(v, str) or not v.strip()
                for v in (self.room_code, self.room_name, self.rate_code, self.rate_name, self.response_hash)
            )
        ):
            raise ValueError("Explicit price identity, source and conditions are required")
        if (
            not isinstance(self.currency, str)
            or len(self.currency) != 3
            or not self.currency.isascii()
            or not self.currency.isupper()
            or not self.currency.isalpha()
        ):
            raise ValueError("Explicit ISO currency is required")
        for key in (
            "amount_excluding_tax",
            "amount_including_tax",
            "average_nightly_excluding_tax",
            "mandatory_fees",
            "tax_amount",
        ):
            value = getattr(self, key)
            if value is None:
                continue
            amount = Decimal(str(value))
            if (
                not amount.is_finite()
                or amount < 0
                or (key not in ("mandatory_fees", "tax_amount") and amount == 0)
            ):
                raise ValueError("Price must be finite and positive; fees may equal zero")
            object.__setattr__(self, key, amount)
        if self.amount_including_tax < self.amount_excluding_tax:
            raise ValueError("Inclusive price cannot be lower than the exclusive price")
        if self.captured_at is not None:
            object.__setattr__(self, "captured_at", utc(self.captured_at))

    @property
    def series_key(self) -> str:
        identity = [
            self.brand,
            self.hotel_id,
            str(self.checkin),
            str(self.checkout),
            self.adults,
            self.rooms,
            self.room_code,
            self.rate_code,
            self.member_rate,
            self.currency,
            self.price_basis,
            self.source_kind,
            self.cancellation_policy.comparison_dict(),
        ]
        # Retain old .com keys, but never mix .cn observations with that source.
        if self.source_site != "www.marriott.com":
            identity.append(self.source_site)
        if self.deposit_required is not None:
            identity.append({"deposit_required": self.deposit_required})
        return sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()

    @property
    def source_site(self):
        return urlsplit(self.source_url).hostname

    def free_cancel_now(self, as_of: datetime | None = None) -> bool | None:
        return self.cancellation_policy.free_cancel_now(as_of)

    @property
    def record_id(self) -> str:
        # Re-reading an export must not create an independent observation.
        value = [
            self.series_key,
            self.response_hash,
            self.captured_at.isoformat() if self.captured_at else None,
        ]
        return sha256(json.dumps(value).encode()).hexdigest()

    def to_dict(self) -> dict:
        result = asdict(self)
        result["cancellation_policy"] = self.cancellation_policy.to_dict()
        for key in (
            "amount_excluding_tax",
            "amount_including_tax",
            "average_nightly_excluding_tax",
            "mandatory_fees",
            "tax_amount",
        ):
            value = getattr(self, key)
            result[key] = str(value) if value is not None else None
        result.update(
            checkin=str(self.checkin),
            checkout=str(self.checkout),
            captured_at=self.captured_at.isoformat() if self.captured_at else None,
        )
        return result

    @classmethod
    def from_dict(cls, value: dict) -> "PriceRecord":
        data = dict(value)
        data["checkin"] = date.fromisoformat(data["checkin"])
        data["checkout"] = date.fromisoformat(data["checkout"])
        data["cancellation_policy"] = CancellationPolicy.from_dict(data["cancellation_policy"])
        if data.get("captured_at"):
            data["captured_at"] = datetime.fromisoformat(data["captured_at"])
        return cls(**data)
