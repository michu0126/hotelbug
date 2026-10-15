import asyncio
import errno
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as BrowserError
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers import browser_session as pool


def fake_runtime(monkeypatch):
    context = MagicMock()
    context.close = AsyncMock()
    context.pages = []
    context.new_page = AsyncMock(side_effect=lambda: MagicMock())
    browser = MagicMock()
    browser.is_connected.return_value = True
    browser.close = AsyncMock()
    browser.new_context = AsyncMock(return_value=context)
    driver = MagicMock()
    driver.stop = AsyncMock()
    driver.chromium.launch = AsyncMock(return_value=browser)
    driver.chromium.launch_persistent_context = AsyncMock(return_value=context)
    starter = MagicMock()
    starter.start = AsyncMock(return_value=driver)
    monkeypatch.setattr(pool, "async_playwright", MagicMock(return_value=starter))
    return starter, driver, browser, context


async def test_tasks_share_context_and_close_only_at_shutdown(monkeypatch):
    starter, driver, browser, context = fake_runtime(monkeypatch)
    try:
        await asyncio.gather(pool.new_provider_page("test"), pool.new_provider_page("test"))
        starter.start.assert_awaited_once()
        assert context.new_page.await_count == 2
        context.close.assert_not_awaited()
    finally:
        await pool.close_browser_sessions()
    context.close.assert_awaited_once()
    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()


async def test_profile_persists_and_isolates_provider_and_proxy(monkeypatch, tmp_path):
    _, driver, _, _ = fake_runtime(monkeypatch)
    try:
        for provider, proxy in (("test", None), ("other", None), ("test", "http://127.0.0.1:7890")):
            await pool.new_provider_page(provider, proxy_server=proxy, session_dir=str(tmp_path))
        paths = [call.args[0] for call in driver.chromium.launch_persistent_context.await_args_list]
        assert len(set(paths)) == 3
        await pool.close_browser_sessions()
        await pool.new_provider_page("test", session_dir=str(tmp_path))
        assert driver.chromium.launch_persistent_context.await_args.args[0] == paths[0]
    finally:
        await pool.close_browser_sessions()


async def test_closed_context_restarts_next_task(monkeypatch):
    starter, _, _, context = fake_runtime(monkeypatch)
    try:
        await pool.new_provider_page("test")
        event, on_close = context.on.call_args.args
        assert event == "close"
        on_close()
        await pool.new_provider_page("test")
        assert starter.start.await_count == 2
    finally:
        await pool.close_browser_sessions()


async def test_http_proxy_credentials_are_passed_separately(monkeypatch):
    _, driver, _, _ = fake_runtime(monkeypatch)
    try:
        await pool.new_provider_page("test", proxy_server="http://name:encoded%40password@127.0.0.1:7890")
        assert driver.chromium.launch.await_args.kwargs["proxy"] == {
            "server": "http://127.0.0.1:7890",
            "username": "name",
            "password": "encoded@password",
        }
    finally:
        await pool.close_browser_sessions()


@pytest.mark.parametrize("error", [OSError("startup failed"), asyncio.CancelledError()])
async def test_failed_or_cancelled_launch_stops_driver(monkeypatch, error):
    _, driver, _, _ = fake_runtime(monkeypatch)
    driver.chromium.launch.side_effect = error
    expected = ProviderError if isinstance(error, OSError) else asyncio.CancelledError
    try:
        with pytest.raises(expected) as caught:
            await pool.new_provider_page("test")
        if isinstance(error, OSError):
            assert caught.value.code == ErrorCode.BROWSER_UNAVAILABLE
        driver.stop.assert_awaited_once()
    finally:
        await pool.close_browser_sessions()


async def test_shutdown_closes_other_groups_after_one_failure(monkeypatch):
    _, driver, _, context = fake_runtime(monkeypatch)
    await pool.new_provider_page("test")
    await pool.new_provider_page("other")
    context.close.side_effect = [RuntimeError("close failed"), None]
    with pytest.raises(RuntimeError, match="close failed"):
        await pool.close_browser_sessions()
    assert driver.stop.await_count == 2
    assert not pool._sessions


async def test_shutdown_skips_context_already_confirmed_closed_but_stops_driver(monkeypatch):
    _, driver, browser, context = fake_runtime(monkeypatch)
    runtime = pool.BrowserSession(driver, browser, context)
    runtime.mark_closed()  # The actual context close event, not an inferred timeout.
    context.close.side_effect = BrowserError("Target page, context or browser has been closed")
    await runtime.close()
    context.close.assert_not_awaited()
    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()


