import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from test_models_adapters import HOTEL, STAY

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from configuration import RequestPolicy
from runtime import BrandPaused, BrandRunner, synchronize_capture_pause
from storage import Storage

NOW = datetime(2030, 1, 1, tzinfo=timezone.utc)


async def test_per_brand_concurrency_is_one_even_with_parallel_callers():
    store = Storage(":memory:")
    running, maximum = 0, 0

    async def fetch(*_, **__):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        await asyncio.sleep(0.01)
        running -= 1
        return []

    adapter = AsyncMock()
    adapter.fetch_rates.side_effect = fetch
    runner = BrandRunner(store, RequestPolicy(delay_min_seconds=0, delay_max_seconds=0))
    await asyncio.gather(*(runner.fetch(adapter, HOTEL, STAY) for _ in range(4)))
    assert maximum == 1 and adapter.fetch_rates.await_count == 4
    store.close()


async def test_random_delay_and_exponential_backoff_are_bounded():
    store = Storage(":memory:")
    sleeps = AsyncMock()
    adapter = AsyncMock()
    adapter.fetch_rates.side_effect = [TransientFailure(), asyncio.TimeoutError(), []]
    runner = BrandRunner(
        store, RequestPolicy(delay_min_seconds=1, delay_max_seconds=3), sleep=sleeps, uniform=lambda a, b: b
    )
    assert await runner.fetch(adapter, HOTEL, STAY) == []
    assert [call.args[0] for call in sleeps.await_args_list] == [3, 2, 3, 4, 3]
    store.close()


@pytest.mark.parametrize("error", [BrandBlocked("403 or captcha"), RateLimited(90)])
async def test_denial_pauses_without_retrying_and_does_not_clear_on_new_runner(tmp_path, error):
    path = tmp_path / "pause.sqlite"
    store = Storage(path)
    adapter = AsyncMock()
    adapter.fetch_rates.side_effect = error
    policy = RequestPolicy(delay_min_seconds=0, delay_max_seconds=0)
    runner = BrandRunner(store, policy, clock=lambda: NOW)
    with pytest.raises(BrandPaused):
        await runner.fetch(adapter, HOTEL, STAY)
    assert adapter.fetch_rates.await_count == 1
    store.close()
    store = Storage(path)
    runner = BrandRunner(store, policy, clock=lambda: NOW + timedelta(seconds=89))
    with pytest.raises(BrandPaused):
        await runner.fetch(adapter, HOTEL, STAY)
    assert adapter.fetch_rates.await_count == 1
    if isinstance(error, RateLimited):
        assert not store.paused("hilton", NOW + timedelta(seconds=90))
    else:
        assert store.paused("hilton", NOW + timedelta(days=365))
    store.close()


async def test_retries_stop_without_inventing_zero_price():
    store = Storage(":memory:")
    adapter = AsyncMock()
    adapter.fetch_rates.side_effect = TransientFailure("network")
    runner = BrandRunner(store, RequestPolicy(delay_min_seconds=0, delay_max_seconds=0), sleep=AsyncMock())
    with pytest.raises(TransientFailure):
        await runner.fetch(adapter, HOTEL, STAY)
    assert adapter.fetch_rates.await_count == 3
    assert store.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
    store.close()


def test_live_mode_honors_capture_denial_until_explicit_review_and_resume(tmp_path):
    marker = tmp_path / "hilton-paused.json"
    marker.write_text('{"pause_reason":"HTTP_403"}')
    store = Storage(":memory:")
    synchronize_capture_pause(store, marker)
    assert store.paused("hilton", NOW) and marker.exists()
    synchronize_capture_pause(store, marker, resume=True)
    store.resume("hilton")
    assert not marker.exists() and not store.paused("hilton", NOW)
    assert len(list(tmp_path.glob("hilton-paused-reviewed-*.json"))) == 1
    store.close()
