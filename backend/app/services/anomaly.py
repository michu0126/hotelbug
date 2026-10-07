"""Same-condition calendar evidence and explainable, configurable scoring."""

from datetime import timedelta
from decimal import Decimal
from statistics import median

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.anomaly_config import AnomalyPolicy
from app.models.tables import Hotel, Rate
from app.schemas.domain import RateData
from app.services.jobs import within_monitoring_window
from app.services.pricing import displayed_price, price_field

CALENDAR_IDENTITY = (
    "provider",
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


def text_amount(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def drop_percent(amount: Decimal, reference: Decimal | None) -> str | None:
    if reference is None or reference <= 0:
        return None
    return str(round((1 - amount / reference) * 100, 2))


def comparison_percentages(amount: Decimal, analysis: dict) -> dict:
    result = dict(analysis)
    for name in ("neighbor_date_median", "same_weekday_median", "month_mean", "previous_price"):
        reference = Decimal(analysis[name]) if analysis.get(name) is not None else None
        result[f"drop_from_{name}_percent"] = drop_percent(amount, reference)
    for window, value in analysis.get("medians", {}).items():
        result[f"drop_from_median_{window}d_percent"] = drop_percent(
            amount, Decimal(value) if value is not None else None
        )
    return result


async def calendar_features(
    session: AsyncSession, hotel: Hotel, rate: RateData, policy: AnomalyPolicy
) -> dict:
    """Only current, fresh, matching room/plan/conditions; never a hotel-wide minimum."""
    month_start = rate.check_in.replace(day=1)
    month_end = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    column = getattr(Rate, price_field(rate))
    conditions = [
        Rate.hotel_id == hotel.id,
        Rate.check_in >= min(month_start, rate.check_in - timedelta(days=28)),
        Rate.check_in < max(month_end, rate.check_in + timedelta(days=29)),
        Rate.check_in != rate.check_in,
        Rate.captured_at <= rate.captured_at,
        Rate.captured_at >= rate.captured_at - timedelta(hours=policy.calendar_max_age_hours),
        Rate.availability.is_(True),
        Rate.points_price.is_(None),
        column > 0,
        *(getattr(Rate, name) == getattr(rate, name) for name in CALENDAR_IDENTITY),
    ]
    if price_field(rate) == "cash_price":
        conditions.append(Rate.total_price.is_(None))
    rows = (await session.scalars(select(Rate).where(*conditions))).all()
    nights = (rate.check_out - rate.check_in).days
    amounts = {
        row.check_in: displayed_price(row)
        for row in rows
        if (row.check_out - row.check_in).days == nights
        and within_monitoring_window(row.check_in, row.check_out)
    }
    adjacent = [
        amounts[d]
        for d in (rate.check_in - timedelta(days=1), rate.check_in + timedelta(days=1))
        if d in amounts
    ]
    weekdays = [v for d, v in amounts.items() if d.weekday() == rate.check_in.weekday()]
    monthly = [v for d, v in amounts.items() if month_start <= d < month_end]
    # Both neighboring nights are needed to call a price below "前后日期".
    return {
        "neighbor_date_median": text_amount(median(adjacent)) if len(adjacent) == 2 else None,
        "neighbor_days": len(adjacent),
        "same_weekday_median": text_amount(median(weekdays)) if len(weekdays) >= 2 else None,
        "same_weekday_days": len(weekdays),
        "month_mean": text_amount(sum(monthly, Decimal(0)) / len(monthly)) if monthly else None,
        "month_days": len(monthly),
    }


def anomaly_score(
    amount: Decimal, analysis: dict, policy: AnomalyPolicy, *, confirmed: bool = False
) -> tuple[int, str, dict[str, int]]:
    """One mutually exclusive drop band and low band; no invented evidence."""
    contributions: dict[str, int] = {}
    baseline = Decimal(analysis["baseline"])
    fraction = 1 - amount / baseline
    for threshold in (70, 50, 40, 30, 20):
        if fraction * 100 > threshold:
            contributions[f"drop_{threshold}"] = getattr(policy, f"drop_{threshold}")
            break
    low = Decimal(analysis["historical_low"]) if analysis.get("historical_low") is not None else None
    if low is not None and amount < low:
        contributions["new_low"] = policy.new_low
    elif low is not None and amount <= low * (1 + policy.near_low_margin):
        contributions["near_low"] = policy.near_low
    neighbor = analysis.get("neighbor_date_median")
    if neighbor is not None and amount <= Decimal(neighbor) * (1 - policy.neighbor_drop_fraction):
        contributions["neighbor"] = policy.neighbor
    medians, counts = analysis.get("medians", {}), analysis.get("history_counts", {})
    comparison_fraction = Decimal(analysis["drop_fraction"])
    if all(
        counts.get(str(window), 0) >= policy.support_min_days
        and medians.get(str(window)) is not None
        and amount <= Decimal(medians[str(window)]) * (1 - comparison_fraction)
        for window in (7, 30)
    ):
        contributions["consistent_history"] = policy.consistent_history
    if confirmed:
        contributions["verified"] = policy.verified
    total = min(100, max(0, sum(contributions.values())))
    level = (
        "POSSIBLE_BUG_PRICE"
        if total >= policy.possible_bug_threshold
        else "VERY_LOW_PRICE"
        if total >= policy.very_low_threshold
        else "LOW_PRICE"
        if total >= policy.low_threshold
        else "NORMAL"
    )
    return total, level, contributions