@pytest.mark.parametrize(
    "error,code",
    [
        (BrowserTimeout("launch timed out"), ErrorCode.TIMEOUT),
        (TimeoutError("driver startup timed out"), ErrorCode.TIMEOUT),
        *[
            (OSError(number, "local resource shortage"), ErrorCode.BROWSER_STARTUP_FAILED)
            for number in (errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE)
        ],
        (FileNotFoundError(errno.ENOENT, "missing executable"), ErrorCode.BROWSER_UNAVAILABLE),
        (PermissionError(errno.EACCES, "profile not writable"), ErrorCode.BROWSER_UNAVAILABLE),
        (BrowserError("Executable doesn't exist"), ErrorCode.BROWSER_UNAVAILABLE),
        (BrowserError("Failed to create a ProcessSingleton"), ErrorCode.BROWSER_UNAVAILABLE),
    ],
)
async def test_startup_classification_and_same_profile_recovery(monkeypatch, tmp_path, error, code):
    _, driver, _, context = fake_runtime(monkeypatch)
    driver.chromium.launch_persistent_context.side_effect = [error, context]
    try:
        with pytest.raises(ProviderError) as caught:
            await pool.new_provider_page("test", session_dir=str(tmp_path))
        assert caught.value.code == code
        assert caught.value.__cause__ is error
        assert not pool._sessions
        driver.stop.assert_awaited_once()
        await pool.new_provider_page("test", session_dir=str(tmp_path))
        first, second = driver.chromium.launch_persistent_context.await_args_list
        assert first == second  # No profile reset, channel/proxy change or fallback flags.
    finally:
        await pool.close_browser_sessions()


async def test_driver_start_failure_is_classified(monkeypatch):
    starter, driver, _, _ = fake_runtime(monkeypatch)
    starter.start.side_effect = OSError(errno.EAGAIN, "cannot start subprocess")
    with pytest.raises(ProviderError) as caught:
        await pool.new_provider_page("test")
    assert caught.value.code == ErrorCode.BROWSER_STARTUP_FAILED
    driver.stop.assert_not_awaited()
    assert not pool._sessions
    await pool.close_browser_sessions()


@pytest.mark.parametrize(
    "detail,message",
    [
        (
            "The profile appears to be in use by another Chromium process (1234) on another computer",
            "Browser profile is already in use; verify its owning process before recovery",
        ),
        (
            "Profile is already in use",
            "Browser profile is already in use; verify its owning process before recovery",
        ),
        (
            "Failed to create a ProcessSingleton for your profile directory",
            "Browser profile lock could not be created; check ownership and profile permissions",
        ),
        *[
            (text, "Browser virtual display is unavailable; check the Worker display process")
            for text in (
                "Missing X server or $DISPLAY",
                "Looks like you launched a headed browser without having a XServer running",
                "Cannot open display: :99",
            )
        ],
        *[
            (text, "Browser executable is missing; check the browser-enabled Worker image")
            for text in (
                "Executable doesn't exist at /private/path",
                "Chromium distribution 'chrome' is not found",
            )
        ],
        *[
            (text, "Browser system libraries are missing; check the browser-enabled Worker image")
            for text in (
                "error while loading shared libraries: libmissing.so",
                "Host system is missing dependencies to run browsers",
            )
        ],
        ("unrecognized launch failure", "Dedicated browser session could not start"),
    ],
)
async def test_startup_diagnostics_are_fixed_text_and_preserve_retry_policy(
    monkeypatch, tmp_path, detail, message
):
    _, driver, _, _ = fake_runtime(monkeypatch)
    # Raw launch errors must never be exposed through job/API messages.
    raw = BrowserError(
        detail + "\n--proxy-server=http://private-user:private-pass@proxy.invalid:7890?token=private-token"
    )
    driver.chromium.launch_persistent_context.side_effect = raw
    with pytest.raises(ProviderError) as caught:
        await pool.new_provider_page("test", session_dir=str(tmp_path), headless=False)
    assert caught.value.code == ErrorCode.BROWSER_UNAVAILABLE
    assert str(caught.value) == message
    assert caught.value.__cause__ is raw
    driver.stop.assert_awaited_once()
    assert not pool._sessions
    await pool.close_browser_sessions()


def test_startup_permission_diagnostic_does_not_expose_path():
    error = pool.startup_error(PermissionError(errno.EACCES, "denied", "/private/profile/path"))
    assert str(error) == "Browser startup permission denied"
    assert error.code == ErrorCode.BROWSER_UNAVAILABLE


