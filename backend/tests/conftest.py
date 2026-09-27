import os

import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from redis.asyncio import Redis
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.tables import Base
from app.services.queue import Queue


@pytest_asyncio.fixture
async def sessions():
    url = os.getenv("TEST_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    if not url.startswith("sqlite"):
        assert make_url(url).database.endswith("_test"), "Refusing to reset a non-test database"
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest_asyncio.fixture
async def queue():
    url = os.getenv("TEST_REDIS_URL")
    if url:
        assert url.endswith("/15"), "Tests require isolated Redis DB 15"
    redis = Redis.from_url(url, decode_responses=True) if url else FakeRedis(decode_responses=True)
    await redis.flushdb()
    yield Queue(redis)
    await redis.flushdb()
    await redis.aclose()
