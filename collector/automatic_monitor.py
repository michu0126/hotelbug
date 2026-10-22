"""Automatic ordinary-page queries, passive capture and durable price monitoring.

The driver clicks the official website. Only the separate passive listener saves
the response; a successful click alone never completes a queue task.
"""

import argparse
import asyncio
import contextlib
import json
import random
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott import parse_capture
from adapters.marriott_ui import DateNotSelectable
from automatic_config import load_automatic_config
from main import RunLock
from models import PriceRecord, utc, utcnow
from passive_capture import Collector, capture_event, ensure_chrome
from passive_config import load_passive_config
from passive_monitor import MAX_CAPTURE_BYTES, CaptureWatcher, PassiveMonitor
from passive_storage import PassiveStorage


def _read_capture(path):
    if path.stat().st_size > MAX_CAPTURE_BYTES:
        raise ValueError("Oversized capture")
    result = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(result, dict):
        raise ValueError("Capture must be a JSON object")
    capture_event(result.get("capture_event"))
    return result


def _capture_paths(directory, task):
    return directory.glob(f"*_{task.hotel_id}_{task.checkin}_{task.checkout}_*.json")


def capture_baseline(directory, task):
    """Existing observations cannot complete a new browser query."""
    ids = set()
    for path in _capture_paths(directory, task):
        try:
            ids.add(_read_capture(path)["capture_event"]["event_id"])
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return ids


def find_capture(directory, task, started_at, baseline, result, now):
    """Require fresh, complete, matching passive evidence, never a saved sample."""
    started_at, now = utc(started_at).replace(microsecond=0), utc(now)
    for path in _capture_paths(directory, task):
        try:
            document = _read_capture(path)
            event = document["capture_event"]["event_id"]
            observed = utc(datetime.fromisoformat(document["captured_at"].replace("Z", "+00:00")))
            options = document["variables"]["search"]["options"]
            if (
                event in baseline
                or observed < started_at
                or observed > now + timedelta(seconds=5)
                or document["property_id"] != task.hotel_id
                or document["check_in"] != str(task.checkin)
                or document["check_out"] != str(task.checkout)
                or options["numberInParty"] != task.adults
                or options["quantity"] != 1
                or document["source_site"] != result.get("source_site")
            ):
                continue
            records = parse_capture(document)
            digest = sha256(
                json.dumps(document["response"], sort_keys=True, ensure_ascii=True).encode()
            ).hexdigest()
            if result.get("response_hash") and digest != result["response_hash"]:
                continue
            if len(records) != result.get("offers"):
                continue
            return {"path": path, "event_id": event, "offers": len(records), "response_hash": digest}
        except (OSError, ValueError, TypeError, KeyError):
            continue
    return None


async def wait_for_capture(
    directory, task, started_at, baseline, result, *, timeout=30, clock=utcnow, check=None
):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if check:
            check()
        found = find_capture(directory, task, started_at, baseline, result, clock())
        if found:
            return found
        if asyncio.get_running_loop().time() >= deadline:
            raise TransientFailure("Matching new passive capture was not saved")
        await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))


def event_ingested(database, event_id):
    if not database.is_file():
        return False
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            row = db.execute("SELECT result FROM passive_events WHERE id=?", (event_id,)).fetchone()
            return row is not None
    except sqlite3.OperationalError:
        return False


async def wait_for_ingestion(database, capture, *, timeout=20, check=None):
    # Empty, fully validated availability responses intentionally create no
    # monetary observation in PassiveMonitor.
    if not capture["offers"]:
        return
    deadline = asyncio.get_running_loop().time() + timeout
    while not event_ingested(database, capture["event_id"]):
        if check:
            check()
        if asyncio.get_running_loop().time() >= deadline:
            raise TransientFailure("Capture saved but monitor has not ingested it")
        await asyncio.sleep(min(0.25, max(0, deadline - asyncio.get_running_loop().time())))


