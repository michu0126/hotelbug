import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from test_models_adapters import HOTEL, STAY, quote

from adapters.hilton import MockHiltonAdapter
from configuration import BarkSettings, Config, DateRules, RequestPolicy, load_config
from main import Monitor, RunLock
from notifier import BarkNotifier, NotificationUncertain
from runtime import BrandRunner
from storage import Storage

NOW = datetime(2030, 10, 20, 8, tzinfo=timezone.utc)


def config(tmp_path, *, mode="mock", enabled=False):
    return Config(
        mode=mode,
        sqlite_path=tmp_path / "monitor.sqlite",
        dates=DateRules(days=1),
        requests=RequestPolicy(delay_min_seconds=0, delay_max_seconds=0),
        bark=BarkSettings(enabled=enabled),
        watchlist=(HOTEL,),
    )


def seed_history(storage, mock=True):
    for age, amount in ((1, "90"), (2, "100"), (3, "200")):
        storage.save(
            quote(
                amount=amount,
                is_mock=mock,
                observed_at=NOW - timedelta(days=age),
                rate_type="MOCK_PUBLIC_FLEXIBLE",
                room_type="MOCK_STANDARD_ROOM",
            )
        )


async def test_mock_closed_loop_and_restart_confirmation_never_deliver_to_bark(tmp_path):
    clock = [NOW]
    settings = config(tmp_path, enabled=True)  # Even explicit enabled cannot leak a mock push.
    store = Storage(settings.sqlite_path)
    seed_history(store)
    adapter = MockHiltonAdapter(["60", "64"], clock=lambda: clock[0])
    notifier = AsyncMock()
    monitor = Monitor(settings, store, {"hilton": adapter}, notifier=notifier, clock=lambda: clock[0])
    assert await monitor.observe(HOTEL, STAY) == 1
    assert await monitor.confirm_due() == 0
    store.close()
    clock[0] += timedelta(seconds=61)
    store = Storage(settings.sqlite_path)
    monitor = Monitor(settings, store, {"hilton": adapter}, notifier=notifier, clock=lambda: clock[0])
    assert await monitor.confirm_due() == 1
    assert adapter.calls[-1][-1] is True
    assert await monitor.notify_ready() == 0
    notifier.send.assert_not_awaited()
    assert store.db.execute("SELECT status FROM candidates").fetchone()[0] == "DRY_RUN"
    assert store.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_same_series_real_notifications_are_persistently_guarded_24_hours(tmp_path, uncertain):
    settings = config(tmp_path, mode="live", enabled=True)
    store = Storage(settings.sqlite_path)
    now = [NOW]
    notifier = AsyncMock()
    if uncertain:
        notifier.send.side_effect = NotificationUncertain()
    monitor = Monitor(settings, store, {}, notifier=notifier, clock=lambda: now[0])

    def ready():
        first = quote(amount="60", observed_at=now[0])
        second = quote(amount="61", observed_at=now[0])
        identity = store.candidate(first, 100, 65, settings.detector)
        store.finish_candidate(identity, "READY", confirmation=second)

    ready()
    assert await monitor.notify_ready() == (0 if uncertain else 1)
    now[0] += timedelta(hours=23)
    ready()
    assert await monitor.notify_ready() == 0
    assert notifier.send.await_count == 1
    now[0] += timedelta(hours=1)
    ready()
    assert await monitor.notify_ready() == (0 if uncertain else 1)
    assert notifier.send.await_count == 2
    store.close()


async def test_bark_uses_json_post_key_not_url_and_includes_hotel_date_and_price(monkeypatch):
    monkeypatch.setenv("BARK_DEVICE_KEY", "synthetic-device-key")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        notifier = BarkNotifier(BarkSettings(enabled=True), client=client)
        await notifier.send(quote(amount="60"), 100)
        with pytest.raises(ValueError, match="Mock"):
            await notifier.send(quote(is_mock=True), 100)
    assert len(requests) == 1 and str(requests[0].url) == "https://api.day.app/push"
    payload = json.loads(requests[0].content)
    assert payload["device_key"] == "synthetic-device-key"
    assert all(
        text in payload["body"] for text in (HOTEL.name, str(STAY.check_in), "60 USD", "PUBLIC_FLEXIBLE")
    )


