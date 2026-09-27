import asyncio


async def test_priority_and_deduplication(queue):
    await queue.put("low", 1)
    await queue.put("high", 90)
    await queue.put("high", 90)
    assert await queue.pop() == "high"
    assert await queue.pop() == "low"
    assert await queue.pop() is None


async def test_distributed_lock_owner_and_provider_isolation(queue):
    owner = await queue.lock("test-lock", 5)
    assert owner and await queue.lock("test-lock", 5) is None
    await queue.unlock("test-lock", "wrong-owner")
    assert await queue.lock("test-lock", 5) is None
    await queue.unlock("test-lock", owner)
    assert await queue.lock("test-lock", 5)
    first = await queue.provider_slot("marriott", 1, 30)
    assert first and await queue.provider_slot("marriott", 1, 30) is None
    assert await queue.provider_slot("hilton", 1, 30)


async def test_rate_limiter_atomic(queue):
    results = await asyncio.gather(*(queue.rate_limit("marriott", 5) for _ in range(4)))
    assert results.count(0) == 1
    assert await queue.rate_limit("hilton", 5) == 0
