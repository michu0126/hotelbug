"""Real Chromium + intercepted public-page fixtures; zero real hotel traffic."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from test_hilton_rates import HOTEL, STAY

from adapters.base import BrandBlocked, RateLimited
from adapters.hilton import HiltonAdapter
from adapters.hilton_urls import RATES_PATH, ROOMS_PATH
from configuration import BrowserSettings, RequestPolicy
from runtime import BrandPaused, BrandRunner
from storage import Storage


def rate_sample():
    return json.loads(
        (Path(__file__).parent / "fixtures/hilton_full_rates_sanitized.json").read_text(encoding="utf-8")
    )


def browser_settings():
    return BrowserSettings(headless=True, channel=None, navigation_timeout_ms=5000)


async def test_normal_room_clicks_observe_matching_public_plans_and_confirm_with_new_context():
    sample = rate_sample()
    requests, contexts = [], []

    async def configure(context):
        contexts.append(context)

        async def route_request(route):
            requests.append((route.request.method, route.request.url))
            path = urlsplit(route.request.url).path
            if path == ROOMS_PATH:
                assert parse_qs(urlsplit(route.request.url).query)["ctyhocn"] == [HOTEL.hotel_id]
                vars_rooms = {
                    key: value for key, value in sample["variables"].items() if key != "selectedRoomTypeCode"
                }
                payload = json.dumps(
                    {"operationName": "hotel_shopAvailOptions_shopPropAvail", "variables": vars_rooms}
                )
                html = f"""<h1>Offline Hilton fixture</h1>
                    <div data-testid="roomCardTile" data-roomtypecode="K1DV"><h2>江景大床豪华客房</h2>
                    <button data-testid="moreRatesButton" onclick="rates('K1DV')">更多房价</button></div>
                    <div data-testid="roomCardTile" data-roomtypecode="K1D"><h2>Offline second room</h2>
                    <button data-testid="moreRatesButton" onclick="rates('K1D')">更多房价</button></div>
                    <script>
                      const base={json.dumps(sample["variables"])};
                      fetch('/graphql/customer',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({payload})}});
                      async function rates(code){{
                        history.pushState(null,'','{RATES_PATH}');
                        document.body.innerHTML='<h1>Offline rate plans</h1><button data-testid="roomSelectedLabel" onclick="history.back()">修改房型</button>';
                        const variables={{...base,selectedRoomTypeCode:code}};
                        await fetch('/graphql/customer',{{method:'POST',headers:{{'content-type':'application/json'}},body:JSON.stringify({{operationName:'hotel_shopAvailOptions_shopPropAvail',variables}})}});
                      }}
                      const roomMarkup=document.body.innerHTML;
                      window.onpopstate=()=>{{ document.body.innerHTML=roomMarkup; }};
                    </script>"""
                await route.fulfill(status=200, content_type="text/html", body=html)
            elif path == "/graphql/customer":
                payload = route.request.post_data_json
                selected = payload["variables"].get("selectedRoomTypeCode")
                result = deepcopy(sample["response"])
                types = result["data"]["hotel"]["shopAvail"]["roomTypes"]
                if selected is None:
                    other = deepcopy(types[0])
                    other["roomTypeCode"] = "K1D"
                    types.append(other)
                elif selected == "K1D":
                    text = json.dumps(result).replace('"K1DV"', '"K1D"')
                    result = json.loads(text)
                await route.fulfill(status=200, content_type="application/json", body=json.dumps(result))
            else:
                raise AssertionError("Unexpected outbound request: " + path)

        await context.route("**/*", route_request)

    adapter = HiltonAdapter(
        browser_settings(),
        RequestPolicy(delay_min_seconds=0, delay_max_seconds=0),
        configure_context=configure,
    )
    try:
        first = await adapter.fetch_rates(HOTEL, STAY)
        second = await adapter.fetch_rates(HOTEL, STAY, confirmation=True)
        assert len(first) == len(second) == 6
        assert {q.room_code for q in first} == {"K1DV", "K1D"}
        assert all(q.membership_required is False and not q.is_mock for q in first)
        assert len(contexts) == 2 and contexts[0] is not contexts[1]
        assert {q.observation_id for q in first}.isdisjoint(q.observation_id for q in second)
        assert len([url for method, url in requests if method == "POST"]) == 6
    finally:
        await adapter.close()
    assert adapter._browser is None and adapter._playwright is None


@pytest.mark.parametrize("status", [403, 429])
async def test_access_denial_stops_without_alternate_transport_or_retry(status):
    requests = []

    async def configure(context):
        async def denied(route):
            requests.append(route.request.url)
            await route.fulfill(
                status=status,
                headers={"retry-after": "123"},
                content_type="text/html",
                body="<h1>Denied</h1>",
            )

        await context.route("**/*", denied)

    adapter = HiltonAdapter(browser_settings(), configure_context=configure)
    try:
        with pytest.raises(BrandBlocked if status == 403 else RateLimited) as caught:
            await adapter.fetch_rates(HOTEL, STAY)
        if status == 429:
            assert caught.value.retry_after == 123
        assert len(requests) == 1
    finally:
        await adapter.close()


async def test_visible_challenge_pauses_even_with_http_200():
    async def configure(context):
        await context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<h1>Please complete the captcha</h1>"
            ),
        )

    adapter = HiltonAdapter(browser_settings(), configure_context=configure)
    try:
        with pytest.raises(BrandBlocked):
            await asyncio.wait_for(adapter.fetch_rates(HOTEL, STAY), timeout=8)
    finally:
        await adapter.close()


async def test_cancelled_collection_closes_its_context_and_pending_observers():
    async def configure(context):
        await context.route(
            "**/*",
            lambda route: route.fulfill(
                status=200, content_type="text/html", body="<h1>Offline loading only</h1>"
            ),
        )

    adapter = HiltonAdapter(browser_settings(), configure_context=configure)
    try:
        await adapter._start()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(adapter.fetch_rates(HOTEL, STAY), timeout=0.4)
        assert adapter._browser.contexts == []
    finally:
        await adapter.close()


async def test_real_browser_denial_is_persisted_by_runner_with_no_retry(tmp_path):
    requests = []

    async def configure(context):
        async def deny(route):
            requests.append(route.request.url)
            await route.fulfill(status=403, content_type="text/html", body="<h1>Denied</h1>")

        await context.route("**/*", deny)

    store = Storage(tmp_path / "pause.sqlite")
    policy = RequestPolicy(delay_min_seconds=0, delay_max_seconds=0)
    adapter = HiltonAdapter(browser_settings(), policy, configure_context=configure)
    runner = BrandRunner(store, policy)
    try:
        with pytest.raises(BrandPaused):
            await runner.fetch(adapter, HOTEL, STAY)
        assert store.paused("hilton", adapter.clock())
        with pytest.raises(BrandPaused):
            await runner.fetch(adapter, HOTEL, STAY)
        assert len(requests) == 1
        assert adapter._browser.contexts == []
    finally:
        store.close()
        await adapter.close()
