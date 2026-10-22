"""External CDP ownership contract; no real hotel or user-browser requests."""

import asyncio
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout
from playwright.async_api import async_playwright

from app.core.errors import ErrorCode, ProviderError
from app.providers import browser_session as pool


class FakePage:
    def __init__(self):
        self.handlers = {}
        self.closed = False
        self.close = AsyncMock(side_effect=self._close)

    def on(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def emit(self, event, *args):
        for callback in self.handlers.get(event, []):
            callback(*args)

    def is_closed(self):
        return self.closed

    async def _close(self):
        self.closed = True
        self.emit("close")


def external_runtime(monkeypatch):
    user = FakePage()
    context = MagicMock()
    context.pages = [user]
    context.close = AsyncMock()

    async def new_page():
        page = FakePage()
        context.pages.append(page)
        return page

    context.new_page = AsyncMock(side_effect=new_page)
    browser = MagicMock()
    browser.contexts = [context]
    browser.is_connected.return_value = True
    browser.close = AsyncMock()
    driver = MagicMock()
    driver.stop = AsyncMock()
    driver.chromium.connect_over_cdp = AsyncMock(return_value=browser)
    driver.chromium.launch = AsyncMock()
    driver.chromium.launch_persistent_context = AsyncMock()
    starter = MagicMock()
    starter.start = AsyncMock(return_value=driver)
    monkeypatch.setattr(pool, "async_playwright", MagicMock(return_value=starter))
    cookie_loader = MagicMock(side_effect=AssertionError("External Chrome must not import saved cookies"))
    monkeypatch.setattr(pool, "load_connection", cookie_loader)
    return starter, driver, browser, context, user


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "http://localhost:9222",
        "https://127.0.0.1:9222",
        "http://192.168.50.4:9222",
        "ws://127.0.0.1:9222/devtools/browser",
        "http://127.0.0.1:9222/json/version",
        "http://127.0.0.1:9222?token=secret",
        "http://user:secret@127.0.0.1:9222",
        "http://127.0.0.1:9222#fragment",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:9222\n",
        " http://127.0.0.1:9222",
    ],
)
def test_external_endpoint_is_only_explicit_loopback_http_port(value):
    with pytest.raises(ValueError, match="127.0.0.1"):
        with pool.external_browser(value):
            pass
    assert pool._external_endpoint.get() is None


def test_external_context_is_nested_and_restored_after_exception():
    with pool.external_browser("http://127.0.0.1:9222/"):
        assert pool._external_endpoint.get() == "http://127.0.0.1:9222"
        with pytest.raises(RuntimeError):
            with pool.external_browser("http://127.0.0.1:9223"):
                assert pool._external_endpoint.get() == "http://127.0.0.1:9223"
                raise RuntimeError("operation failed")
        assert pool._external_endpoint.get() == "http://127.0.0.1:9222"
    assert pool._external_endpoint.get() is None


async def test_external_context_does_not_change_other_task_routing():
    entered, finish = asyncio.Event(), asyncio.Event()

    async def scoped():
        with pool.external_browser("http://127.0.0.1:9222"):
            entered.set()
            await finish.wait()
            assert pool._external_endpoint.get() == "http://127.0.0.1:9222"

    task = asyncio.create_task(scoped())
    await entered.wait()
    assert pool._external_endpoint.get() is None
    finish.set()
    await task


async def test_external_groups_share_connection_but_only_use_new_pages(monkeypatch, tmp_path):
    starter, driver, browser, context, user = external_runtime(monkeypatch)
    try:
        with pool.external_browser("http://127.0.0.1:9222"):
            pages = await asyncio.gather(
                pool.new_provider_page("ihg", session_dir=str(tmp_path), headless=False),
                pool.new_provider_page("accor", proxy_server="http://127.0.0.1:7890"),
            )
        assert len(set(pages)) == 2 and user not in pages
        starter.start.assert_awaited_once()
        driver.chromium.connect_over_cdp.assert_awaited_once_with("http://127.0.0.1:9222", timeout=15000)
        driver.chromium.launch.assert_not_awaited()
        driver.chromium.launch_persistent_context.assert_not_awaited()
        assert context.new_page.await_count == 2
        assert not list(tmp_path.iterdir())
        context.add_cookies.assert_not_called()
        context.clear_cookies.assert_not_called()
        browser.close.assert_not_awaited()
    finally:
        await pool.close_browser_sessions()
    user.close.assert_not_awaited()
    assert all(page.close.await_count == 1 for page in pages)
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()
    driver.stop.assert_awaited_once()


async def test_shutdown_closes_owned_popups_not_unrelated_tabs(monkeypatch):
    _, driver, browser, context, user = external_runtime(monkeypatch)
    with pool.external_browser("http://127.0.0.1:9222"):
        page = await pool.new_provider_page("hilton")
    popup, nested, unrelated = FakePage(), FakePage(), FakePage()
    context.pages.extend([popup, nested, unrelated])
    page.emit("popup", popup)
    popup.emit("popup", nested)
    await pool.close_browser_sessions()
    for owned in (page, popup, nested):
        owned.close.assert_awaited_once()
    for unowned in (user, unrelated):
        unowned.close.assert_not_awaited()
    browser.close.assert_not_awaited()
    context.close.assert_not_awaited()
    driver.stop.assert_awaited_once()


async def test_idle_disconnect_waits_for_owned_popup_but_not_user_pages(monkeypatch):
    _, driver, browser, context, user = external_runtime(monkeypatch)
    try:
        with pool.external_browser("http://127.0.0.1:9222"):
            page = await pool.new_provider_page("hilton")
        popup = FakePage()
        page.emit("popup", popup)
        context.pages.append(popup)
        await page.close()
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        driver.stop.assert_not_awaited()
        await popup.close()
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        driver.stop.assert_awaited_once()
        assert not pool._sessions
        user.close.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()
    finally:
        await pool.close_browser_sessions()


