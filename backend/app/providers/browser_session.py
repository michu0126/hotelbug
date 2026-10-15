"""Keep a dedicated official-site browser session across Worker jobs."""

import asyncio
import errno
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from app.core.errors import ErrorCode, ProviderError

_sessions = {}
_locks = {}


def startup_error(exc: Exception) -> ProviderError:
    """Classify local startup failures without exposing launch commands or secrets.

    Only fixed diagnostic text is persisted. Playwright errors can include
    command arguments, proxy credentials, profile paths and environment values.
    These hints do not reset profiles, remove locks, or change retry policy.
    """
    if isinstance(exc, (BrowserTimeout, TimeoutError)):
        return ProviderError(ErrorCode.TIMEOUT, "Dedicated browser startup timed out")
    if isinstance(exc, OSError) and exc.errno in {
        errno.EAGAIN,
        errno.ENOMEM,
        errno.EMFILE,
        errno.ENFILE,
    }:
        return ProviderError(
            ErrorCode.BROWSER_STARTUP_FAILED, "Dedicated browser startup temporarily lacked local resources"
        )
    if isinstance(exc, PermissionError):
        return ProviderError(ErrorCode.BROWSER_UNAVAILABLE, "Browser startup permission denied")
    if isinstance(exc, BrowserError):
        detail = str(exc).casefold()
        hints = (
            (
                ("appears to be in use by another chromium process", "profile is already in use"),
                "Browser profile is already in use; verify its owning process before recovery",
            ),
            (
                ("failed to create a processsingleton",),
                "Browser profile lock could not be created; check ownership and profile permissions",
            ),
            (
                ("missing x server or $display", "without having a xserver running", "cannot open display"),
                "Browser virtual display is unavailable; check the Worker display process",
            ),
            (
                ("executable doesn't exist", "chromium distribution 'chrome' is not found"),
                "Browser executable is missing; check the browser-enabled Worker image",
            ),
            (
                ("error while loading shared libraries", "host system is missing dependencies"),
                "Browser system libraries are missing; check the browser-enabled Worker image",
            ),
        )
        for patterns, message in hints:
            if any(pattern in detail for pattern in patterns):
                return ProviderError(ErrorCode.BROWSER_UNAVAILABLE, message)
    return ProviderError(ErrorCode.BROWSER_UNAVAILABLE, "Dedicated browser session could not start")


@dataclass
class BrowserSession:
    playwright: object
    browser: object
    context: object
    closed: bool = False
    last_used: float = 0
    idle_page: object | None = None

    def touch(self, *_):
        self.last_used = time.monotonic()

    def mark_closed(self, *_):
        self.closed = True

    @property
    def alive(self):
        return not self.closed and (self.browser is None or self.browser.is_connected())

    async def close(self):
        was_closed = self.closed
        self.closed = True
        try:
            if not was_closed:
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
            playwright = browser = context = None
            try:
                playwright = await async_playwright().start()
                options = {"headless": headless, "channel": browser_channel or "chromium"}
                if proxy_server:
                    parsed = urlsplit(proxy_server)
                    host = parsed.hostname or ""
                    if ":" in host:
                        host = f"[{host}]"
                    server = f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
                    options["proxy"] = {"server": server}
                    if parsed.username is not None:
                        options["proxy"]["username"] = unquote(parsed.username)
                    if parsed.password is not None:
                        options["proxy"]["password"] = unquote(parsed.password)
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
                # Headed persistent Chromium exits when its last tab closes.
                # Retain one startup blank as an owned keeper; idle cleanup
                # excludes only this exact page, never job pages or popups.
                idle_page = next((page for page in context.pages if page.url == "about:blank"), None)
                runtime = BrowserSession(playwright, browser, context)
                runtime.idle_page = idle_page
                runtime.touch()
                context.on("close", runtime.mark_closed)
                _sessions[key] = runtime
            except BaseException as exc:
                # A timed-out Worker task can cancel startup; do not orphan a browser/driver.
                # Cleanup failure must not hide the launch error or cancellation.
                for resource, method in ((context, "close"), (browser, "close"), (playwright, "stop")):
                    if resource is not None:
                        await asyncio.gather(getattr(resource, method)(), return_exceptions=True)
                if isinstance(exc, (BrowserError, OSError)):
                    raise startup_error(exc) from exc
                raise
        page = await runtime.context.new_page()
        runtime.touch()
        page.on("close", runtime.touch)
        return page


async def close_idle_browser_sessions(max_idle_seconds: float = 120) -> None:
    """Release idle browser processes, retaining their existing disk profiles.

    The same per-session lock protects page creation and eviction. All live
    context pages (including popups) protect a session from eviction.
    """
    loop_id = id(asyncio.get_running_loop())
    for key in list(_sessions):
        if key[0] != loop_id:
            continue
        async with _locks.setdefault(key, asyncio.Lock()):
            runtime = _sessions.get(key)
            if runtime is None or any(
                page is not runtime.idle_page or page.url != "about:blank" for page in runtime.context.pages
            ):
                continue
            if time.monotonic() - runtime.last_used < max_idle_seconds:
                continue
            try:
                await runtime.close()
            except BrowserError:
                pass  # Disconnected browsers can reject close; still stop the driver.
            _sessions.pop(key, None)


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
