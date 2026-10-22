"""Actual local Chrome CDP connection, all page traffic fulfilled from a fixture.

Only this test harness drives a page. The passive collector sends no navigation,
click, JavaScript evaluation, API replay or booking command.
"""

import asyncio
import contextlib
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import websockets
from playwright.async_api import async_playwright

import passive_capture as pc


@pytest.mark.skipif(pc.find_chrome() is None, reason="Installed Chrome is required for local CDP smoke test")
async def test_actual_chrome_connection_and_passive_response_capture(tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    args = SimpleNamespace(
        port=port,
        launch=True,
        chrome=pc.find_chrome(),
        profile=str(tmp_path / "profile"),
        start_url="about:blank",
    )
    info = pc.ensure_chrome(args)
    process = getattr(args, "_launched_process", None)
    assert process is not None, "Test must own a newly launched Chrome, never attach to another browser"
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/marriott_cn_NYCTE_sanitized.json").read_text(encoding="utf-8")
    )
    output = tmp_path / "captures"
    browser = None
    try:
        async with websockets.connect(info["webSocketDebuggerUrl"], max_size=25_000_000, proxy=None) as ws:
            collector = pc.Collector(ws, output)
            commands = []
            original_send = collector.send

            async def recorded_send(method, params=None, session=None):
                commands.append(method)
                return await original_send(method, params, session)

            collector.send = recorded_send
            reader = asyncio.create_task(collector.reader())
            try:
                await collector.send(
                    "Target.setAutoAttach",
                    {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
                )
                for _ in range(60):
                    if collector.sessions:
                        break
                    await asyncio.sleep(0.05)
                assert collector.sessions, "Existing Chrome page must be attached before any fixture traffic"
                async with async_playwright() as driver:
                    browser = await driver.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                    context = browser.contexts[0]
                    seen = []

                    async def route(route):
                        seen.append(route.request.url)
                        path = urlsplit(route.request.url).path
                        if path in pc.TARGET_PATHS:
                            await route.fulfill(
                                content_type="application/json", body=json.dumps(fixture["response"])
                            )
                        else:
                            payload = {
                                "operationName": fixture["operation"],
                                "variables": fixture["variables"],
                            }
                            html = f"""<h1>Offline fixture: not a live hotel query</h1><script>
                                fetch('{next(iter(pc.TARGET_PATHS))}',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({json.dumps(payload)})}});
                                </script>"""
                            await route.fulfill(content_type="text/html", body=html)

                    await context.route("**/*", route)
                    await context.pages[0].goto(
                        "https://www.marriott.com.cn/offline-passive-fixture", wait_until="domcontentloaded"
                    )
                    for _ in range(100):
                        if collector.count:
                            break
                        await asyncio.sleep(0.05)
                    assert collector.count == 1
                    files = list(output.glob("*.json"))
                    assert len(files) == 1
                    record = json.loads(files[0].read_text(encoding="utf-8"))
                    assert record["property_id"] == "NYCTE" and record["source_site"] == "www.marriott.com.cn"
                    assert record["variables"]["search"]["options"]["customerId"] == "REDACTED"
                    assert pc.capture_event(record["capture_event"])["version"] == 1
                    assert (
                        len(
                            record["response"]["data"]["commerce"]["product"]["searchProductsByProperty"][
                                "edges"
                            ]
                        )
                        == 75
                    )
                    # A second identical response from a new browser request
                    # must survive deduplication for later price confirmation.
                    await context.pages[0].evaluate(
                        """async payload => {
                            const response = await fetch('/mi/query/PhoenixBookDTTSearchProductsByProperty', {
                                method: 'POST', headers: {'content-type': 'application/json'},
                                body: JSON.stringify(payload)
                            });
                            await response.json();
                        }""",
                        {"operationName": fixture["operation"], "variables": fixture["variables"]},
                    )
                    for _ in range(100):
                        if collector.count == 2:
                            break
                        await asyncio.sleep(0.05)
                    assert collector.count == 2
                    saved = [json.loads(p.read_text(encoding="utf-8")) for p in output.glob("*.json")]
                    assert len(saved) == 2 and pc.content_hash(saved[0]) == pc.content_hash(saved[1])
                    assert saved[0]["capture_event"]["event_id"] != saved[1]["capture_event"]["event_id"]
                    assert len(seen) == 3  # All three requests were intercepted before the network.
                    assert set(commands) <= {
                        "Target.setAutoAttach",
                        "Network.enable",
                        "Network.getResponseBody",
                        "Network.getRequestPostData",
                    }
                    await collector.close()
                    reader.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reader
                    # Only this test's isolated Chrome profile is owned/closed.
                    await browser.close()
                    browser = None
            finally:
                await collector.close()
                reader.cancel()
                with contextlib.suppress(asyncio.CancelledError, websockets.ConnectionClosed):
                    await reader
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