async def test_bark_timeout_is_sanitized_without_automatic_resend(monkeypatch):
    monkeypatch.setenv("BARK_DEVICE_KEY", "must-not-appear")

    def fail(request):
        raise httpx.ReadTimeout("must-not-appear", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
        notifier = BarkNotifier(BarkSettings(enabled=True), client=client)
        with pytest.raises(NotificationUncertain) as error:
            await notifier.send(quote(), 100)
        assert "must-not-appear" not in str(error.value)


def test_single_instance_os_lock_is_released(tmp_path):
    path = tmp_path / "monitor.lock"
    with RunLock(path):
        with pytest.raises(RuntimeError):
            with RunLock(path):
                pass
    with RunLock(path):
        pass


def test_example_config_loads_and_year_rules_are_bounded():
    from pathlib import Path

    settings = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert (
        settings.mode == "mock" and not settings.bark.enabled and settings.requests.concurrency_per_brand == 1
    )
    stays = list(DateRules(days=365).stays(NOW.date()))
    assert len(stays) == 365 and stays[-1].check_in == NOW.date() + timedelta(days=364)
    with pytest.raises(ValueError):
        DateRules(days=366)
    with pytest.raises(ValueError):
        replace(settings.requests, concurrency_per_brand=2)


async def test_invalid_adapter_identity_is_not_saved(tmp_path):
    settings = config(tmp_path)
    store = Storage(":memory:")
    adapter = AsyncMock()
    adapter.fetch_rates.return_value = [
        quote(hotel=replace(HOTEL, hotel_id="OTHER"), is_mock=True, observed_at=NOW)
    ]
    runner = BrandRunner(store, settings.requests, clock=lambda: NOW)
    monitor = Monitor(settings, store, {"hilton": adapter}, runner=runner, clock=lambda: NOW)
    with pytest.raises(ValueError, match="mismatched"):
        await monitor.observe(HOTEL, STAY)
    assert store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    store.close()


async def test_missing_bark_key_keeps_confirmed_candidate_without_starting_cooldown(tmp_path, monkeypatch):
    monkeypatch.delenv("BARK_DEVICE_KEY", raising=False)
    settings = config(tmp_path, mode="live", enabled=True)
    store = Storage(":memory:")
    item = quote(amount="60", observed_at=NOW)
    identity = store.candidate(item, 100, 65, settings.detector)
    store.finish_candidate(identity, "READY", confirmation=item)
    monitor = Monitor(settings, store, {}, clock=lambda: NOW)
    assert await monitor.notify_ready() == 0
    assert len(store.ready()) == 1
    assert store.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    store.close()


async def test_confirmation_tick_does_not_poll_the_entire_watchlist(tmp_path):
    settings = config(tmp_path)
    store = Storage(":memory:")
    adapter = AsyncMock()
    monitor = Monitor(settings, store, {"hilton": adapter}, clock=lambda: NOW)
    assert await monitor.tick(poll=False) == {"candidates": 0, "confirmed": 0, "sent": 0}
    adapter.fetch_rates.assert_not_awaited()
    store.close()


async def test_full_synthetic_real_mode_chain_uses_two_fetches_and_one_mocked_bark_post(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BARK_DEVICE_KEY", "synthetic-only")
    settings = config(tmp_path, mode="live", enabled=True)
    store = Storage(settings.sqlite_path)
    clock = [NOW]
    for age, amount in ((3, "90"), (2, "100"), (1, "200")):
        store.save(quote(amount=amount, observed_at=NOW - timedelta(days=age)))
    adapter = AsyncMock()
    adapter.fetch_rates.side_effect = [
        [quote(amount="60", observed_at=NOW)],
        [quote(amount="64", observed_at=NOW + timedelta(seconds=61))],
    ]
    requests = []

    def acknowledge(request):
        requests.append(request)
        return httpx.Response(200, json={"code": 200})

    async with httpx.AsyncClient(transport=httpx.MockTransport(acknowledge)) as client:
        monitor = Monitor(
            settings,
            store,
            {"hilton": adapter},
            clock=lambda: clock[0],
            notifier=BarkNotifier(settings.bark, client=client),
        )
        assert await monitor.observe(HOTEL, STAY) == 1
        assert requests == []
        clock[0] += timedelta(seconds=61)
        assert await monitor.confirm_due() == 1
        assert adapter.fetch_rates.await_count == 2
        assert adapter.fetch_rates.await_args.kwargs["confirmation"] is True
        assert await monitor.notify_ready() == 1
        assert await monitor.notify_ready() == 0
    assert len(requests) == 1
    assert store.db.execute("SELECT status FROM candidates").fetchone()[0] == "SENT"
    store.close()
