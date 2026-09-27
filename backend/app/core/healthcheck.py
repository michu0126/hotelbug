import asyncio
import sys

from redis.asyncio import Redis

from app.core.config import get_settings


async def check(role: str) -> None:
    redis = Redis.from_url(get_settings().redis_url.get_secret_value())
    try:
        if not await redis.exists("hotelbug:heartbeat:" + role):
            raise SystemExit(1)
    finally:
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(check(sys.argv[1]))
