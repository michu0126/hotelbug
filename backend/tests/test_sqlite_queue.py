import asyncio
import multiprocessing
import sqlite3
from concurrent.futures import ProcessPoolExecutor

import pytest

from app.services.sqlite_queue import SQLiteQueue


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


async def test_renew_does_not_release_owned_lease(tmp_path):
    clock = Clock()
    queue = SQLiteQueue(tmp_path / "queue.sqlite", clock=clock)
    token = await queue.lock("runner", 10)
    clock.now += 5
    assert await queue.renew("runner", token, 20)
    assert not await queue.renew("runner", "other", 20)
    clock.now += 6
    assert await queue.lock("runner", 10) is None
    clock.now += 20
    assert not await queue.renew("runner", token, 20)
    assert await queue.lock("runner", 10)
    await queue.close()


async def test_persistent_priority_deduplication_and_fifo(tmp_path):
    path = tmp_path / "queue.sqlite"
    first = SQLiteQueue(path)
    await first.put("first", 5)
    await first.put("second", 5)
    await first.put("priority", 99.5)
    await first.put("first", 5)
    await first.put("updated", 1)
    await first.put("updated", 6)
    await first.close()
    second = SQLiteQueue(path)
    assert [await second.pop() for _ in range(5)] == ["priority", "updated", "first", "second", None]
    # Once popped, a scheduler replay is a new FIFO entry, not silently lost.
    await second.put("priority", 2)
    await second.put("first", 2)
    assert await second.pop() == "priority"
    await second.close()


async def test_independent_connections_pop_each_job_once(tmp_path):
    path = tmp_path / "queue.sqlite"
    left, right = SQLiteQueue(path), SQLiteQueue(path)
    for index in range(40):
        await left.put(str(index), index)
    results = await asyncio.gather(*(queue.pop() for queue in [left, right] * 30))
    jobs = [item for item in results if item is not None]
    assert len(jobs) == len(set(jobs)) == 40
    assert set(jobs) == {str(index) for index in range(40)}
    await left.close()
    await right.close()


async def test_lock_expiry_persists_and_stale_owner_cannot_unlock(tmp_path):
    clock = Clock()
    path = tmp_path / "queue.sqlite"
    first = SQLiteQueue(path, clock=clock)
    old = await first.lock("lease", 10)
    assert old is not None
    await first.close()
    second = SQLiteQueue(path, clock=clock)
    assert await second.lock("lease", 10) is None
    clock.now += 10
    current = await second.lock("lease", 20)
    assert current and current != old
    await second.unlock("lease", old)
    assert await second.lock("lease", 10) is None
    await second.unlock("lease", current)
    assert await second.lock("lease", 10)
    await second.close()


async def test_lock_atomic_across_instances(tmp_path):
    path = tmp_path / "queue.sqlite"
    queues = [SQLiteQueue(path) for _ in range(4)]
    owners = await asyncio.gather(*(queues[index % 4].lock("lease", 60) for index in range(16)))
    assert sum(owner is not None for owner in owners) == 1
    for queue in queues:
        await queue.close()


async def test_rate_limit_is_atomic_persistent_and_does_not_extend_wait(tmp_path):
    clock = Clock()
    path = tmp_path / "queue.sqlite"
    left, right = SQLiteQueue(path, clock=clock), SQLiteQueue(path, clock=clock)
    results = await asyncio.gather(*(queue.rate_limit("marriott", 5) for queue in [left, right] * 8))
    assert results.count(0) == 1
    assert results.count(5) == 15
    clock.now += 2
    assert await left.rate_limit("marriott", 5) == 3
    assert await left.rate_limit("hyatt", 5) == 0
    await left.close()
    await right.close()
    reopened = SQLiteQueue(path, clock=clock)
    assert await reopened.rate_limit("marriott", 5) == 3
    clock.now += 3
    assert await reopened.rate_limit("marriott", 5) == 0
    assert await reopened.rate_limit("hyatt", 5) == 2
    await reopened.close()


