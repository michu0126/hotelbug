"""Freeze a historical median; require a later independent same-series quote."""

from datetime import timedelta
from decimal import Decimal
from statistics import median

from models import Quote


def detect(quote, historical_amounts, policy):
    if len(historical_amounts) < policy.min_history_samples:
        return None
    baseline = median(historical_amounts)
    threshold = baseline * (Decimal(1) - policy.drop_fraction)
    return (baseline, threshold) if quote.amount <= threshold else None


def confirms(first: Quote, second: Quote, threshold: Decimal, policy) -> bool:
    return (
        first.series_key == second.series_key
        and first.observation_id != second.observation_id
        and first.observed_at + timedelta(seconds=policy.confirmation_delay_seconds) <= second.observed_at
        and second.observed_at > first.observed_at
        and second.observed_at <= first.observed_at + timedelta(seconds=policy.confirmation_max_age_seconds)
        and second.amount <= threshold
    )
