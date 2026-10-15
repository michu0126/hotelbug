"""Convert a server Retry-After hint without changing scheduler minimum waits."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil


def retry_after_seconds(value: str | None, *, now: datetime | None = None) -> int | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    # Match the existing browser-provider bound and avoid impossible timestamps
    # from malformed, unbounded integer headers. HTTP delay-seconds use ASCII.
    if value.isascii() and value.isdecimal() and len(value) <= 9:
        return int(value)
    try:
        until = parsedate_to_datetime(value)
        if until.tzinfo is None:
            return None
        current = now if now is not None else datetime.now(timezone.utc)
        return max(0, ceil((until - current).total_seconds()))
    except (ValueError, TypeError, OverflowError):
        return None
