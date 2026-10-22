"""Open an isolated official-site browser; preserve full HAR and JSON bodies."""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from playwright.async_api import Error as BrowserError
from playwright.async_api import async_playwright

from adapters.base import official_url
from adapters.hilton import SEARCH_URL
from adapters.page_policy import (
    JSON_BODY_TIMEOUT_SECONDS,
    PAGE_TIMEOUT_MS,
    is_official_request,
    visible_challenge,
)


class JsonSink:
    def __init__(self, directory: Path):
        self.directory = directory
        (directory / "json").mkdir(parents=True, exist_ok=False)
        self.entries = []
        self.tasks = set()
        self.sequence = 0

    def schedule(self, response):
        self.sequence += 1
        task = asyncio.create_task(self.save(response, self.sequence))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def save(self, response, sequence):
        content_type = response.headers.get("content-type", "").lower().split(";", 1)[0]
        declared_json = content_type == "application/json" or content_type.endswith("+json")
        if not declared_json and response.request.resource_type not in ("xhr", "fetch"):
            return
        entry = {
            "sequence": sequence,
            "url": response.url,
            "status": response.status,
            "method": response.request.method,
            "content_type": content_type,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            body = await asyncio.wait_for(response.body(), JSON_BODY_TIMEOUT_SECONDS)
            try:
                json.loads(body)
                valid = True
            except (ValueError, UnicodeDecodeError):
                if not declared_json:
                    return
                valid = False
            filename = f"json/{sequence:06d}.json"
            await asyncio.to_thread((self.directory / filename).write_bytes, body)
            entry.update(file=filename, size_bytes=len(body), valid_json=valid)
        except Exception as exc:
            # Preserve missing-body evidence instead of claiming a complete capture.
            entry.update(file=None, error=type(exc).__name__)
        self.entries.append(entry)

    async def finish(self):
        while self.tasks:
            await asyncio.gather(*list(self.tasks))
        manifest = sorted(self.entries, key=lambda item: item["sequence"])
        (self.directory / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return manifest


async def capture(
    playwright,
    output: Path,
    *,
    url=SEARCH_URL,
    seconds=0,
    headless=False,
    proxy=None,
    channel=None,
    configure_context=None,
):
    official_url("hilton", url)
    if seconds < 0 or (headless and seconds == 0):
        raise ValueError("Headless capture needs a positive --seconds duration")
    output.mkdir(parents=True, exist_ok=False)
    sink = JsonSink(output)
    browser = None
    context = None
    paused = asyncio.Event()
    reason = None
    errors = []
    stop_reason = "USER_CLOSED"

    def on_response(response):
        nonlocal reason
        sink.schedule(response)
        if is_official_request("hilton", response.request) and response.status in (403, 429):
            reason = f"HTTP_{response.status}"
            paused.set()

    try:
        browser = await playwright.chromium.launch(
            headless=headless, channel=channel, proxy={"server": proxy} if proxy else None
        )
        context = await browser.new_context(
            record_har_path=str(output / "capture.har"),
            record_har_mode="full",
            record_har_content="embed",
            record_har_omit_content=False,
        )
        context.on("response", on_response)
        if configure_context is not None:
            await configure_context(context)  # Offline test harness only; not exposed by CLI.
        page = await context.new_page()
        closed = asyncio.Event()
        page.on("close", lambda _: closed.set())
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        except BrowserError as exc:
            errors.append(type(exc).__name__)
        deadline = asyncio.get_running_loop().time() + seconds if seconds else None
        while not closed.is_set() and not paused.is_set():
            if deadline is not None and asyncio.get_running_loop().time() >= deadline:
                stop_reason = "DURATION_FINISHED"
                break
            try:
                if await visible_challenge(page):
                    reason = "VISIBLE_CHALLENGE"
                    paused.set()
                    break
            except BrowserError:
                if page.is_closed():
                    break
            try:
                await asyncio.wait_for(paused.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        if paused.is_set():
            stop_reason = "BRAND_PAUSED"
    except BrowserError as exc:
        errors.append(type(exc).__name__)
        stop_reason = "BROWSER_ERROR"
    except asyncio.CancelledError:
        stop_reason = "INTERRUPTED"
        raise
    finally:
        if context:
            context.remove_listener("response", on_response)
        manifest = []
        try:
            manifest = await sink.finish()
        except OSError as exc:
            errors.append(type(exc).__name__)
        try:
            if context:
                await context.close()  # Required: Playwright flushes the HAR here.
        except BrowserError as exc:
            errors.append(type(exc).__name__)
        finally:
            if browser:
                await browser.close()
        summary = {
            "brand": "hilton",
            "stop_reason": stop_reason,
            "pause_reason": reason,
            "json_responses": len(manifest),
            "body_read_errors": sum("error" in item for item in manifest),
            "errors": errors,
            "har_saved": (output / "capture.har").is_file(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }
        summary["capture_complete"] = (
            summary["har_saved"]
            and not errors
            and not summary["body_read_errors"]
            and stop_reason in ("USER_CLOSED", "DURATION_FINISHED")
        )
        (output / "session.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return summary


async def run(args):
    official_url("hilton", args.url)
    root = Path(args.output).resolve()
    root.mkdir(parents=True, exist_ok=True)
    pause_file = root / "hilton-paused.json"
    if pause_file.exists():
        if not args.resume_brand:
            raise SystemExit(
                "Hilton is paused. Review the previous session; explicitly use --resume-brand to resume."
            )
        pause_file.unlink()
    output = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8])
    print("只在新打开的官网窗口正常查询。完成后关闭该窗口，HAR 将自动保存。")
    print("样本含敏感信息，仅保存在本地，不会上传。遇到 403／验证码立即停止。")
    async with async_playwright() as playwright:
        result = await capture(
            playwright, output, url=args.url, seconds=args.seconds, proxy=args.proxy, channel=args.channel
        )
    if result["stop_reason"] == "BRAND_PAUSED":
        pause_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"directory": str(output), **result}, ensure_ascii=False))
    if not result["har_saved"]:
        raise SystemExit("HAR 未保存。请先安装 Playwright 浏览器；Windows 可尝试 --channel chrome。")


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=SEARCH_URL)
    parser.add_argument("--output", default=str(Path(__file__).resolve().parents[1] / "samples"))
    parser.add_argument(
        "--seconds", type=float, default=0, help="0: finish when you close the sampling window"
    )
    parser.add_argument("--proxy", help="Optional existing router proxy, e.g. http://192.168.50.4:7890")
    parser.add_argument(
        "--channel",
        choices=("chrome", "msedge", "chromium"),
        help="Optional installed browser, still using a new isolated profile",
    )
    parser.add_argument(
        "--resume-brand", action="store_true", help="Explicitly resume a reviewed paused capture"
    )
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    cli()
