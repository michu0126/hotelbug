from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from test_models_adapters import quote

from configuration import DetectionPolicy
from detector import confirms, detect
from storage import Storage

NOW = datetime(2030, 10, 18, 8, tzinfo=timezone.utc)
POLICY = DetectionPolicy()


@pytest.mark.parametrize("amount,triggered", [("65", True), ("64.99", True), ("65.01", False), ("99", False)])
def test_at_least_35_percent_below_historical_median(amount, triggered):
    result = detect(quote(amount=amount), [Decimal("90"), Decimal("100"), Decimal("200")], POLICY)
    assert bool(result) is triggered
    if result:
        assert result == (100, 65)


def test_history_excludes_current_future_expired_and_other_series(tmp_path):
    store = Storage(tmp_path / "history.sqlite")
    current = quote(amount="60", observed_at=NOW)
    for age, amount in ((1, "90"), (2, "100"), (3, "200"), (100, "900"), (-1, "1"), (0, "60")):
        store.save(quote(amount=amount, observed_at=NOW - timedelta(days=age)))
    store.save(replace(current, currency="EUR"))
    store.save(current)
    assert sorted(store.history(current, 90)) == [90, 100, 200]
    assert detect(current, store.history(current, 90), POLICY) == (100, 65)
    assert detect(current, [Decimal("100")], POLICY) is None
    store.close()


@pytest.mark.parametrize(
    "invalid",
    ["same_id", "same_time", "early", "expired", "currency", "rate", "tax", "price", "mock", "stay"],
)
def test_confirmation_requires_fresh_independent_same_series_quote(invalid):
    first = quote(amount="60", observed_at=NOW)
    second = quote(amount="64", observed_at=NOW + timedelta(seconds=61))
    changes = {
        "same_id": {"observation_id": first.observation_id},
        "same_time": {"observed_at": NOW},
        "early": {"observed_at": NOW + timedelta(seconds=59)},
        "expired": {"observed_at": NOW + timedelta(hours=1)},
        "currency": {"currency": "EUR"},
        "rate": {"rate_type": "MEMBER"},
        "tax": {"tax_basis": "TAX_EXCLUDED"},
        "price": {"amount": "66"},
        "mock": {"is_mock": True},
        "stay": {"stay": replace(first.stay, adults=1)},
    }
    assert confirms(first, second, Decimal("65"), POLICY)
    assert not confirms(first, replace(second, **changes[invalid]), Decimal("65"), POLICY)


def test_candidates_pauses_and_24_hour_notification_guard_survive_restart(tmp_path):
    path = tmp_path / "monitor.sqlite"
    first = quote(amount="60", observed_at=NOW)
    store = Storage(path)
    identity = store.candidate(first, Decimal("100"), Decimal("65"), POLICY)
    assert identity and store.candidate(first, Decimal("100"), Decimal("65"), POLICY) is None
    store.pause("hilton", "HTTP_403")
    store.close()
    store = Storage(path)
    assert store.paused("hilton", NOW + timedelta(days=10))
    assert store.due(NOW + timedelta(seconds=59)) == []
    candidate = store.due(NOW + timedelta(seconds=60))[0]
    assert candidate["id"] == identity
    assert store.reserve_notification(candidate, NOW, 24) == "RESERVED"
    store.close()  # Simulate process loss after sending starts, before acknowledgment.
    store = Storage(path)
    assert store.reserve_notification(candidate, NOW + timedelta(hours=1), 24) == "UNKNOWN"
    store.notification_result(first.series_key, NOW, 24, success=True)
    assert store.reserve_notification(candidate, NOW + timedelta(hours=23, minutes=59), 24) == "COOLDOWN"
    assert store.reserve_notification(candidate, NOW + timedelta(hours=24), 24) == "RESERVED"
    store.resume("hilton")
    assert not store.paused("hilton", NOW)
    store.close()
