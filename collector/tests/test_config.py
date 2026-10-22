from dataclasses import replace

import pytest

from configuration import DateRules, DetectionPolicy, RequestPolicy


@pytest.mark.parametrize("fraction", ["0", "1", "-0.1", "NaN", "Infinity"])
def test_invalid_drop_fraction_is_rejected(fraction):
    with pytest.raises(ValueError):
        DetectionPolicy(drop_fraction=fraction)


@pytest.mark.parametrize(
    "changes",
    [
        {"cooldown_hours": 23},
        {"min_history_samples": 0},
        {"confirmation_delay_seconds": -1},
        {"confirmation_max_age_seconds": 30},
        {"confirmation_delay_seconds": float("nan")},
    ],
)
def test_invalid_confirmation_and_cooldown_policy_is_rejected(changes):
    with pytest.raises(ValueError):
        DetectionPolicy(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"concurrency_per_brand": 2},
        {"max_attempts": 100},
        {"delay_max_seconds": -1},
        {"timeout_seconds": 0},
        {"delay_min_seconds": float("inf")},
    ],
)
def test_request_limits_cannot_be_bypassed_with_config(changes):
    with pytest.raises(ValueError):
        RequestPolicy(**changes)


def test_invalid_date_types_and_windows_are_rejected():
    for changes in ({"days": 1.5}, {"days": True}, {"start_offset_days": -1}, {"days": 365, "nights": 2}):
        with pytest.raises(ValueError):
            replace(DateRules(), **changes)
