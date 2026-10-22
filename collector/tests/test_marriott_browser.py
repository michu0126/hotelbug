"""Persistent Chromium + normal homepage workflow. All external requests intercepted."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott import RATES_OPERATION, RATES_PATH, MarriottAdapter, MarriottSampleError
from adapters.marriott_browser import HOMEPAGE, day_label
from configuration import BrowserSettings

CI, CO = "2026-10-07", "2026-10-08"
FIXTURE = Path(__file__).parent / "fixtures" / "marriott_observed.json"
HOTEL_PATH = "/en-us/hotels/nycmq-new-york-marriott-marquis/overview/"
NAME = "New York Marriott Marquis"


def settings():
    return BrowserSettings(headless=True, channel=None, navigation_timeout_ms=1500)


def request_body(adults=2):
    return {
        "operationName": RATES_OPERATION,
        "variables": {
            "search": {
                "propertyId": "NYCMQ",
                "options": {
                    "startDate": CI,
                    "endDate": CO,
                    "quantity": 1,
                    "numberInParty": adults,
                    "childAges": [],
                },
            }
        },
    }


def fixture_route(seen, *, adults=2, early=False):
    async def route(route):
        seen.append((route.request.method, route.request.url))
        path = urlsplit(route.request.url).path
        if route.request.url == HOMEPAGE:
            html = f"""<h1>Offline Marriott homepage</h1>
                <input aria-label="Destination"><div id="downshift-0-item-0">{NAME}</div>
                <button onclick="location.href='/fixture-search'">Find Hotels</button>"""
            if early:
                html += f"<script>fetch('{RATES_PATH}',{{method:'POST',body:JSON.stringify({json.dumps(request_body())})}})</script>"
        elif path == "/fixture-search":
            html = f'''<div class="property-card-container"><button>{NAME}</button>
                <a href="{HOTEL_PATH}">Hotel website</a></div>'''
        elif path == HOTEL_PATH:
            from datetime import date

            labels = [day_label(date.fromisoformat(value)) for value in (CI, CO)]
            html = f'''<h1>{NAME}</h1>
                <button onclick="document.querySelector('#calendar').style.display='block'">STAY DATES</button>
                <div id="calendar" style="display:none">
                  <button role="gridcell" aria-label="{labels[0]}">Check in</button>
                  <button role="gridcell" aria-label="{labels[1]}">Check out</button>
                  <button onclick="document.querySelector('#calendar').style.display='none'">Done</button>
                </div><button>1 Room, 2 Guests</button>
                <button onclick="rates()">View Rates</button>
                <script>
                document.cookie='fixture_profile=retained; Max-Age=3600; Path=/';
                async function rates(){{await fetch('{RATES_PATH}',{{method:'POST',
                headers:{{'content-type':'application/json'}},body:JSON.stringify({json.dumps(request_body(adults))})}});}}
                </script>'''
        elif path == RATES_PATH:
            body = json.loads(FIXTURE.read_text(encoding="utf-8"))["rates"]
            await route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return
        else:
            raise AssertionError("Unexpected outbound fixture request: " + path)
        await route.fulfill(status=200, content_type="text/html", body=html)

    return route


async def test_homepage_hotel_dates_clicks_and_profile_cookie_survives_reopen(tmp_path):
    seen, contexts, cookies = [], [], []

    async def configure(context):
        contexts.append(context)
        cookies.append(await context.cookies(HOMEPAGE))
        await context.route("**/*", fixture_route(seen, early=True))

    profile = tmp_path / "marriott-profile"
    adapter = MarriottAdapter(
        mode="live", browser_settings=settings(), configure_context=configure, profile_dir=profile
    )
    try:
        (first,) = await adapter.fetch_prices("NYCMQ", CI, CO)
        (second,) = await adapter.fetch_prices("NYCMQ", CI, CO)
        assert first.source_kind == second.source_kind == "LIVE"
        assert first.captured_at < second.captured_at and first.record_id != second.record_id
        assert first.series_key == second.series_key and first.member_rate and first.adults == 2
        assert len(contexts) == 2 and contexts[0] is not contexts[1]
        assert not cookies[0]
        assert any(c["name"] == "fixture_profile" and c["value"] == "retained" for c in cookies[1])
        assert adapter.diagnostics["profile_dir"] == str(profile.resolve())
        assert [s["stage"] for s in adapter.diagnostics["steps"]] == [
            "HOMEPAGE",
            "SEARCH_HOTEL",
            "OPEN_HOTEL",
            "SELECT_DATES",
            "VIEW_RATES",
        ]
        assert adapter.diagnostics["stage"] == "COMPLETE"
        assert all("availabilitySearch.mi" not in url for method, url in seen)
        assert seen[0] == ("GET", HOMEPAGE)
        assert adapter._live._context is None and adapter._live._profile_lock is None
    finally:
        await adapter.close()


@pytest.mark.parametrize("case", ["403", "429", "captcha", "timeout"])
async def test_homepage_denial_stops_with_no_alternate_url_or_sample(tmp_path, case):
    seen = []

    async def configure(context):
        async def route(route):
            seen.append(route.request.url)
            await route.fulfill(
                status=int(case) if case in ("403", "429") else 200,
                headers={"retry-after": "123"},
                content_type="text/html",
                body="<h1>Please complete the captcha</h1>"
                if case == "captcha"
                else "<h1>Offline fixture</h1>",
            )

        await context.route("**/*", route)

    adapter = MarriottAdapter(
        mode="live",
        browser_settings=settings(),
        configure_context=configure,
        profile_dir=tmp_path / "profile",
    )
    try:
        expected = (
            BrandBlocked if case in ("403", "captcha") else RateLimited if case == "429" else TransientFailure
        )
        with pytest.raises(expected) as caught:
            await adapter.fetch_prices("NYCMQ", CI, CO)
        if case == "429":
            assert caught.value.retry_after == 123
        assert seen == [HOMEPAGE]
        assert adapter._live._context is None and adapter._live._profile_lock is None
    finally:
        await adapter.close()


def test_live_mode_and_samples_are_mutually_exclusive():
    with pytest.raises(ValueError):
        MarriottAdapter([FIXTURE], mode="live")


async def test_cancellation_releases_persistent_context_and_profile_lock(tmp_path):
    async def configure(context):
        await context.route(
            "**/*", lambda route: route.fulfill(content_type="text/html", body="<h1>Offline waiting</h1>")
        )

    adapter = MarriottAdapter(
        mode="live",
        browser_settings=settings(),
        configure_context=configure,
        profile_dir=tmp_path / "profile",
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.fetch_prices("NYCMQ", CI, CO), 1)
        assert adapter._live._context is None and adapter._live._profile_lock is None
    finally:
        await adapter.close()


async def test_wrong_page_request_occupancy_is_not_a_price(tmp_path):
    async def configure(context):
        await context.route("**/*", fixture_route([], adults=1))

    adapter = MarriottAdapter(
        mode="live",
        browser_settings=settings(),
        configure_context=configure,
        profile_dir=tmp_path / "profile",
    )
    try:
        with pytest.raises(MarriottSampleError, match="occupancy"):
            await adapter.fetch_prices("NYCMQ", CI, CO)
    finally:
        await adapter.close()


async def test_late_403_during_context_close_still_overrides_timeout(tmp_path, monkeypatch):
    from playwright.async_api import Error as BrowserError

    from adapters import marriott_browser

    callbacks, closed = {}, []

    class Page:
        url = HOMEPAGE

        def set_default_timeout(self, value):
            pass

        def is_closed(self):
            return False

        async def title(self):
            return "Offline fixture"

    class Context:
        pages = [Page()]

        def on(self, name, callback):
            callbacks[name] = callback

        async def close(self):
            callbacks["response"](
                SimpleNamespace(
                    status=403,
                    url=HOMEPAGE,
                    headers={},
                    request=SimpleNamespace(url=HOMEPAGE, resource_type="document", method="GET"),
                )
            )
            closed.append(True)

    adapter = marriott_browser.MarriottLivePage(settings(), profile_dir=tmp_path / "profile")

    async def start():
        adapter._context = Context()

    async def flow(*args):
        raise BrowserError("Synthetic initial timeout")

    async def no_challenge(page):
        return False

    monkeypatch.setattr(adapter, "_start", start)
    monkeypatch.setattr(adapter, "_homepage_hotel_dates", flow)
    monkeypatch.setattr(marriott_browser, "visible_challenge", no_challenge)
    try:
        with pytest.raises(BrandBlocked):
            await adapter.fetch_prices("NYCMQ", CI, CO)
        assert closed == [True] and adapter.diagnostics["responses"][-1]["status"] == 403
    finally:
        await adapter.close()


async def test_launch_uses_same_persistent_profile_local_chrome_and_headed_settings(tmp_path, monkeypatch):
    from adapters import marriott_browser

    launches, closed = [], []

    class Context:
        browser = None

        async def close(self):
            closed.append(True)

    class Chromium:
        async def launch_persistent_context(self, directory, **kwargs):
            launches.append((directory, kwargs))
            return Context()

    class Driver:
        chromium = Chromium()

        async def stop(self):
            pass

    class Manager:
        async def start(self):
            return Driver()

    monkeypatch.setattr(marriott_browser, "async_playwright", Manager)
    profile = tmp_path / ".profiles" / "marriott"
    adapter = marriott_browser.MarriottLivePage(
        BrowserSettings(channel="chrome", headless=False, proxy="http://192.168.50.4:7890"),
        profile_dir=profile,
    )
    for _ in range(2):
        await adapter._start()
        await adapter.close()
    assert len(closed) == 2 and len(launches) == 2
    assert launches[0] == launches[1]
    assert launches[0][0] == str(profile.resolve())
    assert launches[0][1]["channel"] == "chrome" and launches[0][1]["headless"] is False
    assert launches[0][1]["proxy"] == {"server": "http://192.168.50.4:7890"}
    assert adapter._profile_lock is None
