"""Real Chromium, entirely intercepted fixtures: never a live hotel request."""

import json

import pytest
from playwright.async_api import async_playwright

from scripts.capture_har import capture


@pytest.mark.parametrize("case", ["json", "403", "captcha"])
async def test_actual_chromium_flushes_full_har_and_stops_on_denial(tmp_path, case):
    requests = []

    async def configure(context):
        async def route_request(route):
            requests.append(route.request.url)
            if route.request.url.endswith("fixture.json"):
                await route.fulfill(status=200, content_type="application/json", body='{"synthetic":true}')
            else:
                html = {
                    "json": '<h1>Offline fixture</h1><script>fetch("/fixture.json")</script>',
                    "403": "<h1>Access Denied</h1>",
                    "captcha": "<h1>Please complete the captcha</h1>",
                }[case]
                await route.fulfill(status=403 if case == "403" else 200, content_type="text/html", body=html)

        await context.route("**/*", route_request)

    async with async_playwright() as playwright:
        result = await capture(
            playwright, tmp_path / "capture", seconds=0.8, headless=True, configure_context=configure
        )
    assert result["har_saved"] and result["errors"] == []
    har = json.loads((tmp_path / "capture" / "capture.har").read_text(encoding="utf-8"))
    assert har["log"]["entries"] and har["log"]["entries"][0]["response"]["content"]["text"]
    if case == "json":
        assert result["json_responses"] == 1
        assert result["stop_reason"] == "DURATION_FINISHED"
        manifest = json.loads((tmp_path / "capture" / "manifest.json").read_text())
        assert json.loads((tmp_path / "capture" / manifest[0]["file"]).read_text()) == {"synthetic": True}
    else:
        assert result["stop_reason"] == "BRAND_PAUSED"
        assert result["pause_reason"] == ("HTTP_403" if case == "403" else "VISIBLE_CHALLENGE")
        assert len(requests) == 1
