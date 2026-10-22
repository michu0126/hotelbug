"""Durable, single-brand work queue for ordinary Marriott website searches.

The queue never sends a hotel request. A UI driver owns a short renewable lease,
performs one normal website search, and reports the result using that lease.
Confirmation requests are scheduling hints, not evidence of a confirmed price.
"""

import hashlib
import json
import math
import random
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4


def _instant(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware datetime is required")
    return value.astimezone(timezone.utc)


def _stamp(value):
    return _instant(value).isoformat()


def _day(value):
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("A date, not a datetime, is required")
    return value


def _positive(value, name, *, allow_zero=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number")
    if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
        raise ValueError(f"{name} must be a finite positive number")
    return value


def _identity(hotel_id, checkin, checkout, adults):
    hotel_id = str(hotel_id).upper()
    if not re.fullmatch(r"[A-Z0-9]{5}", hotel_id):
        raise ValueError("Marriott hotel ID must have five letters or digits")
    if _day(checkout) <= _day(checkin):
        raise ValueError("Checkout must follow checkin")
    if isinstance(adults, bool) or not isinstance(adults, int) or not 1 <= adults <= 10:
        raise ValueError("Adults must be an integer between 1 and 10")
    raw = json.dumps([hotel_id, checkin.isoformat(), checkout.isoformat(), adults])
    return hotel_id, hashlib.sha256(raw.encode()).hexdigest()


def _hotel_url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"www.marriott.com", "www.marriott.com.cn"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or parsed.fragment
    ):
        raise ValueError("Hotel URL must be an HTTPS Marriott official page")
    return value


@dataclass(frozen=True)
class QueryTask:
    id: str
    hotel_id: str
    hotel_url: str
    checkin: date
    checkout: date
    adults: int
    attempts: int
    priority: int
    lease_token: str


class StaleLeaseError(RuntimeError):
    """A task result does not belong to its current owner."""


class Queue:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS automatic_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT,
                window_start TEXT,
                window_end TEXT,
                updated_at TEXT
            );
            INSERT OR IGNORE INTO automatic_state(singleton) VALUES(1);
            CREATE TABLE IF NOT EXISTS automatic_tasks (
                id TEXT PRIMARY KEY,
                hotel_id TEXT NOT NULL,
                hotel_url TEXT NOT NULL,
                checkin TEXT NOT NULL,
                checkout TEXT NOT NULL,
                adults INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'WAITING',
                due_at TEXT NOT NULL,
                retry_after TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 0,
                confirmation_due TEXT,
                confirmation_expires TEXT,
                lease_token TEXT,
                lease_until TEXT,
                lease_confirmation INTEGER NOT NULL DEFAULT 0,
                last_started_at TEXT,
                last_completed_at TEXT,
                last_error TEXT,
                updated_at TEXT NOT NULL,
                UNIQUE(hotel_id, checkin, checkout, adults)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS automatic_one_running
                ON automatic_tasks(status) WHERE status='RUNNING';
            CREATE INDEX IF NOT EXISTS automatic_due
                ON automatic_tasks(active, status, due_at);
        """)
        # Preserve existing queues; only successful query start times can cover
        # a confirmation hint. A later failed attempt must not overwrite them.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(automatic_tasks)")}
        if "last_success_started_at" not in columns:
            self.db.execute("ALTER TABLE automatic_tasks ADD COLUMN last_success_started_at TEXT")
        state_columns = {row[1] for row in self.db.execute("PRAGMA table_info(automatic_state)")}
        if "stop_requested" not in state_columns:
            self.db.execute(
                "ALTER TABLE automatic_state ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0"
            )

    def request_stop(self):
        self.db.execute("UPDATE automatic_state SET stop_requested=1 WHERE singleton=1")

    def clear_stop(self):
        self.db.execute("UPDATE automatic_state SET stop_requested=0 WHERE singleton=1")

    def stop_requested(self):
        return bool(
            self.db.execute("SELECT stop_requested FROM automatic_state WHERE singleton=1").fetchone()[0]
        )

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()

    @classmethod
    def read_report(cls, path, now=None):
        """Inspect an active worker's database without creating files or taking its lock."""
        path = Path(path).resolve()
        if not path.is_file():
            result = {
                "initialized": False,
                "paused": False,
                "pause_reason": None,
                "window_start": None,
                "window_end": None,
                "states": {},
                "tasks": 0,
                "successful_queries": 0,
                "attempts": 0,
                "visited_tasks": 0,
                "next_due": None,
            }
            if now is not None:
                result["observed_at"] = _stamp(now)
            return result
        reader = cls.__new__(cls)
        reader.db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
        reader.db.row_factory = sqlite3.Row
        try:
            return reader.report(now)
        finally:
            reader.close()

    @contextmanager
    def _transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.db.rollback()
            raise
        else:
            self.db.commit()

    def seed(self, hotels, today, days=365, nights=1, adults=1, *, now):
        """Synchronize scope without resetting progress, retries or active leases.

        ``days=365, nights=1`` produces 365 check-in dates, starting today; the
        final checkout is today + 365 days. Removing a hotel retires its tasks.
        Re-seeding daily advances the rolling window and adds its new last day.
        """
        today = _day(today)
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise ValueError("Days must be an integer between 1 and 365")
        if isinstance(nights, bool) or not isinstance(nights, int) or not 1 <= nights <= days:
            raise ValueError("Nights must be an integer between 1 and days")
        hotels = list(hotels)
        normalized = []
        seen = set()
        for hotel in hotels:
            if not isinstance(hotel, dict):
                hotel = {"hotel_id": hotel.hotel_id, "hotel_url": hotel.hotel_url}
            hotel_id, _ = _identity(hotel["hotel_id"], today, today + timedelta(days=nights), adults)
            if hotel_id in seen:
                raise ValueError("Hotel IDs must not be duplicated")
            seen.add(hotel_id)
            normalized.append((hotel_id, _hotel_url(hotel["hotel_url"])))
        # Validate occupancy even when the watchlist is empty.
        _identity("EMPTY", today, today + timedelta(days=nights), adults)
        timestamp = _stamp(now)

        def rows():
            # Stream a potentially large hotel × date product rather than retaining it in RAM.
            for hotel_id, url in normalized:
                for offset in range(days - nights + 1):
                    checkin = today + timedelta(days=offset)
                    checkout = checkin + timedelta(days=nights)
                    _, task_id = _identity(hotel_id, checkin, checkout, adults)
                    yield (
                        task_id,
                        hotel_id,
                        url,
                        checkin.isoformat(),
                        checkout.isoformat(),
                        adults,
                        timestamp,
                        timestamp,
                    )

        with self._transaction():
            self.db.execute("UPDATE automatic_tasks SET active=0")
            self.db.executemany(
                """INSERT INTO automatic_tasks(
                       id,hotel_id,hotel_url,checkin,checkout,adults,due_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                       hotel_url=excluded.hotel_url,active=1,
                       status=CASE WHEN automatic_tasks.status='EXPIRED'
                                   THEN 'WAITING' ELSE automatic_tasks.status END,
                       updated_at=excluded.updated_at""",
                rows(),
            )
            self.db.execute(
                """UPDATE automatic_tasks SET status='EXPIRED',lease_token=NULL,
                       lease_until=NULL,confirmation_due=NULL,confirmation_expires=NULL,
                       updated_at=? WHERE active=0""",
                (timestamp,),
            )
            self.db.execute(
                """UPDATE automatic_state SET window_start=?,window_end=?,updated_at=?
                   WHERE singleton=1""",
                (today.isoformat(), (today + timedelta(days=days)).isoformat(), timestamp),
            )
        return len(normalized) * (days - nights + 1)

    def _maintain(self, now, today=None):
        timestamp = _stamp(now)
        day = _day(today) if today is not None else now.date()
        self.db.execute(
            """UPDATE automatic_tasks SET active=0,status='EXPIRED',lease_token=NULL,
                   lease_until=NULL,confirmation_due=NULL,confirmation_expires=NULL,
                   updated_at=? WHERE active=1 AND checkin<?""",
            (timestamp, day.isoformat()),
        )
        self.db.execute(
            """UPDATE automatic_tasks SET confirmation_due=NULL,confirmation_expires=NULL
               WHERE confirmation_expires<=?""",
            (timestamp,),
        )
        self.db.execute(
            """UPDATE automatic_tasks SET status='WAITING',lease_token=NULL,lease_until=NULL,
                   last_error='LEASE_EXPIRED',updated_at=?
               WHERE status='RUNNING' AND lease_until<=?""",
            (timestamp, timestamp),
        )

    def claim(self, now, *, lease_seconds=180, today=None):
        """Atomically obtain one due task; there can be only one running task."""
        _positive(lease_seconds, "Lease seconds")
        timestamp = _stamp(now)
        with self._transaction():
            self._maintain(now, today)
            state = self.db.execute("SELECT paused FROM automatic_state").fetchone()
            if state["paused"]:
                return None
            if self.db.execute("SELECT 1 FROM automatic_tasks WHERE status='RUNNING'").fetchone():
                return None
            row = self.db.execute(
                """SELECT *,
                       CASE WHEN confirmation_due<=? AND confirmation_expires>?
                            THEN 1 ELSE 0 END AS confirm_now
                   FROM automatic_tasks
                   WHERE active=1 AND status='WAITING'
                     AND (retry_after IS NULL OR retry_after<=?)
                     AND (due_at<=? OR (confirmation_due<=? AND confirmation_expires>?))
                   ORDER BY confirm_now DESC,priority DESC,due_at,checkin,hotel_id LIMIT 1""",
                (timestamp, timestamp, timestamp, timestamp, timestamp, timestamp),
            ).fetchone()
            if row is None:
                return None
            token = uuid4().hex
            self.db.execute(
                """UPDATE automatic_tasks SET status='RUNNING',lease_token=?,lease_until=?,
                       lease_confirmation=?,attempts=attempts+1,total_attempts=total_attempts+1,
                       last_started_at=?,updated_at=? WHERE id=?""",
                (
                    token,
                    _stamp(now + timedelta(seconds=lease_seconds)),
                    row["confirm_now"],
                    timestamp,
                    timestamp,
                    row["id"],
                ),
            )
            return QueryTask(
                row["id"],
                row["hotel_id"],
                row["hotel_url"],
                date.fromisoformat(row["checkin"]),
                date.fromisoformat(row["checkout"]),
                row["adults"],
                row["attempts"] + 1,
                max(row["priority"], 100) if row["confirm_now"] else row["priority"],
                token,
            )

    def _owned(self, task, now):
        row = self.db.execute(
            """SELECT * FROM automatic_tasks
               WHERE id=? AND status='RUNNING' AND active=1 AND lease_token=? AND lease_until>?""",
            (task.id, task.lease_token, _stamp(now)),
        ).fetchone()
        if row is None:
            raise StaleLeaseError("Task lease is no longer current")
        return row

    def renew(self, task, now, *, lease_seconds=180):
        _positive(lease_seconds, "Lease seconds")
        with self._transaction():
            self._owned(task, now)
            self.db.execute(
                "UPDATE automatic_tasks SET lease_until=?,updated_at=? WHERE id=?",
                (_stamp(now + timedelta(seconds=lease_seconds)), _stamp(now), task.id),
            )

    def complete(self, task, now, *, revisit_seconds):
        """Record one successful observed search and schedule its next revisit."""
        _positive(revisit_seconds, "Revisit seconds")
        with self._transaction():
            row = self._owned(task, now)
            self.db.execute(
                """UPDATE automatic_tasks SET status='WAITING',due_at=?,attempts=0,retry_after=NULL,
                       successes=successes+1,lease_token=NULL,lease_until=NULL,last_error=NULL,
                       last_completed_at=?,updated_at=?,last_success_started_at=last_started_at,
                       confirmation_due=CASE WHEN ? THEN NULL ELSE confirmation_due END,
                       confirmation_expires=CASE WHEN ? THEN NULL ELSE confirmation_expires END
                   WHERE id=?""",
                (
                    _stamp(now + timedelta(seconds=revisit_seconds)),
                    _stamp(now),
                    _stamp(now),
                    row["lease_confirmation"],
                    row["lease_confirmation"],
                    task.id,
                ),
            )

    def retry(
        self,
        task,
        now,
        reason,
        *,
        base_seconds=30,
        max_seconds=1800,
        max_attempts=4,
        jitter=None,
    ):
        """Persist capped exponential backoff; return its due date, or None if failed."""
        _positive(base_seconds, "Base seconds")
        _positive(max_seconds, "Max seconds")
        if max_seconds < base_seconds:
            raise ValueError("Max seconds must not be smaller than base seconds")
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("Max attempts must be a positive integer")
        with self._transaction():
            row = self._owned(task, now)
            if row["attempts"] >= max_attempts:
                self._finish_failure(task, now, reason)
                return None
            # Full jitter on [base, capped exponential], bounded and injectable in tests.
            ceiling = min(max_seconds, base_seconds * 2 ** min(row["attempts"], 30))
            delay = (jitter or random.uniform)(base_seconds, ceiling)
            if not isinstance(delay, (int, float)) or not math.isfinite(delay):
                raise ValueError("Retry jitter must produce a finite number")
            delay = min(ceiling, max(base_seconds, delay))
            due = _instant(now) + timedelta(seconds=delay)
            self.db.execute(
                """UPDATE automatic_tasks SET status='WAITING',due_at=?,retry_after=?,lease_token=NULL,
                       lease_until=NULL,last_error=?,updated_at=?,
                       confirmation_due=CASE WHEN confirmation_due<? THEN ? ELSE confirmation_due END
                   WHERE id=?""",
                (
                    due.isoformat(),
                    due.isoformat(),
                    str(reason)[:500],
                    _stamp(now),
                    due.isoformat(),
                    due.isoformat(),
                    task.id,
                ),
            )
            return due

    def _finish_failure(self, task, now, reason):
        self.db.execute(
            """UPDATE automatic_tasks SET status='FAILED',lease_token=NULL,lease_until=NULL,
                   confirmation_due=NULL,confirmation_expires=NULL,last_error=?,updated_at=?
               WHERE id=?""",
            (str(reason)[:500], _stamp(now), task.id),
        )

    def fail(self, task, now, reason):
        with self._transaction():
            self._owned(task, now)
            self._finish_failure(task, now, reason)

    def defer(self, task, now, *, seconds, reason):
        """Revisit a date the website cannot select yet, without claiming a success."""
        _positive(seconds, "Deferral seconds")
        with self._transaction():
            self._owned(task, now)
            self.db.execute(
                """UPDATE automatic_tasks SET status='WAITING',lease_token=NULL,lease_until=NULL,
                       attempts=0,due_at=?,retry_after=?,confirmation_due=NULL,
                       confirmation_expires=NULL,last_error=?,updated_at=? WHERE id=?""",
                (
                    _stamp(now + timedelta(seconds=seconds)),
                    _stamp(now + timedelta(seconds=seconds)),
                    str(reason)[:500],
                    _stamp(now),
                    task.id,
                ),
            )

    def pause(self, reason, now, *, task=None):
        """Persist brand-wide pause and release its current lease, without restarting it."""
        with self._transaction():
            if task is not None:
                self._owned(task, now)
            self.db.execute(
                """UPDATE automatic_state SET paused=1,pause_reason=?,updated_at=?
                   WHERE singleton=1""",
                (str(reason)[:500], _stamp(now)),
            )
            self.db.execute(
                """UPDATE automatic_tasks SET status='WAITING',lease_token=NULL,lease_until=NULL,
                       last_error=?,updated_at=? WHERE status='RUNNING'""",
                (str(reason)[:500], _stamp(now)),
            )

    def resume(self, now):
        with self._transaction():
            self.db.execute(
                """UPDATE automatic_state SET paused=0,pause_reason=NULL,updated_at=?
                   WHERE singleton=1""",
                (_stamp(now),),
            )

    def prioritize(self, hotel_id, checkin, checkout, adults, *, due_at, expires_at):
        """Schedule an independent recheck, without reviving excluded/failed tasks."""
        _, task_id = _identity(hotel_id, checkin, checkout, adults)
        due_at, expires_at = _instant(due_at), _instant(expires_at)
        if expires_at <= due_at:
            raise ValueError("Confirmation expiry must follow its due date")
        with self._transaction():
            row = self.db.execute(
                """SELECT confirmation_due,confirmation_expires,last_success_started_at FROM automatic_tasks
                   WHERE id=? AND active=1 AND status IN ('WAITING','RUNNING')""",
                (task_id,),
            ).fetchone()
            if row is None:
                return False
            if row["last_success_started_at"] and row["last_success_started_at"] >= due_at.isoformat():
                return False  # Already queried after this hint became due; do not starve other dates.
            # Repeated detector hints must not postpone an existing earlier recheck.
            if row["confirmation_due"] is not None and row["confirmation_expires"] > due_at.isoformat():
                due_at = min(due_at, datetime.fromisoformat(row["confirmation_due"]))
                expires_at = max(expires_at, datetime.fromisoformat(row["confirmation_expires"]))
            self.db.execute(
                """UPDATE automatic_tasks SET confirmation_due=?,confirmation_expires=?
                   WHERE id=?""",
                (due_at.isoformat(), expires_at.isoformat(), task_id),
            )
            return True

    def get(self, task_id):
        row = self.db.execute("SELECT * FROM automatic_tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def report(self, now=None):
        """Read-only progress summary; does not reclaim leases or execute work."""
        state = dict(self.db.execute("SELECT * FROM automatic_state").fetchone())
        counts = {
            row["status"]: row["total"]
            for row in self.db.execute(
                "SELECT status,COUNT(*) AS total FROM automatic_tasks WHERE active=1 GROUP BY status"
            )
        }
        totals = self.db.execute(
            """SELECT COUNT(*) AS tasks,COALESCE(SUM(successes),0) AS successful_queries,
                      COALESCE(SUM(total_attempts),0) AS attempts,
                      COALESCE(SUM(successes>0),0) AS visited_tasks,
                      MIN(CASE WHEN status='WAITING' THEN
                          CASE WHEN confirmation_due<due_at THEN confirmation_due ELSE due_at END
                          END) AS next_due
               FROM automatic_tasks WHERE active=1"""
        ).fetchone()
        result = {
            "initialized": True,
            "paused": bool(state["paused"]),
            "pause_reason": state["pause_reason"],
            "stop_requested": bool(state.get("stop_requested", 0)),
            "window_start": state["window_start"],
            "window_end": state["window_end"],
            "states": counts,
            **dict(totals),
        }
        if self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_results'"
        ).fetchone():
            result["outcomes"] = {
                row[0]: row[1]
                for row in self.db.execute("SELECT status,COUNT(*) FROM automatic_results GROUP BY status")
            }
        if now is not None:
            result["observed_at"] = _stamp(now)
        return result
