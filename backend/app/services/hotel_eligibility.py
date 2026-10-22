"""Exclude corroborated official QA properties from automatic monitoring only.

Keep catalog identities, active flags, watchlists and historical observations.
The AQA code family alone or a name substring alone is not sufficient evidence.
SQL and Python use the same small normalization vocabulary for both databases.
"""

from sqlalchemy import and_, func, or_

from app.models.tables import Hotel
from app.schemas.domain import JobKind
from app.services.catalog_price_source import ROOTS, SOURCE_MARKER

SEPARATORS = "-_/.(),:;\t\r\n\u00a0"
TEST_WORDS = ("test", "testing", "training", "empower")
EXCLUDED_REASON = "Official test/training hotel excluded from automatic monitoring"


def is_test_hotel(hotel) -> bool:
    code = hotel.provider_hotel_id.upper()
    name = " " + hotel.hotel_name.lower().translate(str.maketrans(dict.fromkeys(SEPARATORS, " "))) + " "
    return (
        hotel.provider == "marriott"
        and len(code) == 5
        and code.startswith("AQA")
        and (any(f" {word} " in name for word in TEST_WORDS) or "测试" in name or "培训" in name)
    )


def test_hotel_predicate():
    name = func.lower(Hotel.hotel_name)
    for separator in SEPARATORS:
        name = func.replace(name, separator, " ")
    name = " " + name + " "
    return and_(
        Hotel.provider == "marriott",
        func.length(Hotel.provider_hotel_id) == 5,
        func.upper(func.substr(Hotel.provider_hotel_id, 1, 3)) == "AQA",
        or_(
            *(name.contains(f" {word} ") for word in TEST_WORDS), name.contains("测试"), name.contains("培训")
        ),
    )


def automatic_hotel_predicate():
    """Apply before bounded keyset/rotation limits, not after selecting a batch."""
    return ~test_hotel_predicate()


def automatic_job_excluded(job, hotel) -> bool:
    if not hotel or not is_test_hotel(hotel) or job.payload.get("connection_test") is True:
        return False
    if job.kind == JobKind.PROVIDER_HEALTHCHECK:
        return job.payload.get("scheduled_probe") is True
    if job.kind == JobKind.FETCH_CALENDAR_HINTS:
        return True
    return job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY) and (
        job.payload.get(SOURCE_MARKER) in ROOTS or job.payload.get("calendar_signal") is True
    )