@pytest.mark.parametrize("count", [0, 2])
async def test_ambiguous_context_disconnects_without_closing_browser(monkeypatch, count):
    _, driver, browser, context, user = external_runtime(monkeypatch)
    browser.contexts = [context] * count
    with pool.external_browser("http://127.0.0.1:9222"):
        with pytest.raises(ProviderError, match="exactly one"):
            await pool.new_provider_page("gha")
    assert not pool._sessions
    driver.stop.assert_awaited_once()
    context.new_page.assert_not_awaited()
    context.close.assert_not_awaited()
    browser.close.assert_not_awaited()
    user.close.assert_not_awaited()
    await pool.close_browser_sessions()


@pytest.mark.parametrize(
    "error", [BrowserError("private detail"), BrowserTimeout("private detail"), asyncio.CancelledError()]
)
async def test_attach_failure_stops_driver_and_never_launches_replacement(monkeypatch, error):
    _, driver, browser, context, _ = external_runtime(monkeypatch)
    driver.chromium.connect_over_cdp.side_effect = error
    expected = asyncio.CancelledError if isinstance(error, asyncio.CancelledError) else ProviderError
    with pool.external_browser("http://127.0.0.1:9222"):
        with pytest.raises(expected) as caught:
            await pool.new_provider_page("gha")
    if isinstance(caught.value, ProviderError):
        assert "private detail" not in str(caught.value)
        assert caught.value.code == (
            ErrorCode.TIMEOUT if isinstance(error, BrowserTimeout) else ErrorCode.BROWSER_UNAVAILABLE
        )
    assert not pool._sessions
    driver.stop.assert_awaited_once()
    driver.chromium.launch.assert_not_awaited()
    browser.close.assert_not_awaited()
    context.close.assert_not_awaited()
    await pool.close_browser_sessions()


async def test_owned_page_close_error_still_detaches_and_preserves_user(monkeypatch):
    _, driver, browser, context, user = external_runtime(monkeypatch)
    with pool.external_browser("http://127.0.0.1:9222"):
        first = await pool.new_provider_page("gha")
        second = await pool.new_provider_page("ihg")
    first.close.side_effect = BrowserError("Target already closed")
    await pool.close_browser_sessions()
    second.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    user.close.assert_not_awaited()
    browser.close.assert_not_awaited()
    context.close.assert_not_awaited()


async def test_lost_connection_reattaches_same_endpoint_without_closing_external_context(monkeypatch):
    starter, driver, browser, context, user = external_runtime(monkeypatch)
    try:
        with pool.external_browser("http://127.0.0.1:9222"):
            await pool.new_provider_page("gha")
            browser.on.call_args.args[1]()
            await pool.new_provider_page("gha")
        assert starter.start.await_count == 2
        assert driver.chromium.connect_over_cdp.await_count == 2
        assert {call.args for call in driver.chromium.connect_over_cdp.await_args_list} == {
            ("http://127.0.0.1:9222",)
        }
        user.close.assert_not_awaited()
        context.close.assert_not_awaited()
        browser.close.assert_not_awaited()
    finally:
        await pool.close_browser_sessions()


async def test_real_local_chrome_disconnect_preserves_preexisting_tabs(tmp_path):
    """Only our own temporary Chrome/profile and blank pages; no hotel traffic."""
    chrome = next(
        (
            Path(root) / "Google/Chrome/Application/chrome.exe"
            for root in (os.getenv("PROGRAMFILES", ""), os.getenv("PROGRAMFILES(X86)", ""))
            if root and (Path(root) / "Google/Chrome/Application/chrome.exe").is_file()
        ),
        None,
    )
    if os.name != "nt" or chrome is None:
        pytest.skip("Local installed Windows Chrome smoke")
    with socket.socket() as port_picker:
        port_picker.bind(("127.0.0.1", 0))
        port = port_picker.getsockname()[1]
    endpoint = f"http://127.0.0.1:{port}"
    process = subprocess.Popen(
        [
            str(chrome),
            "--headless=new",
            "--disable-background-networking",
            "--disable-component-update",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_path / 'external-smoke-profile'}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    probe = browser = None
    try:
        probe = await async_playwright().start()
        for attempt in range(60):
            try:
                browser = await probe.chromium.connect_over_cdp(endpoint, timeout=1000)
                break
            except BrowserError:
                if process.poll() is not None or attempt == 59:
                    raise
                await asyncio.sleep(0.1)
        context = browser.contexts[0]
        user = context.pages[0]
        await user.set_content("<title>Keep this preexisting tab</title><p>Owner content</p>")
        other = await context.new_page()
        await other.set_content("<title>Keep another owner tab</title>")
        with pool.external_browser(endpoint):
            owned = await pool.new_provider_page("ihg")
            await owned.set_content("<title>Task-owned page</title>")
            async with owned.expect_popup() as appeared:
                await owned.evaluate("window.open('about:blank')")
            popup = await appeared.value
            await popup.set_content("<title>Task-owned popup</title>")
        await pool.close_browser_sessions()
        for _ in range(50):
            if len(context.pages) == 2:
                break
            await asyncio.sleep(0.02)
        assert browser.is_connected() and process.poll() is None
        assert len(context.pages) == 2
        assert await user.title() == "Keep this preexisting tab"
        assert await other.title() == "Keep another owner tab"
        assert await user.locator("p").inner_text() == "Owner content"
    finally:
        await pool.close_browser_sessions()
        if browser is not None:
            # This separate probe is the owner of this temporary test Chrome.
            await browser.close()
        if probe is not None:
            await probe.stop()
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
