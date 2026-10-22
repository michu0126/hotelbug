"""A durable, single-machine queue for the local global-monitor runner.

This implements the Redis Queue contract without a Redis server. SQLite WAL
and short IMMEDIATE transactions serialize independent processes, not just
coroutines. A popped job is still owned by the ordinary database job lease;
the Scheduler replays due jobs after a crash, exactly as with the Redis queue.
The local runner deliberately allows at most one browser task per group.
"""

import asyncio
import math
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, TypeVar
from uuid import uuid4

T = TypeVar("T")


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError(f"{label} must be a nonempty string without NUL characters")
    return value


def _number(value: int | float, label: str, *, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError(f"{label} must be {'positive and ' if positive else ''}finite")
    return result


class SQLiteQueue:
    """File-backed queue; use a separate file from the application's ORM DB.

    Lock/rate expiry uses UTC Unix seconds so it survives a process restart.
    ``clock`` is injectable only to make expiry tests deterministic. Every
    operation opens and closes its own connection and runs off the event loop.
    ``close`` prevents new operations; there is no background service to stop.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        busy_timeout_seconds: float = 5,
    ):
        if str(path) == ":memory:":
            raise ValueError("SQLiteQueue requires a persistent file path")
        self.path = Path(path).expanduser().resolve()
        self.clock = clock
        self.timeout = _number(busy_timeout_seconds, "busy timeout", positive=True)
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS local_queue_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL UNIQUE,
                    priority REAL NOT NULL,
                    enqueued_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS local_queue_priority
                    ON local_queue_jobs (priority DESC, sequence ASC);
                CREATE TABLE IF NOT EXISTS local_queue_locks (
                    lock_key TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS local_queue_limits (
                    provider TEXT PRIMARY KEY,
                    next_allowed_at REAL NOT NULL
                );
                """
            )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=self.timeout, isolation_level=None)
        try:
            # FULL preserves accepted queue/lease changes across an OS crash.
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    def _transaction(self, operation: Callable[[sqlite3.Connection, float], T]) -> T:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # Read the clock only after acquiring the cross-process writer
                # lock, otherwise lock contention can produce already-old TTLs.
                now = _number(self.clock(), "clock")
                result = operation(connection, now)
                connection.commit()
                return result
            except BaseException:
                connection.rollback()
                raise

    async def _run(self, operation: Callable[[sqlite3.Connection, float], T]) -> T:
        if self._closed:
            raise RuntimeError("SQLiteQueue is closed")
        return await asyncio.to_thread(self._transaction, operation)

    async def put(self, job_id: str, priority: int | float) -> None:
        job_id = _text(job_id, "job id")
        priority = _number(priority, "priority")

        def insert(connection, now):
            connection.execute(
                """INSERT INTO local_queue_jobs(job_id, priority, enqueued_at) VALUES (?, ?, ?)
                   ON CONFLICT(job_id) DO UPDATE SET priority=excluded.priority""",
                (job_id, priority, now),
            )

        await self._run(insert)

    async def pop(self) -> str | None:
        def remove(connection, _now):
            row = connection.execute(
                "SELECT sequence, job_id FROM local_queue_jobs ORDER BY priority DESC, sequence ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute("DELETE FROM local_queue_jobs WHERE sequence=?", (row[0],))
            return row[1]

        return await self._run(remove)

    async def lock(self, key: str, ttl: int | float) -> str | None:
        key = _text(key, "lock key")
        ttl = _number(ttl, "lock TTL", positive=True)
        owner = str(uuid4())

        def acquire(connection, now):
            result = connection.execute(
                """INSERT INTO local_queue_locks(lock_key, owner, expires_at) VALUES (?, ?, ?)
                   ON CONFLICT(lock_key) DO UPDATE SET owner=excluded.owner, expires_at=excluded.expires_at
                   WHERE local_queue_locks.expires_at <= ?""",
                (key, owner, now + ttl, now),
            )
            return owner if result.rowcount == 1 else None

        return await self._run(acquire)

    async def unlock(self, key: str, owner: str) -> None:
        key, owner = _text(key, "lock key"), _text(owner, "lock owner")
        await self._run(
            lambda connection, _now: (
                connection.execute(
                    "DELETE FROM local_queue_locks WHERE lock_key=? AND owner=?", (key, owner)
                ).rowcount
            )
        )

    async def renew(self, key: str, owner: str, ttl: int | float) -> bool:
        """Extend only an unexpired owned lease, without releasing it to competitors."""
        key, owner = _text(key, "lock key"), _text(owner, "lock owner")
        ttl = _number(ttl, "lock TTL", positive=True)
        return await self._run(
            lambda connection, now: (
                connection.execute(
                    "UPDATE local_queue_locks SET expires_at=? WHERE lock_key=? AND owner=? AND expires_at>?",
                    (now + ttl, key, owner, now),
                ).rowcount
                == 1
            )
        )

    async def provider_slot(self, provider: str, concurrency: int, ttl: int) -> tuple[str, str] | None:
        provider = _text(provider, "provider")
        if type(concurrency) is not int or concurrency != 1:
            raise ValueError("The local collector requires per-brand concurrency=1")
        key = f"hotelbug:slot:{provider}:0"
        owner = await self.lock(key, ttl)
        return (key, owner) if owner else None

    async def rate_limit(self, provider: str, seconds: int | float) -> float:
        provider = _text(provider, "provider")
        interval = _number(seconds, "rate interval", positive=True)

        def reserve(connection, now):
            row = connection.execute(
                "SELECT next_allowed_at FROM local_queue_limits WHERE provider=?", (provider,)
            ).fetchone()
            if row is not None and row[0] > now:
                return float(row[0] - now)
            connection.execute(
                """INSERT INTO local_queue_limits(provider, next_allowed_at) VALUES (?, ?)
                   ON CONFLICT(provider) DO UPDATE SET next_allowed_at=excluded.next_allowed_at""",
                (provider, now + interval),
            )
            return 0.0

        return await self._run(reserve)

    @staticmethod
    def read_report(path: str | Path, *, now: float | None = None) -> dict:
        """Read status without creating/migrating a database or clearing expiry."""
        target = Path(path).expanduser().resolve()
        if not target.exists():
            return {"initialized": False, "queued_jobs": 0, "active_locks": 0, "rate_limits": 0}
        stamp = _number(time.time() if now is None else now, "clock")
        connection = sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            # Use one snapshot even if another process changes rows between counts.
            connection.execute("BEGIN")
            return {
                "initialized": True,
                "queued_jobs": connection.execute("SELECT COUNT(*) FROM local_queue_jobs").fetchone()[0],
                "active_locks": connection.execute(
                    "SELECT COUNT(*) FROM local_queue_locks WHERE expires_at>?", (stamp,)
                ).fetchone()[0],
                "rate_limits": connection.execute(
                    "SELECT COUNT(*) FROM local_queue_limits WHERE next_allowed_at>?", (stamp,)
                ).fetchone()[0],
            }
        finally:
            connection.close()

    async def report(self) -> dict:
        if self._closed:
            raise RuntimeError("SQLiteQueue is closed")
        return await asyncio.to_thread(self.read_report, self.path, now=self.clock())

    async def close(self) -> None:
        self._closed = True
