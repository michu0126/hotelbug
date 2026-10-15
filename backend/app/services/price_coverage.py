"""Rolling stay-observation coverage, not hotel-directory or room-offer counts.

The denominator is active collected hotels on the selected price source, never
the provider's uncollected worldwide catalog. Current adapters query one stay
per job. The rate-limit calculations are optimistic bounds, not an ETA or a
claim that browser throughput can meet the configured refresh intervals.
"""

from datetime import timedelta, timezone
from urllib.parse import urlparse

from sqlalchemy import and_, case, func, or_, select, true

from app.models.tables import CrawlJob, Hotel, PriceHistory, StayScan, utcnow
from app.schemas.domain import ProviderName
from app.services.catalog_price_source import SOURCE_MARKER, load_catalog_price_source

SHANGHAI = timezone(timedelta(hours=8))
OBSERVED_STATUSES = ("AVAILABLE", "UNAVAILABLE", "NOT_OPEN")


def one_night_condition(dialect_name: str):
    # PostgreSQL date + integer is a date; SQLite needs explicit date arithmetic.
    # Do not fetch millions of rows merely to subtract Python dates.
    if dialect_name == "sqlite":
        return func.julianday(StayScan.check_out) - func.julianday(StayScan.check_in) == 1
    return StayScan.check_out == StayScan.check_in + 1


def observation_source_condition(source):
    """Do not relabel old observations when a hotel's catalog URL changes.

    A tagged automatic job establishes the source for a successful no-inventory
    result. A manual/legacy available result can instead establish it through
    the actual persisted quote URLs. Unknown provenance stays outside coverage.
    """
    if source.root is None:
        return true()
    origin = "https://" + urlparse(source.root).hostname + "/%"
    quote_identity = (
        PriceHistory.job_id == StayScan.job_id,
        PriceHistory.hotel_id == StayScan.hotel_id,
        PriceHistory.provider == "marriott",
        PriceHistory.check_in == StayScan.check_in,
        PriceHistory.check_out == StayScan.check_out,
        PriceHistory.adults == StayScan.adults,
        PriceHistory.rooms == StayScan.rooms,
    )
    matching_quote = (
        select(PriceHistory.id).where(*quote_identity, PriceHistory.source_url.like(origin)).exists()
    )
    other_quote = (
        select(PriceHistory.id).where(*quote_identity, ~PriceHistory.source_url.like(origin)).exists()
    )
    marked_job = (
        select(CrawlJob.id)
        .where(
            CrawlJob.id == StayScan.job_id,
            CrawlJob.hotel_id == StayScan.hotel_id,
            CrawlJob.provider == "marriott",
            CrawlJob.status == "SUCCEEDED",
            CrawlJob.kind.in_(("FETCH_RATE", "FETCH_CALENDAR", "VERIFY_ANOMALY")),
            CrawlJob.check_in == StayScan.check_in,
            CrawlJob.check_out == StayScan.check_out,
            CrawlJob.payload[SOURCE_MARKER].as_string() == source.root,
        )
        .exists()
    )
    return or_(
        Hotel.provider != "marriott",
        and_(~other_quote, or_(marked_job, and_(StayScan.status == "AVAILABLE", matching_quote))),
    )


