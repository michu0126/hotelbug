"""Keep a dedicated official-site browser session across Worker jobs."""

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from app.core.errors import ErrorCode, ProviderError

_sessions = {}
_locks = {}


@dataclass
class BrowserSession:
    playwright: object
    browser: object
    context: object
    closed: bool = False

    def mark_closed(self, *_):
        self.closed = True

    @property
    def alive(self):
        return not self.closed and (self.browser is None or self.browser.is_connected())

    async def close(self):
        self.closed = True
        try:
            await self.context.close()
        finally:
            try:
                if self.browser:
                    await self.browser.close()
            finally:
                await self.playwright.stop()


async def new_provider_page(
    provider: str,
    *,
    proxy_server: str | None = None,
    browser_channel: str | None = None,
    headless: bool = True,
    session_dir: str | None = None,
):
    identity = (provider, proxy_server, browser_channel or "chromium", headless, session_dir)
    key = (id(asyncio.get_running_loop()), identity)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        runtime = _sessions.get(key)
        if runtime is not None and not runtime.alive:
            _sessions.pop(key, None)
            try:
                await runtime.close()
            except BrowserError:
                pass  # Already-disconnected contexts can reject close; the driver is still stopped.
            runtime = None
        if runtime is None:
            playwright = await async_playwright().start()
            options = {"headless": headless, "channel": browser_channel or "chromium"}
            if proxy_server:
                options["proxy"] = {"server": proxy_server}
            browser = None
            try:
                if session_dir:
                    suffix = hashlib.sha256(repr(identity).encode()).hexdigest()[:12]
                    profile = Path(session_dir) / f"{provider}-{suffix}"
                    profile.mkdir(parents=True, exist_ok=True)
                    context = await playwright.chromium.launch_persistent_context(
                        str(profile), locale="en-US", timezone_id="Asia/Shanghai", **options
                    )
                else:
                    browser = await playwright.chromium.launch(**options)
                    context = await browser.new_context(locale="en-US", timezone_id="Asia/Shanghai")
                runtime = BrowserSession(playwright, browser, context)
                context.on("close", runtime.mark_closed)
                _sessions[key] = runtime
            except BaseException as exc:
                # A timed-out Worker task can cancel startup; do not orphan a browser/driver.
                try:
                    if browser:
                        await browser.close()
                finally:
                    await playwright.stop()
                if isinstance(exc, (BrowserError, OSError)):
                    raise ProviderError(
                        ErrorCode.BROWSER_UNAVAILABLE, "Dedicated browser session could not start"
                    ) from exc
                raise
        return await runtime.context.new_page()


async def close_browser_sessions() -> None:
    loop_id = id(asyncio.get_running_loop())
    keys = [key for key in _sessions if key[0] == loop_id]
    runtimes = [_sessions.pop(key) for key in keys]
    for key in list(_locks):
        if key[0] == loop_id:
            _locks.pop(key, None)
    # Close every group even when one disconnected browser raises during shutdown.
    results = await asyncio.gather(*(runtime.close() for runtime in runtimes), return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            raise result
