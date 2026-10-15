"""Slow official-site reads must not stall the autonomous scheduler."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings
from app.scheduler import main as scheduler


class Clients:
    def __init__(self):
        self.catalog = object()
        self.telegram = object()
        self.update = AsyncMock()
        self.close = AsyncMock()


@pytest.mark.parametrize("slow_lane", ["catalog", "telegram"])
async def test_slow_network_lane_does_not_block_other_lane_or_heartbeat(
    sessions, queue, monkeypatch, slow_lane
):
    catalog_started = asyncio.Event()
    dispatch_started = asyncio.Event()
    notification_started = asyncio.Event()
    heartbeat_started = asyncio.Event()
    never = asyncio.Event()

    async def discover(*args):
        catalog_started.set()
        if slow_lane == "catalog":
            await never.wait()

    async def dispatch(*args):
        dispatch_started.set()

    async def notification(*args):
        notification_started.set()
        if slow_lane == "telegram":
            await never.wait()

    async def heartbeat(*args, **kwargs):
        assert args[0] == "hotelbug:heartbeat:scheduler"
        assert kwargs["ex"] == 60
        heartbeat_started.set()

    clients = []

    def new_clients():
        instance = Clients()
        clients.append(instance)
        return instance

    monkeypatch.setattr(scheduler, "RuntimeHttpClients", new_clients)
    monkeypatch.setattr(scheduler, "discover_accor", discover)
    monkeypatch.setattr(scheduler, "tick", dispatch)
    monkeypatch.setattr(scheduler, "deliver_one", notification)
    redis = AsyncMock()
    redis.set.side_effect = heartbeat
    task = asyncio.create_task(scheduler.run_scheduler(sessions, queue, Settings(), redis))
    try:
        async with asyncio.timeout(3):
            await catalog_started.wait()
            await dispatch_started.wait()
            await notification_started.wait()
            await heartbeat_started.wait()
        assert not task.done()
        assert len(clients) == 2
        # A settings refresh in one lane cannot close the other's active pool.
        assert clients[0].catalog is not clients[1].catalog
        for client in clients:
            client.close.assert_not_awaited()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    for client in clients:
        client.close.assert_awaited_once()