async def test_launch_cause_survives_cleanup_error(monkeypatch):
    _, driver, browser, _ = fake_runtime(monkeypatch)
    cause = BrowserTimeout("context creation timed out")
    browser.new_context.side_effect = cause
    browser.close.side_effect = BrowserError("already disconnected")
    driver.stop.side_effect = OSError("driver already stopped")
    with pytest.raises(ProviderError) as caught:
        await pool.new_provider_page("test")
    assert caught.value.code == ErrorCode.TIMEOUT
    assert caught.value.__cause__ is cause
    browser.close.assert_awaited_once()
    driver.stop.assert_awaited_once()
    await pool.close_browser_sessions()


async def test_idle_sessions_close_only_after_timeout_and_reuse_disk_profile(monkeypatch, tmp_path):
    starter, driver, _, context = fake_runtime(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(pool.time, "monotonic", lambda: clock[0])
    try:
        page = await pool.new_provider_page("test", session_dir=str(tmp_path))
        first_profile = driver.chromium.launch_persistent_context.await_args.args[0]
        clock[0] = 180
        await pool.close_idle_browser_sessions()
        context.close.assert_not_awaited()
        # A closing task resets the idle age, rather than counting navigation time.
        page.on.call_args.args[1]()
        clock[0] = 299
        await pool.close_idle_browser_sessions()
        context.close.assert_not_awaited()
        clock[0] = 300
        await pool.close_idle_browser_sessions()
        context.close.assert_awaited_once()
        assert not pool._sessions
        await pool.new_provider_page("test", session_dir=str(tmp_path))
        assert starter.start.await_count == 2
        assert driver.chromium.launch_persistent_context.await_args.args[0] == first_profile
    finally:
        await pool.close_browser_sessions()


async def test_live_pages_and_popups_prevent_idle_eviction(monkeypatch):
    _, _, _, context = fake_runtime(monkeypatch)
    try:
        await pool.new_provider_page("test")
        context.pages = [MagicMock()]  # Even an unowned popup must protect the session.
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_not_awaited()
        context.pages = []
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_awaited_once()
    finally:
        await pool.close_browser_sessions()


async def test_idle_cleanup_waits_for_inflight_page_creation(monkeypatch):
    _, _, _, context = fake_runtime(monkeypatch)
    page = MagicMock()
    started, proceed = asyncio.Event(), asyncio.Event()

    async def create_page():
        started.set()
        await proceed.wait()
        context.pages.append(page)
        return page

    context.new_page.side_effect = create_page
    try:
        creating = asyncio.create_task(pool.new_provider_page("test"))
        await started.wait()
        cleanup = asyncio.create_task(pool.close_idle_browser_sessions(max_idle_seconds=0))
        await asyncio.sleep(0)
        assert not cleanup.done()
        proceed.set()
        await asyncio.gather(creating, cleanup)
        context.close.assert_not_awaited()
    finally:
        proceed.set()
        await pool.close_browser_sessions()


async def test_persistent_startup_retains_keeper_but_restored_pages_protect_session(monkeypatch, tmp_path):
    _, _, _, context = fake_runtime(monkeypatch)
    blank, restored = MagicMock(), MagicMock()
    blank.url, restored.url = "about:blank", "https://example.com/restored"
    blank.close, restored.close = AsyncMock(), AsyncMock()
    context.pages = [blank, restored]
    try:
        await pool.new_provider_page("test", session_dir=str(tmp_path))
        blank.close.assert_not_awaited()
        restored.close.assert_not_awaited()
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_not_awaited()
    finally:
        await pool.close_browser_sessions()


async def test_idle_cleanup_ignores_only_owned_startup_blank(monkeypatch, tmp_path):
    _, _, _, context = fake_runtime(monkeypatch)
    keeper, task_blank = MagicMock(), MagicMock()
    keeper.url = task_blank.url = "about:blank"
    keeper.close = AsyncMock()
    context.pages = [keeper]
    try:
        await pool.new_provider_page("test", session_dir=str(tmp_path), headless=False)
        keeper.close.assert_not_awaited()
        context.pages.append(task_blank)
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_not_awaited()
        context.pages.remove(task_blank)
        keeper.url = "https://example.com/restored"
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_not_awaited()
        keeper.url = "about:blank"
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        context.close.assert_awaited_once()
        assert not pool._sessions
    finally:
        await pool.close_browser_sessions()


async def test_disconnected_idle_browser_does_not_skip_other_groups(monkeypatch):
    _, driver, _, context = fake_runtime(monkeypatch)
    try:
        await pool.new_provider_page("test")
        await pool.new_provider_page("other")
        context.close.side_effect = [BrowserError("already disconnected"), None]
        await pool.close_idle_browser_sessions(max_idle_seconds=0)
        assert driver.stop.await_count == 2
        assert not pool._sessions
    finally:
        await pool.close_browser_sessions()
