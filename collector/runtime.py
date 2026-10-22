"""Serial brand execution, durable pauses and bounded transient retries."""

import asyncio
import random
from datetime import timedelta
from uuid import uuid4

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from models import utcnow


class BrandPaused(Exception):
    pass


def synchronize_capture_pause(storage, path, *, resume=False):
    """Do not silently bypass the capture tool's recorded Hilton denial."""
    if not path.exists():
        return
    if resume:
        # Preserve the original denial evidence when the user explicitly resumes.
        path.rename(path.with_name("hilton-paused-reviewed-" + uuid4().hex[:12] + ".json"))
    else:
        storage.pause("hilton", "CAPTURE_RECORDED_ACCESS_DENIAL_REQUIRES_EXPLICIT_RESUME")


class BrandRunner:
    def __init__(self, storage, policy, *, clock=utcnow, sleep=asyncio.sleep, uniform=random.uniform):
        self.storage, self.policy, self.clock = storage, policy, clock
        self.sleep, self.uniform = sleep, uniform
        self.locks = {}

    async def fetch(self, adapter, hotel, stay, *, confirmation=False):
        lock = self.locks.setdefault(hotel.brand, asyncio.Lock())
        async with lock:
            if self.storage.paused(hotel.brand, self.clock()):
                raise BrandPaused(f"{hotel.brand} is paused")
            for attempt in range(self.policy.max_attempts):
                await self.sleep(self.uniform(self.policy.delay_min_seconds, self.policy.delay_max_seconds))
                try:
                    return await asyncio.wait_for(
                        adapter.fetch_rates(hotel, stay, confirmation=confirmation),
                        timeout=self.policy.timeout_seconds,
                    )
                except BrandBlocked:
                    self.storage.pause(hotel.brand, "HTTP_403_OR_VISIBLE_CHALLENGE")
                    raise BrandPaused(f"{hotel.brand} paused after an access denial") from None
                except RateLimited as exc:
                    self.storage.pause(
                        hotel.brand, "HTTP_429", self.clock() + timedelta(seconds=max(1, exc.retry_after))
                    )
                    raise BrandPaused(f"{hotel.brand} paused after a rate limit") from None
                except (TransientFailure, asyncio.TimeoutError):
                    if attempt == self.policy.max_attempts - 1:
                        raise TransientFailure("Bounded collection attempts exhausted") from None
                    ceiling = min(
                        self.policy.backoff_max_seconds, self.policy.backoff_base_seconds * 2**attempt
                    )
                    await self.sleep(self.uniform(ceiling / 2, ceiling))
