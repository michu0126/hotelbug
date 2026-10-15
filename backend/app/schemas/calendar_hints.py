"""Public calendar minima are search hints, never confirmed room offers."""

import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal, Self
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

SHANGHAI = timezone(timedelta(hours=8))


def _next_month(day: date) -> date:
    return date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)


def month_window(month: str, today: date | None = None) -> tuple[date, date] | None:
    """Intersect a natural month with the next 365 eligible check-in dates.

    The end is exclusive. Today plus 364 days is the final check-in, allowing
    its one-night checkout to remain inside the 365-day monitoring boundary.
    """
    if not isinstance(month, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
        raise ValueError("month must be YYYY-MM")
    try:
        start = date(int(month[:4]), int(month[5:]), 1)
        end = _next_month(start)
    except ValueError as exc:
        raise ValueError("month must be YYYY-MM") from exc
    today = today if today is not None else datetime.now(timezone.utc).astimezone(SHANGHAI).date()
    start, end = max(start, today), min(end, today + timedelta(days=365))
    return (start, end) if start < end else None


class CalendarHintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_hotel_id: str = Field(pattern=r"^[A-Z0-9]{5}$")
    start_date: date
    end_date: date
    adults: Literal[2] = 2
    rooms: Literal[1] = 1

    @model_validator(mode="after")
    def month_range(self) -> Self:
        if not 1 <= (self.end_date - self.start_date).days <= 31:
            raise ValueError("calendar hint window must contain 1–31 dates")
        if self.end_date > _next_month(self.start_date):
            raise ValueError("calendar hint window must stay in one natural month")
        return self


class CalendarHintDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    check_in: date
    displayed_price: Decimal = Field(gt=0, allow_inf_nan=False)


class CalendarHintBatch(CalendarHintRequest):
    provider: Literal["marriott"] = "marriott"
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    basis: Literal["CALENDAR_MINIMUM_TAX_UNCONFIRMED"] = "CALENDAR_MINIMUM_TAX_UNCONFIRMED"
    source_url: HttpUrl
    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_response_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    days: list[CalendarHintDay] = Field(min_length=1, max_length=31)

    @model_validator(mode="after")
    def evidence(self) -> Self:
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be timezone-aware")
        parsed = urlparse(str(self.source_url))
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.scheme != "https"
            or parsed.hostname != "www.marriott.com.cn"
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.path != "/search/availabilityCalendar.mi"
            or set(query) - {"showFullPrice"} != {"isRateCalendar", "propertyCode", "isSearch", "currency"}
            or any(len(values) != 1 for values in query.values())
            or query.get("isRateCalendar") != ["true"]
            or query.get("isSearch") != ["true"]
            or query.get("currency") != [""]
            or query.get("showFullPrice", ["false"]) != ["false"]
            or query.get("propertyCode") != [self.provider_hotel_id]
        ):
            raise ValueError("calendar hint source must identify the same official Marriott CN hotel")
        dates = [day.check_in for day in self.days]
        if len(set(dates)) != len(dates):
            raise ValueError("duplicate calendar hint date")
        if any(not self.start_date <= day < self.end_date for day in dates):
            raise ValueError("calendar hint date outside requested window")
        return self
