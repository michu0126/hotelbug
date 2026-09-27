from uuid import uuid4

from redis.asyncio import Redis

READY = "hotelbug:ready"
RELEASE = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end"
RATE_LIMIT = """
local t=redis.call('TIME')
local now=t[1]*1000+math.floor(t[2]/1000)
local next=tonumber(redis.call('GET',KEYS[1]) or '0')
if next>now then return next-now end
redis.call('SET',KEYS[1],now+tonumber(ARGV[1]),'PX',ARGV[1])
return 0
"""


class Queue:
    def __init__(self, redis: Redis):
        self.redis = redis

    async def put(self, job_id: str, priority: int) -> None:
        await self.redis.zadd(READY, {job_id: -priority})

    async def pop(self) -> str | None:
        result = await self.redis.zpopmin(READY)
        if not result:
            return None
        value = result[0][0]
        return value.decode() if isinstance(value, bytes) else value

    async def lock(self, key: str, ttl: int) -> str | None:
        owner = str(uuid4())
        return owner if await self.redis.set(key, owner, nx=True, ex=ttl) else None

    async def unlock(self, key: str, owner: str) -> None:
        await self.redis.eval(RELEASE, 1, key, owner)

    async def provider_slot(self, provider: str, concurrency: int, ttl: int) -> tuple[str, str] | None:
        for slot in range(concurrency):
            key = f"hotelbug:slot:{provider}:{slot}"
            token = await self.lock(key, ttl)
            if token:
                return key, token
        return None

    async def rate_limit(self, provider: str, seconds: int) -> float:
        milliseconds = await self.redis.eval(RATE_LIMIT, 1, f"hotelbug:limit:{provider}", seconds * 1000)
        return float(milliseconds) / 1000
