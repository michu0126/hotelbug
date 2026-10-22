"""Offline runtime integration: real queue/database, sanitized responses, no hotel traffic."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import yaml

import automatic_monitor as automatic
from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott import parse_capture
from adapters.marriott_ui import DateNotSelectable
from automatic_config import AutomaticConfig, AutomaticHotel, load_automatic_config
from automatic_queue import Queue
from main import RunLock
from passive_capture import save_record
from passive_config import PassiveConfig
from passive_monitor import PassiveMonitor
from passive_storage import PassiveStorage

NOW = datetime(2026, 10, 18, 8, tzinfo=timezone.utc)
FIXTURE = Path(__file__).parent / "fixtures/marriott_cn_NYCTE_sanitized.json"
HOTEL = AutomaticHotel("NYCTE", "https://www.marriott.com.cn/hotels/nycte-test-hotel/overview/", "Test")


@pytest.fixture
def document():
    value = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["captured_at"] = NOW.isoformat()
    value["capture_event"] = {
        "version": 1,
        "event_id": sha256(uuid4().bytes).hexdigest(),
        "collector_id": uuid4().hex,
    }
    return value


def response_summary(document):
    return {
        "status": "ok",
        "hotel_id": document["property_id"],
        "checkin": document["check_in"],
        "checkout": document["check_out"],
        "adults": document["variables"]["search"]["options"]["numberInParty"],
        "source_site": document["source_site"],
        "offers": len(parse_capture(document)),
        "response_hash": sha256(
            json.dumps(document["response"], sort_keys=True, ensure_ascii=True).encode()
        ).hexdigest(),
    }


def query_task():
    return SimpleNamespace(
        hotel_id="NYCTE", checkin=date(2026, 10, 18), checkout=date(2026, 10, 19), adults=1
    )


def test_display_names_do_not_restrict_passive_capture_scope():
    config = PassiveConfig(hotel_names={"XIYBP": "西安钟楼福朋喜来登酒店"})
    assert config.hotels == ()
    assert config.hotel_name("XIYBP") == "西安钟楼福朋喜来登酒店"
    assert config.hotel_name("NYCTE") == "NYCTE"


def write_capture(directory, document, suffix="test"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (f"www.marriott.com.cn_NYCTE_2026-10-18_2026-10-19_{suffix}.json")
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


@pytest.fixture
def environment(tmp_path):
    settings = AutomaticConfig(
        hotels=(HOTEL,),
        sqlite_path=tmp_path / "queue.sqlite",
        days=1,
        min_delay_seconds=1,
        max_delay_seconds=1,
        backoff_seconds=1,
        max_backoff_seconds=3,
        timeout_seconds=1,
        capture_timeout_seconds=0.01,
        ingestion_timeout_seconds=0.01,
    )
    passive = PassiveConfig(sqlite_path=tmp_path / "passive.sqlite", captures_dir=tmp_path / "captures")
    queue = Queue(settings.sqlite_path)
    store = PassiveStorage(passive.sqlite_path)
    clock = [NOW]
    monitor = PassiveMonitor(passive, store, clock=lambda: clock[0])
    yield SimpleNamespace(
        settings=settings, passive=passive, queue=queue, store=store, clock=clock, monitor=monitor
    )
    queue.close()
    store.close()


def make_runtime(environment, driver, services=None):
    return automatic.AutomaticRuntime(
        environment.settings,
        environment.passive,
        environment.queue,
        driver,
        services=services,
        clock=lambda: environment.clock[0],
    )


async def test_unselectable_future_date_is_deferred_not_zero_price(environment):
    driver = SimpleNamespace(query=AsyncMock(side_effect=DateNotSelectable()))
    runtime = make_runtime(environment, driver)
    result = await runtime.query_one()
    assert result["status"] == "date_not_selectable"
    assert environment.queue.report()["successful_queries"] == 0
    assert environment.queue.report()["states"] == {"WAITING": 1}
    assert environment.store.db.execute("SELECT COUNT(*) FROM passive_observations").fetchone()[0] == 0
    assert environment.queue.claim(NOW + timedelta(minutes=1)) is None


async def test_stop_request_prevents_new_browser_actions(environment):
    driver = SimpleNamespace(query=AsyncMock())
    runtime = make_runtime(environment, driver)
    environment.queue.request_stop()
    with pytest.raises(asyncio.CancelledError):
        await runtime.query_one()
    driver.query.assert_not_awaited()
    environment.queue.clear_stop()
    assert not environment.queue.stop_requested()


def successful_driver(environment, document, *, ingest=True):
    async def query(task):
        saved = deepcopy(document)
        saved["captured_at"] = environment.clock[0].isoformat()
        saved["capture_event"]["event_id"] = sha256(uuid4().bytes).hexdigest()
        save_record(saved, environment.passive.captures_dir, set())
        if ingest:
            environment.monitor.ingest(saved)
        return response_summary(saved)

    return SimpleNamespace(query=AsyncMock(side_effect=query))


def test_new_complete_capture_matches_same_second_query(tmp_path, document):
    task = query_task()
    path = write_capture(tmp_path, document)
    result = automatic.find_capture(
        tmp_path, task, NOW + timedelta(microseconds=500), set(), response_summary(document), NOW
    )
    assert result["path"] == path
    assert result["offers"] == 67


def test_old_capture_and_duplicate_event_do_not_complete_new_query(tmp_path, document):
    task = query_task()
    result = response_summary(document)
    write_capture(tmp_path, document)
    baseline = automatic.capture_baseline(tmp_path, task)
    assert baseline == {document["capture_event"]["event_id"]}
    assert automatic.find_capture(tmp_path, task, NOW, baseline, result, NOW) is None
    write_capture(tmp_path, document, "copied")
    assert automatic.find_capture(tmp_path, task, NOW, baseline, result, NOW) is None
    assert automatic.find_capture(tmp_path, task, NOW + timedelta(seconds=1), set(), result, NOW) is None


@pytest.mark.parametrize("bad_document", [None, [], "not an envelope", 123])
def test_non_object_json_files_are_ignored(tmp_path, document, bad_document):
    write_capture(tmp_path, bad_document)
    task = query_task()
    assert automatic.capture_baseline(tmp_path, task) == set()
    assert automatic.find_capture(tmp_path, task, NOW, set(), response_summary(document), NOW) is None


@pytest.mark.parametrize(
    "mismatch",
    ["hotel", "date", "adults", "rooms", "source", "hash", "offers", "future", "no_event", "incomplete"],
)
def test_capture_rejects_mismatched_context(tmp_path, document, mismatch):
    result = response_summary(document)
    if mismatch == "hotel":
        document["property_id"] = "XIYBP"
    elif mismatch == "date":
        document["check_in"] = "2026-10-19"
    elif mismatch == "adults":
        document["variables"]["search"]["options"]["numberInParty"] = 2
    elif mismatch == "rooms":
        document["variables"]["search"]["options"]["quantity"] = 2
    elif mismatch == "source":
        result["source_site"] = "www.marriott.com"
    elif mismatch == "hash":
        result["response_hash"] = "0" * 64
    elif mismatch == "offers":
        result["offers"] += 1
    elif mismatch == "future":
        document["captured_at"] = (NOW + timedelta(seconds=6)).isoformat()
    elif mismatch == "no_event":
        document.pop("capture_event")
    else:
        document["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]["total"] += 1
    write_capture(tmp_path, document)
    assert automatic.find_capture(tmp_path, query_task(), NOW, set(), result, NOW) is None


async def test_wait_missing_capture_times_out_without_request(tmp_path, document):
    with pytest.raises(TransientFailure):
        await automatic.wait_for_capture(
            tmp_path, query_task(), NOW, set(), response_summary(document), timeout=0.01, clock=lambda: NOW
        )


async def test_success_requires_fresh_file_and_database_ingestion(environment, document):
    driver = successful_driver(environment, document)
    runtime = make_runtime(environment, driver)
    result = await runtime.run(once=True)
    assert result["status"] == "ok" and result["offers"] == 67
    assert automatic.event_ingested(environment.passive.sqlite_path, result["event_id"])
    assert environment.queue.report()["successful_queries"] == 1
    assert environment.store.report()["events"] == 1
    row = environment.queue.db.execute("SELECT status,offers,event_id FROM automatic_results").fetchone()
    assert tuple(row) == ("PRICED", 67, result["event_id"])
    assert (await runtime.query_one())["status"] == "idle"
    assert driver.query.await_count == 1


async def test_saved_but_unprocessed_capture_is_not_success(environment, document):
    runtime = make_runtime(environment, successful_driver(environment, document, ingest=False))
    assert (await runtime.run(once=True))["status"] == "retry"
    assert environment.queue.report()["successful_queries"] == 0
    assert environment.queue.db.execute("SELECT COUNT(*) FROM automatic_results").fetchone()[0] == 0


async def test_successful_click_without_capture_is_not_success(environment, document):
    driver = SimpleNamespace(query=AsyncMock(return_value=response_summary(document)))
    assert (await make_runtime(environment, driver).run(once=True))["status"] == "retry"
    assert environment.queue.report()["successful_queries"] == 0


def sold_out_result():
    return {
        "status": "no_availability",
        "offers": 0,
        "hotel_id": "NYCTE",
        "checkin": "2026-10-18",
        "checkout": "2026-10-19",
        "adults": 1,
        "source_site": "www.marriott.com.cn",
        "evidence": "official_sold_out_redirect_and_matching_hotel_card",
    }


async def test_sold_out_is_separate_status_not_zero_price(environment):
    driver = SimpleNamespace(query=AsyncMock(return_value=sold_out_result()))
    assert (await make_runtime(environment, driver).run(once=True))["status"] == "no_availability"
    row = environment.queue.db.execute("SELECT status,offers,event_id FROM automatic_results").fetchone()
    assert tuple(row) == ("NO_AVAILABILITY", 0, None)
    assert environment.queue.report()["successful_queries"] == 1
    assert environment.store.report()["events"] == 0
    assert environment.store.db.execute("SELECT COUNT(*) FROM passive_observations").fetchone()[0] == 0


@pytest.mark.parametrize(
    "key,value",
    [
        ("hotel_id", "XIYBP"),
        ("adults", 2),
        ("evidence", "timeout"),
        ("checkin", "2026-10-19"),
        ("source_site", "example.com"),
        ("offers", 1),
    ],
)
async def test_unproven_sold_out_is_not_success(environment, key, value):
    result = sold_out_result() | {key: value}
    driver = SimpleNamespace(query=AsyncMock(return_value=result))
    assert (await make_runtime(environment, driver).run(once=True))["status"] == "retry"
    assert environment.queue.report()["successful_queries"] == 0


async def test_empty_valid_response_does_not_insert_zero_price(environment, document):
    connection = document["response"]["data"]["commerce"]["product"]["searchProductsByProperty"]
    connection.update(total=0, edges=[])
    result = await make_runtime(environment, successful_driver(environment, document)).run(once=True)
    assert result["status"] == "ok" and result["offers"] == 0
    assert environment.store.report()["events"] == 0
    assert (
        environment.queue.db.execute("SELECT status FROM automatic_results").fetchone()[0] == "NO_CASH_OFFERS"
    )


@pytest.mark.parametrize("error,status", [(BrandBlocked("403"), "paused"), (RateLimited(60), "rate_limited")])
async def test_blocking_persists_brand_pause_and_requires_explicit_resume(environment, error, status):
    driver = SimpleNamespace(query=AsyncMock(side_effect=error))
    runtime = make_runtime(environment, driver)
    assert (await runtime.run(once=True))["status"] == status
    assert Queue.read_report(environment.settings.sqlite_path)["paused"] is True
    assert (await runtime.query_one())["status"] == "paused"
    assert driver.query.await_count == 1
    environment.queue.resume(NOW)
    driver.query.side_effect = None
    driver.query.return_value = sold_out_result()
    assert (await runtime.query_one())["status"] == "no_availability"
    assert not environment.queue.report()["paused"]


async def test_transient_retries_stop_at_configured_limit(environment):
    driver = SimpleNamespace(query=AsyncMock(side_effect=TransientFailure("temporary")))
    runtime = make_runtime(environment, driver)
    for attempt in range(1, 4):
        result = await runtime.query_one()
        assert result["status"] == ("retry" if attempt < 3 else "failed")
        if attempt < 3:
            assert (await runtime.query_one())["status"] == "idle"
            environment.clock[0] = datetime.fromisoformat(result["next_due"])
    assert environment.queue.report()["states"] == {"FAILED": 1}
    assert driver.query.await_count == 3
    assert (await runtime.query_one())["status"] == "idle"


async def test_listener_loss_cancels_current_ui_query(environment):
    stopped = asyncio.Event()
    entered = asyncio.Event()

    async def query(task):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def check():
        if entered.is_set():
            raise RuntimeError("listener stopped")

    runtime = make_runtime(environment, SimpleNamespace(query=query), SimpleNamespace(check=check))
    with pytest.raises(RuntimeError, match="listener stopped"):
        await runtime.query_one()
    assert stopped.is_set()
    assert environment.queue.report()["states"] == {"WAITING": 1}
    assert environment.queue.report()["successful_queries"] == 0


async def test_cancel_releases_task_without_closing_browser(environment):
    started = asyncio.Event()

    async def query(task):
        started.set()
        await asyncio.Event().wait()

    driver = SimpleNamespace(query=query, close=AsyncMock())
    future = asyncio.create_task(make_runtime(environment, driver).query_one())
    await started.wait()
    future.cancel()
    with pytest.raises(asyncio.CancelledError):
        await future
    assert environment.queue.report()["states"] == {"WAITING": 1}
    driver.close.assert_not_awaited()


async def test_bounded_run_moves_to_next_date_serially(environment, monkeypatch):
    environment.settings = replace(environment.settings, days=365)
    visits = []
    active = 0

    async def query(task):
        nonlocal active
        active += 1
        assert active == 1
        visits.append(task.checkin)
        result = sold_out_result() | {"checkin": str(task.checkin), "checkout": str(task.checkout)}
        active -= 1
        return result

    sleep = AsyncMock()
    monkeypatch.setattr(automatic.asyncio, "sleep", sleep)
    runtime = make_runtime(environment, SimpleNamespace(query=query))
    result = await runtime.run(max_queries=2)
    assert result["status"] == "no_availability"
    assert visits == [date(2026, 10, 18), date(2026, 10, 19)]
    report = environment.queue.report()
    assert report["tasks"] == 365 and report["successful_queries"] == 2
    sleep.assert_awaited_once_with(1.0)


def insert_pending(store, record, due, expires, suffix):
    store.db.execute(
        "INSERT INTO passive_alerts(id,series_key,rule,first_event_id,first_at,first_payload,baseline,"
        "threshold,confirm_after,expires_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            suffix,
            record.series_key + suffix,
            "HISTORY",
            "a" * 64,
            NOW.isoformat(),
            json.dumps(record.to_dict()),
            "100",
            "65",
            due.isoformat(),
            expires.isoformat(),
            "PENDING",
        ),
    )
    store.db.commit()


def test_pending_confirmations_deduplicate_and_prioritize_future_day(environment, document):
    environment.settings = replace(environment.settings, days=3)
    record = replace(parse_capture(document)[0], checkin=date(2026, 10, 20), checkout=date(2026, 10, 21))
    due, expiry = NOW - timedelta(seconds=1), NOW + timedelta(hours=2)
    insert_pending(environment.store, record, due, expiry, "one")
    insert_pending(environment.store, record, due + timedelta(seconds=10), expiry, "two")
    insert_pending(environment.store, record, due - timedelta(hours=3), NOW - timedelta(seconds=1), "expired")
    assert automatic.pending_confirmations(environment.passive.sqlite_path, NOW, {"XIYBP"}) == []
    pending = automatic.pending_confirmations(environment.passive.sqlite_path, NOW, {"NYCTE"})
    assert len(pending) == 1 and pending[0][1] == due
    runtime = make_runtime(environment, SimpleNamespace())
    today = runtime.seed_and_prioritize()
    task = environment.queue.claim(NOW, today=today)
    assert task.checkin == date(2026, 10, 20)
    assert task.priority >= 100
    assert environment.queue.report()["tasks"] == 3


def test_completed_confirmation_hint_does_not_starve_unqueried_dates(environment, document):
    environment.settings = replace(environment.settings, days=3)
    record = replace(parse_capture(document)[0], checkin=date(2026, 10, 20), checkout=date(2026, 10, 21))
    insert_pending(environment.store, record, NOW - timedelta(seconds=1), NOW + timedelta(hours=2), "pending")
    runtime = make_runtime(environment, SimpleNamespace())
    today = runtime.seed_and_prioritize()
    confirmation = environment.queue.claim(NOW, today=today)
    assert confirmation.checkin == date(2026, 10, 20)
    # A successful sold-out query completes the queue task but creates no fake
    # price in the passive DB; the original candidate can legitimately remain
    # PENDING until expiry. That same hint must not request a search every tick.
    environment.queue.complete(confirmation, NOW, revisit_seconds=86400)
    runtime.seed_and_prioritize()
    following = environment.queue.claim(NOW, today=today)
    assert following.checkin == date(2026, 10, 18)


async def test_existing_services_are_reused_and_locks_are_not_released(environment, monkeypatch):
    monkeypatch.setattr(automatic, "ensure_chrome", lambda args: {"webSocketDebuggerUrl": "ws://127.0.0.1/x"})
    capture_lock = environment.passive.captures_dir / ".capture.lock"
    monitor_lock = environment.passive.sqlite_path.with_suffix(".lock")
    with RunLock(capture_lock), RunLock(monitor_lock):
        services = automatic.CaptureServices(
            environment.settings, environment.passive, existing_listener=True
        )
        await services.start()
        services.check()
        assert services.collector is None and services.monitor_task is None
        await services.close()
        assert automatic._try_lock(capture_lock) is None
        assert automatic._try_lock(monitor_lock) is None


async def test_explicit_existing_listener_requires_real_lock(environment):
    services = automatic.CaptureServices(environment.settings, environment.passive, existing_listener=True)
    with pytest.raises(RuntimeError, match="already running listener"):
        await services.start()
    assert services.capture_lock is None


@pytest.mark.parametrize(
    "change",
    [
        {"days": 366},
        {"days": 1, "nights": 2},
        {"adults": True},
        {"min_delay_seconds": 0},
        {"max_attempts": 0},
        {"timeout_seconds": True},
    ],
)
def test_invalid_automatic_configuration(change):
    with pytest.raises(ValueError):
        AutomaticConfig(hotels=(HOTEL,), **change)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.marriott.com.cn/reservation/availabilitySearch.mi",
        "https://www.marriott.com/hotels/nycte-test/overview/",
        "https://www.marriott.com.cn/hotels/xiybp-test/overview/",
        "https://example.com/hotels/nycte-test/overview/",
    ],
)
def test_only_matching_china_overview_url_is_accepted(url):
    with pytest.raises(ValueError):
        AutomaticHotel("NYCTE", url)


def test_configuration_paths_and_passive_watchlist_validation(tmp_path):
    passive_path = tmp_path / "passive.yaml"
    automatic_path = tmp_path / "automatic.yaml"
    passive_path.write_text(
        yaml.safe_dump({"sqlite_path": "prices.sqlite", "captures_dir": "captures"}), encoding="utf-8"
    )
    automatic_path.write_text(
        yaml.safe_dump(
            {
                "passive_config": "passive.yaml",
                "hotels": [HOTEL.__dict__],
                "days": 365,
            }
        ),
        encoding="utf-8",
    )
    settings = load_automatic_config(automatic_path)
    assert settings.sqlite_path == tmp_path / "data/automatic-queue.sqlite"
    assert settings.passive_config == passive_path
    passive_path.write_text(yaml.safe_dump({"hotels": [{"hotel_id": "XIYBP"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="excludes"):
        load_automatic_config(automatic_path)