def pending_confirmations(database, now, hotel_ids):
    """Read the active monitor without taking its writer lock."""
    if not database.is_file():
        return []
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            rows = db.execute(
                "SELECT first_payload,confirm_after,expires_at FROM passive_alerts WHERE status='PENDING'"
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    result = {}
    for payload, confirm_after, expires_at in rows:
        try:
            record = PriceRecord.from_dict(json.loads(payload))
            due = utc(datetime.fromisoformat(confirm_after))
            expires = utc(datetime.fromisoformat(expires_at))
            if expires <= utc(now) or record.hotel_id not in hotel_ids:
                continue
            if record.source_site != "www.marriott.com.cn":
                continue  # The China-site UI must never confirm an international-site series.
            key = (record.hotel_id, record.checkin, record.checkout, record.adults)
            previous = result.get(key)
            result[key] = (min(previous[0], due), max(previous[1], expires)) if previous else (due, expires)
        except (ValueError, TypeError, KeyError):
            continue
    return [(key, due, expires) for key, (due, expires) in result.items()]


def _try_lock(path):
    lock = RunLock(path)
    try:
        lock.__enter__()
    except RuntimeError:
        return None
    return lock


class CaptureServices:
    """Own only free local locks; never interrupt an existing collector or Chrome."""

    def __init__(self, settings, passive, *, existing_listener=False):
        self.settings, self.passive = settings, passive
        self.existing_listener = existing_listener
        self.capture_lock = self.monitor_lock = None
        self.websocket = self.connection = self.collector = self.storage = None
        self.reader = self.monitor_task = None

    async def start(self):
        import websockets

        self.capture_lock = _try_lock(self.passive.captures_dir / ".capture.lock")
        if self.existing_listener and self.capture_lock:
            self.capture_lock.__exit__()
            self.capture_lock = None
            raise RuntimeError("--existing-listener requires an already running listener")
        args = SimpleNamespace(
            port=self.settings.port,
            launch=not self.existing_listener,
            chrome=self.settings.chrome,
            profile=str(self.settings.profile),
            start_url="https://www.marriott.com.cn/",
        )
        try:
            info = await asyncio.to_thread(ensure_chrome, args)
            if self.capture_lock:
                self.connection = websockets.connect(
                    info["webSocketDebuggerUrl"], max_size=25_000_000, proxy=None
                )
                self.websocket = await self.connection.__aenter__()
                self.collector = Collector(self.websocket, self.passive.captures_dir)
                self.reader = asyncio.create_task(self.collector.reader())
                await self.collector.send(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                )
                print("[采集] 已启动响应监听，准备自动操作官网页面。")
            else:
                print("[采集] 复用已经运行的响应监听，不关闭或重复启动。")
            self.monitor_lock = _try_lock(self.passive.sqlite_path.with_suffix(".lock"))
            if self.monitor_lock:
                self.storage = PassiveStorage(self.passive.sqlite_path)
                watcher = CaptureWatcher(PassiveMonitor(self.passive, self.storage))
                self.monitor_task = asyncio.create_task(self._watch(watcher))
                print(f"[监控] 已启动历史检测，通知渠道：{self.passive.notifications.channel}。")
            else:
                print("[监控] 复用已经运行的历史检测。")
            return self
        except BaseException:
            await self.close()
            raise

    async def _watch(self, watcher):
        while True:
            result = await watcher.tick()
            if result["files"] or any(result[key] for key in ("sent", "dry_run", "unknown")):
                print("[监控结果] " + json.dumps(result, ensure_ascii=False))
            await asyncio.sleep(self.passive.scan_interval_seconds)

    def check(self):
        for task in (self.reader, self.monitor_task):
            if task is not None and task.done():
                if not task.cancelled():
                    task.result()
                raise RuntimeError("A required capture/monitor service has stopped")
        # Verify an externally owned service still holds its lock, without
        # acquiring its database or terminating anything.
        for owned, path in (
            (self.capture_lock, self.passive.captures_dir / ".capture.lock"),
            (self.monitor_lock, self.passive.sqlite_path.with_suffix(".lock")),
        ):
            if owned is None:
                probe = _try_lock(path)
                if probe:
                    probe.__exit__()
                    raise RuntimeError("An external capture/monitor service has stopped")

    async def close(self):
        for task in (self.reader, self.monitor_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.reader, self.monitor_task) if task), return_exceptions=True
        )
        if self.collector:
            await self.collector.close()
        if self.connection:
            await self.connection.__aexit__(None, None, None)
        if self.storage:
            self.storage.close()
        for lock in (self.monitor_lock, self.capture_lock):
            if lock:
                lock.__exit__()
        self.reader = self.monitor_task = self.collector = self.connection = self.storage = None
        self.monitor_lock = self.capture_lock = None