async def price_coverage_by_provider(session, settings, now=None) -> dict[str, dict]:
    """Use two grouped queries, independent of the number of hotel/date rows.

    A StayScan is one successful observed stay, including explicit unavailable
    and not-yet-open responses. Queued jobs, room count, and historical offers
    never increase this coverage. Exactly-at-cutoff observations are due again,
    matching the scheduler's freshness policy. Future timestamps are ignored.
    """
    now = now or utcnow()
    now = now.replace(tzinfo=now.tzinfo or timezone.utc).astimezone(timezone.utc)
    today = now.astimezone(SHANGHAI).date()
    end = today + timedelta(days=365)
    providers = [provider.value for provider in ProviderName]
    source = await load_catalog_price_source(session)
    verified_source = observation_source_condition(source)
    hotel_scope = (
        Hotel.active.is_(True),
        Hotel.provider.in_(providers),
        source.hotel_predicate(),
    )
    hotel_counts = dict(
        (
            await session.execute(
                select(Hotel.provider, func.count()).where(*hotel_scope).group_by(Hotel.provider)
            )
        ).all()
    )
    cutoff = case(
        (StayScan.check_in <= today + timedelta(days=30), now - timedelta(hours=settings.hot_interval_hours)),
        (
            StayScan.check_in <= today + timedelta(days=90),
            now - timedelta(hours=settings.warm_interval_hours),
        ),
        else_=now - timedelta(hours=settings.cold_interval_hours),
    )
    observations = (
        await session.execute(
            select(
                Hotel.provider,
                StayScan.status,
                func.sum(case((verified_source, 1), else_=0)),
                func.sum(case((and_(verified_source, StayScan.observed_at > cutoff), 1), else_=0)),
                func.sum(case((~verified_source, 1), else_=0)),
            )
            .join(Hotel, Hotel.id == StayScan.hotel_id)
            .where(
                *hotel_scope,
                StayScan.check_in >= today,
                StayScan.check_in < end,
                StayScan.check_out <= end,
                one_night_condition(session.get_bind().dialect.name),
                StayScan.adults == 2,
                StayScan.rooms == 1,
                StayScan.status.in_(OBSERVED_STATUSES),
                StayScan.observed_at <= now,
            )
            .group_by(Hotel.provider, StayScan.status)
        )
    ).all()
    counts = {
        (provider, status): (observed, fresh)
        for provider, status, observed, fresh, _unverified in observations
    }
    result = {}
    for provider in providers:
        hotels = hotel_counts.get(provider, 0)
        required = hotels * 365
        previous = sum(counts.get((provider, status), (0, 0))[0] for status in OBSERVED_STATUSES)
        fresh_by_status = {status: counts.get((provider, status), (0, 0))[1] for status in OBSERVED_STATUSES}
        fresh = sum(fresh_by_status.values())
        policy = settings.provider_policy(provider)
        # 0..30, 31..90, 91..364 are respectively 31, 60, and 274 check-in dates.
        demand = (
            hotels
            * 24
            * (
                31 / settings.hot_interval_hours
                + 60 / settings.warm_interval_hours
                + 274 / settings.cold_interval_hours
            )
        )
        ceiling = 86400 / policy.interval_seconds
        result[provider] = {
            "scope": "ACTIVE_COLLECTED_HOTELS_CURRENT_SOURCE",
            "query_mode": "SINGLE_STAY",
            "timezone": "Asia/Shanghai",
            "window_start": today.isoformat(),
            "window_end_exclusive": end.isoformat(),
            "adults": 2,
            "rooms": 1,
            "nights": 1,
            "global_monitoring_enabled": settings.global_monitoring_enabled,
            "provider_enabled": policy.enabled,
            "eligible_hotels": hotels,
            "required_stays": required,
            "previously_scanned_stays": previous,
            "fresh_stays": fresh,
            "stale_stays": previous - fresh,
            "unscanned_stays": required - previous,
            "pending_stays": required - fresh,
            "source_unverified_stays": sum(
                unverified
                for name, _status, _observed, _fresh, unverified in observations
                if name == provider
            ),
            "fresh_available_stays": fresh_by_status["AVAILABLE"],
            "fresh_unavailable_stays": fresh_by_status["UNAVAILABLE"],
            "fresh_not_open_stays": fresh_by_status["NOT_OPEN"],
            "collected_first_pass_complete": bool(required and previous == required),
            "collected_fresh_coverage_complete": bool(required and fresh == required),
            "freshness_hours": {
                "hot": settings.hot_interval_hours,
                "warm": settings.warm_interval_hours,
                "cold": settings.cold_interval_hours,
            },
            "required_refresh_jobs_per_day": round(demand, 3),
            "provider_start_ceiling_per_day": round(ceiling, 3),
            "minimum_first_pass_start_span_seconds": max(0, required - 1) * policy.interval_seconds,
            "refresh_demand_exceeds_start_ceiling": demand > ceiling,
        }
    return result
