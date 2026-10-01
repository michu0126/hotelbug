"""Verify real Chrome cookie/storage continuity between tasks and after restart."""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from tempfile import TemporaryDirectory
from threading import Thread

from app.providers.accor_browser import AccorBrowserProvider
from app.providers.browser_session import close_browser_sessions, new_provider_page


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        if self.path == "/seed":
            self.send_header("Set-Cookie", "hotelbug_test=present; Path=/; Max-Age=3600")
        self.end_headers()
        script = "localStorage.setItem('hotelbug_test','present');" if self.path == "/seed" else ""
        self.wfile.write(
            (
                "<body><script>"
                + script
                + "document.body.textContent=document.cookie+' '+localStorage.getItem('hotelbug_test');</script></body>"
            ).encode()
        )

    def log_message(self, *_):
        pass


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        import sys

        if not isinstance(sys.exception(), ConnectionResetError):
            super().handle_error(request, client_address)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--channel", default="chromium")
    args = parser.parse_args()
    server = Server(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with TemporaryDirectory(prefix="hotelbug-session-smoke-") as profile:

            async def open_page(provider="session-test"):
                return await new_provider_page(provider, browser_channel=args.channel, session_dir=profile)

            page = await open_page()
            await page.goto(url + "/seed")
            await page.close()
            page = await open_page()
            await page.goto(url + "/check")
            assert await page.locator("body").inner_text() == "hotelbug_test=present present"
            await page.close()
            print("next_job_cookie_and_storage=PASS", flush=True)
            pages = await asyncio.gather(open_page(), open_page())
            assert pages[0].context is pages[1].context
            await asyncio.gather(*(page.close() for page in pages))
            print("concurrent_tasks_share_context=PASS", flush=True)
            page = await open_page("other-provider")
            await page.goto(url + "/check")
            assert await page.locator("body").inner_text() == "null"
            await page.close()
            print("provider_isolation=PASS", flush=True)
            await close_browser_sessions()
            page = await open_page()
            await page.goto(url + "/check")
            assert await page.locator("body").inner_text() == "hotelbug_test=present present"
            await page.close()
            print("restart_cookie_and_storage=PASS", flush=True)
            await page.context.close()
            page = await open_page()
            await page.goto(url + "/check")
            assert await page.locator("body").inner_text() == "hotelbug_test=present present"
            await page.close()
            await close_browser_sessions()
            print("closed_context_recovers=PASS", flush=True)
            provider = AccorBrowserProvider(browser_channel=args.channel, session_dir=profile)
            page = await provider._get_page()
            await page.set_content("""<button onclick="this.textContent='Submitted'">See rates</button>
                <div style="position:fixed;inset:0;z-index:100;background:white">
                <button onclick="this.parentElement.remove()">Continue without Accepting</button></div>""")
            await page.get_by_role("button", name="See rates", exact=True).click(timeout=5000)
            assert await page.get_by_role("button", name="Submitted", exact=True).count() == 1
            await provider.close()
            await close_browser_sessions()
            print("late_consent_overlay_dismissed=PASS", flush=True)
            page = await new_provider_page("headed-runtime", browser_channel=args.channel, headless=False)
            await page.set_content("<h1>Ordinary Worker browser ready</h1>")
            assert await page.locator("h1").inner_text() == "Ordinary Worker browser ready"
            await page.close()
            await close_browser_sessions()
            print("headed_worker_browser_ready=PASS", flush=True)
    finally:
        await close_browser_sessions()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    asyncio.run(main())