class AutomaticRuntime:
    def __init__(self, settings, passive, queue, driver, *, services=None, clock=utcnow):
        self.settings, self.passive, self.queue, self.driver = settings, passive, queue, driver
        self.services, self.clock = services, clock
        self._seeded_day = None
        self.queue.db.execute("""CREATE TABLE IF NOT EXISTS automatic_results (
            id INTEGER PRIMARY KEY, task_id TEXT NOT NULL, finished_at TEXT NOT NULL,
            status TEXT NOT NULL, offers INTEGER NOT NULL, event_id TEXT, evidence TEXT
        )""")

    def record_result(self, task, status, offers=0, event_id=None, evidence=None):
        self.queue.db.execute(
            "INSERT INTO automatic_results(task_id,finished_at,status,offers,event_id,evidence) "
            "VALUES(?,?,?,?,?,?)",
            (task.id, self.clock().isoformat(), status, offers, event_id, evidence),
        )

    async def _drive(self, task):
        """Loss of the response listener cancels UI work, not only the next task."""
        query = asyncio.create_task(self.driver.query(task))
        try:
            deadline = asyncio.get_running_loop().time() + self.settings.timeout_seconds
            while not query.done():
                self._check()
                if asyncio.get_running_loop().time() >= deadline:
                    raise asyncio.TimeoutError()
                await asyncio.wait({query}, timeout=0.25)
            return await query
        finally:
            if not query.done():
                query.cancel()
                await asyncio.gather(query, return_exceptions=True)

    def _check(self):
        if self.queue.stop_requested():
            raise asyncio.CancelledError()
        if self.services:
            self.services.check()

    def seed_and_prioritize(self):
        now = self.clock()
        today = now.astimezone(ZoneInfo(self.settings.timezone)).date()
        if self._seeded_day != today:
            self.queue.seed(
                self.settings.hotels,
                today,
                days=self.settings.days,
                nights=self.settings.nights,
                adults=self.settings.adults,
                now=now,
            )
            self._seeded_day = today
        for (hotel_id, checkin, checkout, adults), due, expires in pending_confirmations(
            self.passive.sqlite_path, now, {hotel.hotel_id for hotel in self.settings.hotels}
        ):
            self.queue.prioritize(hotel_id, checkin, checkout, adults, due_at=due, expires_at=expires)
        return today

    async def query_one(self):
        self._check()
        if self.queue.report()["paused"]:
            return {"status": "paused"}
        today = self.seed_and_prioritize()
        lease_seconds = (
            self.settings.timeout_seconds
            + self.settings.capture_timeout_seconds
            + self.settings.ingestion_timeout_seconds
            + 60
        )
        task = self.queue.claim(self.clock(), lease_seconds=lease_seconds, today=today)
        if task is None:
            return {"status": "idle"}
        print(
            f"[自动查价] {task.hotel_id} {task.checkin} → {task.checkout}，"
            f"{task.adults} 成人，第 {task.attempts} 次尝试。"
        )
        baseline = capture_baseline(self.passive.captures_dir, task)
        started_at = self.clock()
        try:
            result = await self._drive(task)
            if result.get("status") not in ("ok", "no_availability"):
                raise ValueError("Driver did not return a verified availability result")
            if result["status"] == "no_availability":
                if (
                    result.get("evidence") != "official_sold_out_redirect_and_matching_hotel_card"
                    or result.get("hotel_id") != task.hotel_id
                    or result.get("checkin") != str(task.checkin)
                    or result.get("checkout") != str(task.checkout)
                    or result.get("adults") != task.adults
                    or result.get("source_site") != "www.marriott.com.cn"
                    or result.get("offers") != 0
                ):
                    raise ValueError("Sold-out page did not match the query")
                self.record_result(task, "NO_AVAILABILITY", evidence=result["evidence"])
                self.queue.complete(task, self.clock(), revisit_seconds=self.settings.revisit_seconds)
                print(f"[无房] {task.hotel_id} {task.checkin} 官网明确显示售罄；不写入零价格，稍后复查。")
                return {"status": "no_availability", "offers": 0}
            capture = await wait_for_capture(
                self.passive.captures_dir,
                task,
                started_at,
                baseline,
                result,
                timeout=self.settings.capture_timeout_seconds,
                clock=self.clock,
                check=self._check,
            )
            await wait_for_ingestion(
                self.passive.sqlite_path,
                capture,
                timeout=self.settings.ingestion_timeout_seconds,
                check=self._check,
            )
            self.queue.complete(task, self.clock(), revisit_seconds=self.settings.revisit_seconds)
            self.record_result(
                task,
                "PRICED" if capture["offers"] else "NO_CASH_OFFERS",
                capture["offers"],
                capture["event_id"],
            )
            print(
                f"[查价完成] {task.hotel_id} {task.checkin}，"
                f"{capture['offers']} 条有效现金报价，已保存"
                + ("并入库。" if capture["offers"] else "（官网无现金报价）。")
            )
            return {"status": "ok", "offers": capture["offers"], "event_id": capture["event_id"]}
        except BrandBlocked:
            self.queue.pause("HTTP 403 or verification challenge", self.clock(), task=task)
            print("[暂停] 官网返回 403 或验证页面。任务已保存，不会自动重试。")
            return {"status": "paused"}
        except RateLimited as error:
            delay = max(3600, float(error.retry_after))
            self.queue.pause("HTTP 429 rate limit", self.clock(), task=task)
            print(
                f"[限流] 已暂停全部万豪任务；请至少等待 {int(delay)} 秒，"
                "确认官网恢复后再使用 --resume-brand。"
            )
            return {"status": "rate_limited"}
        except DateNotSelectable:
            self.queue.defer(
                task, self.clock(), seconds=self.settings.revisit_seconds, reason="DATE_NOT_SELECTABLE"
            )
            self.record_result(task, "DATE_NOT_SELECTABLE")
            print(f"[日期未开放] {task.hotel_id} {task.checkin} 官网日历暂不可选；保留任务，稍后复查。")
            return {"status": "date_not_selectable"}
        except (TransientFailure, asyncio.TimeoutError, ValueError, OSError) as error:
            due = self.queue.retry(
                task,
                self.clock(),
                type(error).__name__,
                base_seconds=self.settings.backoff_seconds,
                max_seconds=self.settings.max_backoff_seconds,
                max_attempts=self.settings.max_attempts,
            )
            if due is None:
                print("[未完成] 已达到该任务重试上限，标记失败，继续其他日期。")
                return {"status": "failed"}
            print(f"[未完成] 本次未取得完整入库证据，已记录；下次尝试：{due.isoformat()}。")
            return {"status": "retry", "next_due": due.isoformat()}
        except Exception:
            self.queue.retry(
                task,
                self.clock(),
                "ServiceStopped",
                base_seconds=60,
                max_seconds=60,
                max_attempts=task.attempts + 1,
            )
            raise
        except asyncio.CancelledError:
            self.queue.retry(
                task,
                self.clock(),
                "Interrupted",
                base_seconds=1,
                max_seconds=1,
                max_attempts=task.attempts + 1,
            )
            raise

    async def run(self, *, once=False, max_queries=None):
        attempts = 0
        while True:
            result = await self.query_one()
            if result["status"] != "idle":
                attempts += 1
            if once or result["status"] in ("paused", "rate_limited"):
                return result
            if max_queries is not None and attempts >= max_queries:
                return result
            delay = (
                self.settings.idle_seconds
                if result["status"] == "idle"
                else random.uniform(self.settings.min_delay_seconds, self.settings.max_delay_seconds)
            )
            await asyncio.sleep(delay)