async def test_provider_slot_is_group_specific_and_never_more_than_one(tmp_path):
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    first = await queue.provider_slot("marriott", 1, 60)
    assert first
    assert await queue.provider_slot("marriott", 1, 60) is None
    assert await queue.provider_slot("hyatt", 1, 60)
    await queue.unlock(*first)
    assert await queue.provider_slot("marriott", 1, 60)
    for concurrency in (0, 2, True, 1.0):
        with pytest.raises(ValueError, match="concurrency=1"):
            await queue.provider_slot("marriott", concurrency, 60)
    await queue.close()


async def test_report_is_read_only_and_close_is_idempotent(tmp_path):
    path = tmp_path / "nested" / "queue.sqlite"
    assert SQLiteQueue.read_report(path) == {
        "initialized": False,
        "queued_jobs": 0,
        "active_locks": 0,
        "rate_limits": 0,
    }
    assert not path.parent.exists()
    clock = Clock()
    queue = SQLiteQueue(path, clock=clock)
    await queue.put("one", 1)
    await queue.lock("lease", 5)
    await queue.rate_limit("marriott", 5)
    assert await queue.report() == {
        "initialized": True,
        "queued_jobs": 1,
        "active_locks": 1,
        "rate_limits": 1,
    }
    clock.now += 5
    assert (await queue.report())["active_locks"] == 0
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM local_queue_locks").fetchone()[0] == 1
    await queue.close()
    await queue.close()
    with pytest.raises(RuntimeError, match="closed"):
        await queue.pop()
    with pytest.raises(RuntimeError, match="closed"):
        await queue.report()


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), True, "1"])
async def test_invalid_numeric_input_is_not_persisted(tmp_path, value):
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    with pytest.raises(ValueError):
        await queue.put("job", value)
    with pytest.raises(ValueError):
        await queue.lock("lease", value)
    with pytest.raises(ValueError):
        await queue.rate_limit("marriott", value)
    assert (await queue.report())["queued_jobs"] == 0
    await queue.close()


@pytest.mark.parametrize("value", [0, -1])
async def test_ttl_and_interval_must_be_positive(tmp_path, value):
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    with pytest.raises(ValueError):
        await queue.lock("lease", value)
    with pytest.raises(ValueError):
        await queue.rate_limit("marriott", value)
    await queue.put("negative_priority_allowed", value)
    assert await queue.pop() == "negative_priority_allowed"
    await queue.close()


@pytest.mark.parametrize("value", [None, "", "bad\x00key"])
async def test_invalid_identity_is_rejected(tmp_path, value):
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    with pytest.raises(ValueError):
        await queue.put(value, 1)
    with pytest.raises(ValueError):
        await queue.lock(value, 10)
    with pytest.raises(ValueError):
        await queue.rate_limit(value, 10)
    await queue.close()


def _separate_process_worker(path):
    async def run():
        queue = SQLiteQueue(path)
        owner = await queue.lock("cross-process", 60)
        wait = await queue.rate_limit("cross-process", 60)
        jobs = []
        while (job := await queue.pop()) is not None:
            jobs.append(job)
        await queue.close()
        return owner, wait, jobs

    return asyncio.run(run())


async def test_real_processes_share_atomic_locks_rates_and_queue(tmp_path):
    path = tmp_path / "queue.sqlite"
    queue = SQLiteQueue(path)
    for index in range(30):
        await queue.put(f"job-{index}", 1)
    await queue.close()

    def run_workers():
        # spawn exercises the same non-inherited connection path on Windows
        # and Linux; this is not an in-memory fake or a thread-only test.
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
            return list(pool.map(_separate_process_worker, [str(path)] * 3))

    results = await asyncio.to_thread(run_workers)
    assert sum(owner is not None for owner, _, _ in results) == 1
    assert sum(wait == 0 for _, wait, _ in results) == 1
    jobs = [job for _, _, items in results for job in items]
    assert len(jobs) == len(set(jobs)) == 30
    assert SQLiteQueue.read_report(path)["queued_jobs"] == 0


def test_memory_database_is_rejected():
    with pytest.raises(ValueError, match="persistent"):
        SQLiteQueue(":memory:")
