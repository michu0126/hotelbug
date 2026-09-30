"""Compare standard Chrome startup with Playwright startup using an isolated profile."""

import argparse
import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--proxy")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--warmup-url")
    args = parser.parse_args()
    executable = shutil.which("google-chrome") or shutil.which("chromium")
    if os.name == "nt":
        executable = str(Path(os.environ["PROGRAMFILES"]) / "Google/Chrome/Application/chrome.exe")
    if not executable or not Path(executable).is_file():
        raise RuntimeError("Standard Chrome executable not found")
    with TemporaryDirectory(prefix="hotelbug-browser-test-") as profile:
        command = [
            executable,
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "about:blank",
        ]
        if not args.headed:
            command.append("--headless=new")
        if args.proxy:
            command.append(f"--proxy-server={args.proxy}")
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(
            command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, startupinfo=startup
        )
        try:
            port_file = Path(profile) / "DevToolsActivePort"
            for _ in range(100):
                if port_file.exists():
                    break
                if process.poll() is not None:
                    raise RuntimeError("Standard Chrome exited during startup")
                await asyncio.sleep(0.1)
            port = port_file.read_text().splitlines()[0]
            async with async_playwright() as playwright:
                browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                context = browser.contexts[0]
                page = await context.new_page()
                if args.warmup_url:
                    home = await page.goto(args.warmup_url, wait_until="domcontentloaded", timeout=45000)
                    await page.wait_for_timeout(12000)
                    print("warmup=", home.status if home else None, ascii(await page.title()), flush=True)
                response = await page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
                print("status=", response.status if response else None, flush=True)
                await page.wait_for_timeout(12000)
                print(
                    json.dumps(
                        {
                            "url": page.url,
                            "title": await page.title(),
                            "body": (await page.locator("body").inner_text())[:15000],
                        },
                        ensure_ascii=True,
                    ),
                    flush=True,
                )
                await browser.close()
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