async def run(args):
    from adapters.marriott_ui import MarriottUIDriver
    from automatic_queue import Queue

    settings = load_automatic_config(Path(args.config))
    passive = load_passive_config(settings.passive_config)
    passive = replace(
        passive,
        hotel_names={
            **passive.hotel_names,
            **{hotel.hotel_id: hotel.name for hotel in settings.hotels if hotel.name},
        },
    )
    if args.status:
        result = Queue.read_report(settings.sqlite_path)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return result
    if getattr(args, "stop", False):
        if settings.sqlite_path.is_file():
            with Queue(settings.sqlite_path) as queue:
                queue.request_stop()
            print("已请求停止自动查询；当前任务会安全退出，Chrome 与历史数据保留。")
        else:
            print("尚无已启动的自动任务。")
        return {"status": "stop_requested"}
    browser_lock = Path(__file__).parent / "data" / f"automatic-chrome-{settings.port}.lock"
    with RunLock(browser_lock), RunLock(settings.sqlite_path.with_suffix(".lock")):
        queue = Queue(settings.sqlite_path)
        try:
            queue.clear_stop()
            if args.resume_brand:
                queue.resume(utcnow())
            elif queue.report()["paused"]:
                print("[暂停] 万豪任务仍处于暂停状态；检查官网后使用 --resume-brand 恢复。")
                return {"status": "paused"}
            services = CaptureServices(settings, passive, existing_listener=args.existing_listener)
            driver = MarriottUIDriver(port=settings.port, timeout_seconds=settings.timeout_seconds)
            try:
                await services.start()
                await driver.start()
                print("[自动] 正在依次查价；Ctrl+C 可停止，进度保留，Chrome 不会被关闭。")
                runtime = AutomaticRuntime(settings, passive, queue, driver, services=services)
                return await runtime.run(once=args.once, max_queries=args.max_queries)
            finally:
                await driver.close()
                await services.close()
        finally:
            queue.close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.automatic.yaml")))
    parser.add_argument("--once", action="store_true", help="最多执行一个到期查询")
    parser.add_argument("--max-queries", type=int, help="本次最多尝试的查询数，默认持续运行")
    parser.add_argument("--status", action="store_true", help="显示已保存的任务进度，不打开浏览器")
    parser.add_argument("--stop", action="store_true", help="安全停止后台自动查询，保留Chrome和进度")
    parser.add_argument("--resume-brand", action="store_true", help="明确恢复因验证/403而暂停的万豪任务")
    parser.add_argument("--existing-listener", action="store_true", help="只复用现有监听器，不启动新监听器")
    args = parser.parse_args()
    if args.max_queries is not None and args.max_queries < 1:
        parser.error("--max-queries must be positive")
    try:
        asyncio.run(run(args))
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("自动查价已停止；任务、价格历史和 Chrome 窗口均保留。")
    except (ValueError, TypeError, RuntimeError, OSError, SystemExit) as error:
        parser.exit(2, f"自动查价已停止（{type(error).__name__}），未输出账号或凭据。\n")


if __name__ == "__main__":
    cli()
