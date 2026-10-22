"""Synthetic event/price changes to a sanitized fixture; no hotel requests or real push."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

import passive_capture as pc
from adapters.marriott import parse_capture
from configuration import DetectionPolicy
from notifier import NotificationUncertain, TelegramNotifier
from passive_config import PassiveConfig, PassiveNotifications, WatchedHotel, load_passive_config
from passive_monitor import CaptureWatcher, PassiveMonitor, cancel_text
from passive_storage import PassiveStorage

NOW = datetime(2026, 10, 1, 8, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/marriott_cn_NYCTE_sanitized.json"


@pytest.fixture
def capture():
    original = json.loads(FIXTURE.read_text(encoding="utf-8"))
    connection = original["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]
    edge = next(e for e in connection["edges"] if not e["node"]["basicInformation"]["isMembersOnly"])
    connection["edges"], connection["total"] = [edge], 1

    def make(amount="100", when=NOW, *, event_id=None):
        document = deepcopy(original)
        node = document["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]["edges"][0][
            "node"
        ]
        amount = int(Decimal(amount) * 100)
        modes = node["totalPricing"]["rateModes"]
        for key in ("grandTotal", "subtotalPerQuantity"):
            modes[key]["amount"].update(amount=amount, decimalPoint=2)
        modes["totalMandatoryFeesPerQuantity"]["amount"].update(amount=0, decimalPoint=2)
        node["rates"]["rateModes"]["averageNightlyRatePerUnit"]["amount"].update(
            amount=amount, decimalPoint=2
        )
        document["captured_at"] = when.isoformat()
        document["capture_event"] = {
            "version": 1,
            "collector_id": uuid4().hex,
            "event_id": event_id or hashlib.sha256(uuid4().bytes).hexdigest(),
        }
        return document

    return make


def settings(tmp_path, **kwargs):
    return PassiveConfig(
        sqlite_path=tmp_path / "passive.sqlite", captures_dir=tmp_path / "captures", **kwargs
    )


def seed(monitor, capture):
    for age in range(1, 6):
        assert monitor.ingest(capture("100", NOW - timedelta(days=age)))["status"] == "history_only"


async def test_history_confirm_restart_and_single_grouped_delivery(tmp_path, capture):
    config = settings(tmp_path, notifications=PassiveNotifications(channel="telegram"))
    clock = [NOW]
    notifier = AsyncMock(configured=True)
    store = PassiveStorage(config.sqlite_path)
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    seed(monitor, capture)
    first = capture("65")
    assert monitor.ingest(first)["candidates"] == 1  # Inclusive 35% boundary.
    assert await monitor.notify_ready() == {"sent": 0, "dry_run": 0, "unknown": 0}
    notifier.send_message.assert_not_awaited()
    assert monitor.ingest(first)["status"] == "duplicate"
    store.close()

    clock[0] += timedelta(minutes=3)
    store = PassiveStorage(config.sqlite_path)
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    assert monitor.ingest(capture("65", clock[0]))["confirmed"] == 1
    assert (await monitor.notify_ready())["sent"] == 1
    assert (await monitor.notify_ready())["sent"] == 0
    assert notifier.send_message.await_count == 1
    title, body, link = notifier.send_message.call_args.args
    assert "NYCTE" in title and "2026-10-18" in body and "65.00 USD" in body
    assert "历史每日最低价中位数" in body and "35.0%" in body
    assert "www.marriott.com.cn/reservation/" in link and "numberOfGuests=1" in link
    assert store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    assert store.report()["alerts"] == {"SENT": 1}
    store.close()


def test_timestamp_edit_is_conflict_not_independent_confirmation(tmp_path, capture):
    store = PassiveStorage(":memory:")
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: NOW)
    seed(monitor, capture)
    document = capture("60")
    monitor.ingest(document)
    modified = deepcopy(document)
    modified["captured_at"] = (NOW + timedelta(minutes=3)).isoformat()
    monitor.clock = lambda: NOW + timedelta(minutes=3)
    assert monitor.ingest(modified)["status"] == "event_conflict"
    assert store.report()["alerts"] == {"PENDING": 1}
    assert store.report()["observations"] == 6
    store.close()


def test_five_collection_days_not_five_refreshes(tmp_path, capture):
    store = PassiveStorage(":memory:")
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: NOW)
    for seconds in range(8):
        monitor.ingest(capture("100", NOW - timedelta(days=1, seconds=seconds)))
    assert monitor.ingest(capture("1"))["candidates"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM passive_daily_min").fetchone()[0] == 2
    store.close()


@pytest.mark.parametrize("change", ["source", "currency", "adults", "membership", "cancellation", "deposit"])
def test_comparison_dimensions_never_share_history(tmp_path, capture, change):
    store = PassiveStorage(":memory:")
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: NOW)
    seed(monitor, capture)
    document = capture("1")
    node = document["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]["edges"][0]["node"]
    if change == "source":
        document["source_site"] = "www.marriott.com"
    elif change == "adults":
        document["variables"]["search"]["options"]["numberInParty"] = 2
    elif change == "membership":
        node["basicInformation"]["isMembersOnly"] = True
    elif change == "cancellation":
        node["basicInformation"]["freeCancellationUntil"] = "2026-10-01T00:00:00Z"
    elif change == "deposit":
        node["basicInformation"]["depositRequired"] = not node["basicInformation"]["depositRequired"]
    else:

        def replace_currency(value):
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key == "currency":
                        value[key] = "EUR"
                    else:
                        replace_currency(nested)
            elif isinstance(value, list):
                for nested in value:
                    replace_currency(nested)

        replace_currency(document["response"])
    assert monitor.ingest(document)["candidates"] == 0
    store.close()


async def test_recovery_after_confirmation_before_delivery_invalidates_ready(tmp_path, capture):
    store = PassiveStorage(":memory:")
    clock = [NOW]
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: clock[0])
    seed(monitor, capture)
    monitor.ingest(capture("60"))
    clock[0] += timedelta(minutes=3)
    assert monitor.ingest(capture("60", clock[0]))["confirmed"] == 1
    clock[0] += timedelta(minutes=1)
    assert monitor.ingest(capture("100", clock[0]))["rejected"] == 1
    assert (await monitor.notify_ready())["dry_run"] == 0
    # An older low-price file arriving late cannot undo the recovery.
    assert monitor.ingest(capture("50", NOW + timedelta(minutes=3, seconds=30)))["candidates"] == 0
    assert store.report()["alerts"] == {"REJECTED": 1}
    store.close()


async def test_absolute_rule_and_console_are_explicit_dry_run(tmp_path, capture, capsys):
    config = settings(tmp_path, hotels=(WatchedHotel("NYCTE", "测试酒店", "USD", Decimal("70")),))
    clock = [NOW]
    store = PassiveStorage(":memory:")
    notifier = AsyncMock()
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    assert monitor.ingest(capture("60"))["candidates"] == 1
    clock[0] += timedelta(minutes=3)
    assert monitor.ingest(capture("60", clock[0]))["confirmed"] == 1
    assert (await monitor.notify_ready())["dry_run"] == 1
    output = capsys.readouterr().out
    assert "测试酒店" in output and "设定上限 70.00/晚" in output and "历史" not in output
    assert "取消未知" in output
    notifier.send_message.assert_not_awaited()
    assert store.db.execute("SELECT COUNT(*) FROM passive_deliveries").fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("case", ["too_early", "recovered", "expired", "old"])
def test_invalid_second_observation_cannot_confirm(tmp_path, capture, case):
    store = PassiveStorage(":memory:")
    clock = [NOW]
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: clock[0])
    seed(monitor, capture)
    monitor.ingest(capture("60"))
    if case == "too_early":
        clock[0] += timedelta(seconds=179)
        result = monitor.ingest(capture("60", clock[0]))
    elif case == "recovered":
        clock[0] += timedelta(minutes=3)
        result = monitor.ingest(capture("66", clock[0]))
        assert result["rejected"] == 1
    elif case == "expired":
        clock[0] += timedelta(hours=7)
        result = monitor.ingest(capture("60", clock[0]))
    else:
        clock[0] += timedelta(hours=3)
        result = monitor.ingest(capture("60", NOW + timedelta(minutes=3)))
        assert result["status"] == "history_only"
    assert result["confirmed"] == 0
    assert not store.ready_passive()
    store.close()


@pytest.mark.parametrize("uncertain", [False, True])
async def test_strict_24h_cooldown_even_after_large_drop_and_restart(tmp_path, capture, uncertain):
    config = settings(tmp_path, notifications=PassiveNotifications(channel="telegram"))
    clock = [NOW]
    store = PassiveStorage(config.sqlite_path)
    notifier = AsyncMock(configured=True)
    if uncertain:
        notifier.send_message.side_effect = NotificationUncertain("sanitized")
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    seed(monitor, capture)
    monitor.ingest(capture("60"))
    clock[0] += timedelta(minutes=3)
    monitor.ingest(capture("60", clock[0]))
    delivery = await monitor.notify_ready()
    assert delivery["unknown" if uncertain else "sent"] == 1
    store.close()
    clock[0] += timedelta(hours=23)
    store = PassiveStorage(config.sqlite_path)
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    assert monitor.ingest(capture("10", clock[0]))["candidates"] == 0
    assert (await monitor.notify_ready())["sent"] == 0
    assert notifier.send_message.await_count == 1
    clock[0] += timedelta(hours=1)
    assert monitor.ingest(capture("10", clock[0]))["candidates"] == 1
    clock[0] += timedelta(minutes=3)
    monitor.ingest(capture("10", clock[0]))
    await monitor.notify_ready()
    assert notifier.send_message.await_count == 2
    store.close()


async def test_missing_credentials_keep_ready_without_cooldown(tmp_path, capture):
    config = settings(tmp_path, notifications=PassiveNotifications(channel="telegram"))
    clock = [NOW]
    store = PassiveStorage(":memory:")
    notifier = AsyncMock(configured=False)
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])
    seed(monitor, capture)
    monitor.ingest(capture("60"))
    clock[0] += timedelta(minutes=3)
    monitor.ingest(capture("60", clock[0]))
    assert (await monitor.notify_ready())["sent"] == 0
    assert len(store.ready_passive()) == 1
    assert store.db.execute("SELECT COUNT(*) FROM passive_deliveries").fetchone()[0] == 0
    clock[0] += timedelta(hours=3)
    await monitor.notify_ready()
    assert store.report()["alerts"] == {"EXPIRED": 1}
    store.close()


async def test_legacy_sample_only_and_future_capture_rejected(tmp_path, capture):
    store = PassiveStorage(":memory:")
    monitor = PassiveMonitor(settings(tmp_path), store, clock=lambda: NOW)
    seed(monitor, capture)
    old = capture("1")
    old.pop("capture_event")
    assert monitor.ingest(old)["status"] == "legacy_sample_only"
    assert store.report()["legacy_samples"] == 1
    assert store.report()["observations"] == 5
    assert not store.ready_passive()
    with pytest.raises(ValueError, match="Future"):
        monitor.ingest(capture("1", NOW + timedelta(seconds=10)))
    with pytest.raises(ValueError):
        corrupt = capture("1")
        corrupt["capture_event"]["event_id"] = "arbitrary value"
        monitor.ingest(corrupt)
    assert (await monitor.notify_ready())["sent"] == 0
    store.close()


async def test_directory_watcher_imports_once_and_does_not_replay_copied_file(tmp_path, capture):
    config = settings(tmp_path)
    config.captures_dir.mkdir()
    document = capture()
    path = config.captures_dir / "first.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    store = PassiveStorage(":memory:")
    watcher = CaptureWatcher(PassiveMonitor(config, store, clock=lambda: NOW))
    assert (await watcher.tick())["files"][0]["status"] == "ok"
    assert (await watcher.tick())["files"] == []
    (config.captures_dir / "copy.json").write_bytes(path.read_bytes())
    assert (await watcher.tick())["files"][0]["status"] == "duplicate"
    assert store.report()["observations"] == 1
    store.close()


async def test_grouped_public_and_member_alert_uses_one_message(tmp_path, capture):
    config = settings(tmp_path, notifications=PassiveNotifications(channel="telegram"))
    store = PassiveStorage(":memory:")
    clock = [NOW]
    notifier = AsyncMock(configured=True)
    monitor = PassiveMonitor(config, store, notifier=notifier, clock=lambda: clock[0])

    def two(amount, when):
        record = capture(amount, when)
        conn = record["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]
        member = deepcopy(conn["edges"][0])
        member["node"]["basicInformation"]["isMembersOnly"] = True
        conn["edges"].append(member)
        conn["total"] = 2
        return record

    for age in range(1, 6):
        monitor.ingest(two("100", NOW - timedelta(days=age)))
    assert monitor.ingest(two("60", NOW))["candidates"] == 2
    clock[0] += timedelta(minutes=3)
    assert monitor.ingest(two("60", clock[0]))["confirmed"] == 2
    assert (await monitor.notify_ready())["sent"] == 1
    assert notifier.send_message.await_count == 1
    body = notifier.send_message.call_args.args[1]
    assert "公开价" in body and "会员价" in body
    assert store.report()["alerts"] == {"SENT": 2}
    store.close()


def test_storage_rolls_back_entire_event_on_failure(tmp_path, capture):
    store = PassiveStorage(":memory:")
    record = replace(parse_capture(capture())[0], source_kind="LIVE")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        # A duplicate series inside one event is an internal pipeline fault.
        store.ingest("event", "digest", [record, record], settings(tmp_path), NOW, eligible=True)
    assert store.report()["events"] == 0 and store.report()["observations"] == 0
    assert store.db.execute("SELECT COUNT(*) FROM passive_daily_min").fetchone()[0] == 0
    assert store.report()["alerts"] == {}
    store.close()


async def test_invalid_file_does_not_break_watcher_or_echo_private_content(tmp_path, capture, capsys):
    config = settings(tmp_path)
    config.captures_dir.mkdir()
    (config.captures_dir / "bad.json").write_text('{"captured_at":{"private":"synthetic-secret"}}')
    (config.captures_dir / "valid.json").write_text(json.dumps(capture()), encoding="utf-8")
    store = PassiveStorage(":memory:")
    watcher = CaptureWatcher(PassiveMonitor(config, store, clock=lambda: NOW))
    result = await watcher.tick()
    assert {r["status"] for r in result["files"]} == {"unreadable_file", "ok"}
    assert "synthetic-secret" not in capsys.readouterr().out
    assert (await watcher.tick())["files"] == []
    store.close()


@pytest.mark.parametrize("limit", ["dates", "hotel"])
def test_outside_watch_scope_is_stored_without_alert(tmp_path, capture, limit):
    config = (
        settings(tmp_path, days=1)
        if limit == "dates"
        else settings(tmp_path, hotels=(WatchedHotel("OTHER"),))
    )
    store = PassiveStorage(":memory:")
    monitor = PassiveMonitor(config, store, clock=lambda: NOW)
    seed(monitor, capture)
    result = monitor.ingest(capture("1"))
    assert result["status"] == "history_only" and result["candidates"] == 0
    store.close()


def test_event_identity_dedup_retains_independent_same_price(tmp_path, capture):
    first = capture("60")
    second = capture("60", NOW + timedelta(minutes=3))
    assert pc.content_hash(first) == pc.content_hash(second)
    seen = set()
    a = pc.save_record(first, tmp_path, seen)
    b = pc.save_record(second, tmp_path, seen)
    assert a is not None and b is not None and a != b
    assert pc.save_record(first, tmp_path, seen) is None
    assert len(list(tmp_path.glob("*.json"))) == 2
    assert not list(tmp_path.glob("*.tmp"))
    saved = json.loads(b.read_text(encoding="utf-8"))
    assert saved["capture_event"] == second["capture_event"]


def test_unknown_policy_is_unknown_and_money_remains_decimal(capture):
    document = capture("65.01")
    record = parse_capture(document)[0]
    assert record.amount_including_tax == Decimal("65.01")
    assert record.tax_amount is None
    assert record.free_cancel_now(NOW) is None
    assert "未知" in cancel_text(record, NOW)


def test_config_defaults_and_validation(tmp_path):
    config = load_passive_config(Path(__file__).resolve().parents[1] / "config.passive.yaml")
    assert config.days == 365 and config.notifications.channel == "console"
    assert config.detector.min_history_samples == 5 and config.detector.cooldown_hours == 24
    assert config.captures_dir.is_absolute() and config.sqlite_path.is_absolute()
    with pytest.raises(ValueError):
        WatchedHotel("NYCTE", max_total_per_night="1")
    with pytest.raises(ValueError):
        replace(config, days=366)
    with pytest.raises(ValueError):
        replace(config, detector=DetectionPolicy(cooldown_hours=23))


@pytest.mark.parametrize("response_kind", ["ok", "denied", "timeout"])
async def test_telegram_official_payload_and_sanitized_failures(monkeypatch, response_kind, caplog):
    token = "123456789:synthetic-only-test-token-not-real"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    caplog.set_level("INFO", logger="httpx")
    requests = []

    def respond(request):
        requests.append(request)
        if response_kind == "timeout":
            raise httpx.ReadTimeout("private " + token, request=request)
        return httpx.Response(200, json={"ok": response_kind == "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        notifier = TelegramNotifier(PassiveNotifications(channel="telegram"), client=client)
        if response_kind == "ok":
            await notifier.send_message("测试标题", "测试正文", "https://www.marriott.com.cn/")
        else:
            with pytest.raises(NotificationUncertain) as error:
                await notifier.send_message("测试标题", "测试正文")
            assert token not in str(error.value) and token not in repr(error.value)
    assert len(requests) == 1
    assert requests[0].url.host == "api.telegram.org"
    assert requests[0].url.path.endswith("/sendMessage")
    payload = json.loads(requests[0].content)
    assert payload["chat_id"] == "synthetic-chat" and "测试标题" in payload["text"]
    assert payload["link_preview_options"]["is_disabled"] is True
    assert token not in caplog.text


async def test_capture_singleton_lock_does_not_start_or_close_browser(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from main import RunLock

    async def fake_run(args):
        with pytest.raises(RuntimeError):
            with RunLock(Path(args.out) / ".capture.lock"):
                pass

    monkeypatch.setattr(pc, "_run", fake_run)
    await pc.run(SimpleNamespace(out=tmp_path))
    with RunLock(tmp_path / ".capture.lock"):
        pass  # The operating system lock was released.
