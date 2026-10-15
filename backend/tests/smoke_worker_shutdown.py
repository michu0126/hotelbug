"""Real browser shutdown/profile recovery through the Worker's SIGTERM handler.

Only localhost HTML and a temporary profile are used. No hotel requests, user
browser sessions, production database or Telegram configuration are involved.
"""

import argparse
import asyncio
import os
import signal
import time
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch

from smoke_browser_session import Handler, Server

from app.core.config import Settings
from app.crawler import worker
from app.providers.browser_session import close_browser_sessions, new_provider_page


class LocalRedis:
    closed = False

    async def set(self, *_args, **_kwargs):
        pass

    async def aclose(self):
        self.closed = True


class LocalHandler(Handler):
    def do_GET(self):
        if self.path in {"/seed", "/check"}:
            print("shutdown_test_local_http=" + self.path.removeprefix("/"), flush=True)
        super().do_GET()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="chromium")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--startup-timeout", type=int, default=60)
    args = parser.parse_args()
    if not 10 <= args.startup_timeout <= 300:
        parser.error("startup timeout must be between 10 and 300 seconds")
    server = Server(("127.0.0.1", 0), LocalHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with TemporaryDirectory(prefix="hotelbug-shutdown-smoke-") as profile:
            entered = asyncio.Event()
            job_closed = asyncio.Event()
            fixture_failed = asyncio.Event()
            fixture_errors = []
            context = None
            redis = LocalRedis()

            async def open_page():
                return await new_provider_page(
                    "shutdown-test",
                    browser_channel=args.channel,
                    session_dir=profile,
                    headless=not args.headed,
                )

            async def in_flight_job(*_):
                nonlocal context
                page = None
                try:
                    # The real consume loop catches job exceptions and retries.
                    # A smoke fixture must instead expose its first failure,
                    # not hide repeated navigations behind a readiness timeout.
                    if fixture_failed.is_set():
                        await asyncio.Event().wait()
                    print("shutdown_test_stage=browser_start", flush=True)
                    page = await open_page()
                    context = page.context
                    print("shutdown_test_stage=local_page", flush=True)
                    await page.goto(url + "/seed")
                    print("shutdown_test_stage=local_page_loaded", flush=True)
                    assert await page.locator("body").inner_text() == "hotelbug_test=present present"
                    print("shutdown_test_stage=waiting_for_signal", flush=True)
                    entered.set()
                    await asyncio.Event().wait()
                except Exception as exc:
                    fixture_errors.append(exc)
                    fixture_failed.set()
                    await asyncio.Event().wait()
                finally:
                    if page is not None:
                        await page.close()
                    job_closed.set()

            with (
                patch.object(worker, "get_settings", return_value=Settings(worker_concurrency=1)),
                patch.object(worker.Redis, "from_url", return_value=redis),
                patch.object(worker, "process_one", in_flight_job),
            ):
                task = asyncio.create_task(worker.main())
                readiness = asyncio.create_task(entered.wait())
                failure = asyncio.create_task(fixture_failed.wait())
                warmup_started = time.monotonic()
                try:
                    done, _ = await asyncio.wait(
                        (readiness, failure, task),
                        timeout=args.startup_timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if failure in done:
                        raise fixture_errors[0]
                    if task in done:
                        # Report an early Worker exception instead of waiting
                        # 60s and hiding it behind a misleading startup timeout.
                        await task
                        raise AssertionError("Worker exited before its local browser fixture was ready")
                    if readiness not in done:
                        raise TimeoutError("Local browser fixture did not become ready")
                    print(f"shutdown_test_warmup={time.monotonic() - warmup_started:.3f}s", flush=True)
                    started = time.monotonic()
                    print("shutdown_test_stage=sending_sigterm", flush=True)
                    if os.name == "posix":
                        os.kill(os.getpid(), signal.SIGTERM)
                    else:
                        signal.raise_signal(signal.SIGTERM)
                    await asyncio.wait_for(task, 14)
                    print("shutdown_test_stage=worker_stopped", flush=True)
                    assert job_closed.is_set() and redis.closed
                    assert context is not None and not context.pages
                    print(
                        f"worker_sigterm_browser_cleanup=PASS elapsed={time.monotonic() - started:.3f}s",
                        flush=True,
                    )
                finally:
                    readiness.cancel()
                    failure.cancel()
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, readiness, failure, return_exceptions=True)

            page = await open_page()
            assert page.context is not context
            await page.goto(url + "/check")
            assert await page.locator("body").inner_text() == "hotelbug_test=present present"
            await page.close()
            await close_browser_sessions()
            print("worker_sigterm_profile_reopen_cookie_and_storage=PASS", flush=True)
            print("hotel_requests=0 production_profiles=0", flush=True)
    finally:
        await close_browser_sessions()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
