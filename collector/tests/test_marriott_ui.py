"""Ordinary UI clicks in owned local Chrome; every request is served from fixtures.

These tests never attach to port 9222, contact a hotel, or close a user browser.
The JavaScript below is a synthetic website, not code injected by the driver.
"""

import asyncio
import json
import socket
from copy import deepcopy
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode, urlsplit

import pytest
from playwright.async_api import async_playwright

import passive_capture as pc
from adapters.base import BrandBlocked, RateLimited, TransientFailure
from adapters.marriott_ui import (
    CHINESE_MONTHS,
    HOST,
    MarriottUIDriver,
    calendar_month,
    matches_request,
    validate_hotel_url,
)

HOTEL_PATH = "/hotels/nycte-new-york-edition/overview/"
HOTEL_URL = f"https://{HOST}{HOTEL_PATH}"
PRICE_PATH = "/mi/query/PhoenixBookDTTSearchProductsByProperty"
FIXTURE_PATH = Path(__file__).parent / "fixtures/marriott_cn_NYCTE_sanitized.json"


def task(**changes):
    return SimpleNamespace(
        **{
            "hotel_id": "NYCTE",
            "hotel_url": HOTEL_URL,
            "checkin": date(2026, 10, 18),
            "checkout": date(2026, 10, 19),
            "adults": 1,
            **changes,
        }
    )


def request_body():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return {"operationName": fixture["operation"], "variables": fixture["variables"]}


def test_matching_request_requires_exact_hotel_dates_and_occupancy():
    payload = request_body()
    original = deepcopy(payload)
    assert matches_request(payload, task())
    assert payload == original  # Matching does not mutate the user's request.


@pytest.mark.parametrize(
    "path,value",
    [
        (("operationName",), "OtherOperation"),
        (("variables", "search", "propertyId"), "XIYBP"),
        (("variables", "search", "propertyId"), "nycte"),
        (("variables", "search", "options", "startDate"), "2026-10-19"),
        (("variables", "search", "options", "endDate"), "2026-10-20"),
        (("variables", "search", "options", "quantity"), 2),
        (("variables", "search", "options", "quantity"), True),
        (("variables", "search", "options", "quantity"), "1"),
        (("variables", "search", "options", "numberInParty"), 2),
        (("variables", "search", "options", "numberInParty"), True),
        (("variables", "search", "options", "numberInParty"), 1.0),
        (("variables", "search", "options", "childAges"), [4]),
        (("variables", "search", "options", "childAges"), None),
    ],
)
def test_mismatched_request_is_not_accepted(path, value):
    payload = request_body()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert not matches_request(payload, task())


@pytest.mark.parametrize("payload", [None, [], {}, {"variables": {}}, {"variables": {"search": None}}])
def test_missing_request_fields_are_ignored(payload):
    assert not matches_request(payload, task())


@pytest.mark.parametrize("month,name", list(enumerate(CHINESE_MONTHS, start=1)))
def test_chinese_calendar_month(month, name):
    assert calendar_month(f" \n{name}月 2027\n") == (2027, month)


@pytest.mark.parametrize("heading", ["十三月 2027", "October 2026", "十月26", "一月", "0月 2026"])
def test_ambiguous_calendar_month_is_not_guessed(heading):
    with pytest.raises(ValueError, match="calendar month"):
        calendar_month(heading)


@pytest.mark.parametrize(
    "url",
    [
        HOTEL_URL,
        f"https://{HOST}/hotels/nycte-new-york-edition/",
        f"https://{HOST}/hotels/travel/nycte-new-york-edition/overview",
    ],
)
def test_only_matching_public_hotel_overview_is_allowed(url):
    assert validate_hotel_url(url, "NYCTE") == url


@pytest.mark.parametrize(
    "url",
    [
        HOTEL_URL.replace("https:", "http:"),
        HOTEL_URL.replace(HOST, "www.marriott.com"),
        HOTEL_URL.replace(HOST, "www.marriott.com.cn.example.com"),
        HOTEL_URL.replace(HOST, f"user:password@{HOST}"),
        HOTEL_URL.replace(HOST, f"{HOST}:444"),
        HOTEL_URL.replace("nycte-", "xiybp-"),
        HOTEL_URL + "?checkin=2026-10-18",
        HOTEL_URL + "#booking",
        f"https://{HOST}/reservation/availabilitySearch.mi",
        f"https://{HOST}{PRICE_PATH}",
    ],
)
def test_booking_urls_and_unrelated_hosts_are_rejected(url):
    with pytest.raises(ValueError, match="public Marriott China overview"):
        validate_hotel_url(url, "NYCTE")


@pytest.fixture
async def isolated_chrome(tmp_path):
    if pc.find_chrome() is None:
        pytest.skip("Installed Chrome is required for the isolated UI smoke test")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port != 9222
    args = SimpleNamespace(
        port=port,
        launch=True,
        chrome=pc.find_chrome(),
        profile=str(tmp_path / "owned-ui-fixture-profile"),
        start_url="about:blank",
    )
    info = pc.ensure_chrome(args)
    process = getattr(args, "_launched_process", None)
    assert process is not None, "Must own this Chrome process before it can be closed"
    browser = None
    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.connect_over_cdp(info["webSocketDebuggerUrl"])
            context = browser.contexts[0]
            sentinel = context.pages[0]
            await sentinel.set_content("<h1>Existing user-like tab: leave unchanged</h1>")
            yield SimpleNamespace(
                port=port, process=process, browser=browser, context=context, sentinel=sentinel
            )
            await browser.close()  # Only the isolated profile created above is owned by this test.
            browser = None
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)


def hotel_html(*, sold_out=False, wrong_card=False, popup=False):
    payload = json.dumps(request_body())
    # Starting in Dec 2025 proves ordinary next-month clicks cross the year.
    return (
        r"""<!doctype html><meta charset="utf-8"><h1>Offline fixture hotel</h1>
    <button id="datesInput" onclick="calendar.style.display='block'">Dates</button>
    <div id="calendar" style="display:none"><div class="drp-calendar"><table>
      <thead><tr><th class="prev available" onclick="move(-1)">Previous</th>
        <th class="month"></th><th class="next available" onclick="move(1)">Next</th></tr></thead>
      <tbody id="days"></tbody></table></div>
      <button onclick="calendar.style.display='none'; events.push('apply-dates')">确认</button>
    </div>
    <button aria-label="Select number of guests dropdown"
      onclick="guests.style.display='block'">Guests</button>
    <div id="guests" style="display:none"></div>
    <button onclick="rates()">查看房价</button>
    <script>
      const months = __MONTHS__, template = __PAYLOAD__;
      window.events=[]; window.selected=[]; let year=2025, month=12;
      const counts={rooms:2, Adults:2, Children:1};
      function render() {
        document.querySelector('th.month').textContent=months[month-1]+'月 '+year;
        days.innerHTML='<tr><td class="available off">18</td></tr>' +
          Array.from({length:new Date(year,month,0).getDate()}, (_,i)=>
            '<tr><td class="available" onclick="pick('+(i+1)+')">'+(i+1)+'</td></tr>').join('');
      }
      function move(delta) {
        const d=new Date(year,month-1+delta,1); year=d.getFullYear(); month=d.getMonth()+1;
        events.push('month:'+year+'-'+month); render();
      }
      function pick(day) {
        selected.push(year+'-'+String(month).padStart(2,'0')+'-'+String(day).padStart(2,'0'));
        events.push('date:'+selected.at(-1));
      }
      function renderGuests() {
        guests.innerHTML=Object.keys(counts).map(kind=>'<div><button aria-label="Decrease number of '+kind+
          '" onclick="adjust(\''+kind+'\',-1)">−</button><span>'+counts[kind]+
          '</span><button aria-label="Increase number of '+kind+'" onclick="adjust(\''+kind+'\',1)">+</button></div>').join('')+
          '<button onclick="guests.style.display=\'none\';events.push(\'apply-guests\')">确认</button>';
      }
      function adjust(kind,delta) {counts[kind]+=delta;events.push(kind+':'+counts[kind]);renderGuests();}
      async function rates() {
        events.push('view-rates');
        const p=structuredClone(template), o=p.variables.search.options;
        [o.startDate,o.endDate]=selected; o.quantity=counts.rooms; o.numberInParty=counts.Adults;
        o.childAges=counts.Children?Array(counts.Children).fill(5):[];
        if (__POPUP__) {
          const qs=new URLSearchParams({payload:JSON.stringify(p)});
          window.open('/offline-fixture-results?'+qs); return;
        }
        if (__SOLD_OUT__) {
          const qs=new URLSearchParams({propertyCode:'NYCTE',fromDate:o.startDate,toDate:o.endDate,
            numberOfRooms:counts.rooms,numberOfAdults:counts.Adults,childrenCount:counts.Children,
            unsuccessfulSell:'true',wrongCard:__WRONG_CARD__});
          location.href='/search/findHotels.mi?'+qs; return;
        }
        const wrong=structuredClone(p);wrong.variables.search.options.startDate='2026-10-01';
        await fetch('__PRICE_PATH__',{method:'POST',body:JSON.stringify(wrong)});
        await fetch('__PRICE_PATH__',{method:'POST',body:JSON.stringify(p)});
      }
      render(); renderGuests();
    </script>""".replace("__PAYLOAD__", payload)
        .replace("__MONTHS__", json.dumps(CHINESE_MONTHS))
        .replace("__PRICE_PATH__", PRICE_PATH)
        .replace("__SOLD_OUT__", json.dumps(sold_out))
        .replace("__WRONG_CARD__", json.dumps(wrong_card))
        .replace("__POPUP__", json.dumps(popup))
    )


async def install_routes(
    context, seen, *, homepage_status=200, challenge=False, sold_out=False, wrong_card=False, popup=False
):
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    async def route(route):
        request = route.request
        parsed = urlsplit(request.url)
        seen.append((request.method, parsed.path, request.post_data))
        if parsed.path == PRICE_PATH:
            body = request.post_data_json
            # Only the real page-generated, fully selected query receives a valid response.
            response = fixture["response"] if matches_request(body, task()) else {"ignored": True}
            await route.fulfill(content_type="application/json", body=json.dumps(response))
            return
        if parsed.path == "/":
            html = "<h1>Please complete the captcha</h1>" if challenge else "<h1>Offline homepage</h1>"
            await route.fulfill(
                status=homepage_status,
                headers={"retry-after": "123"},
                content_type="text/html; charset=utf-8",
                body=html,
            )
            return
        if parsed.path == HOTEL_PATH:
            html = hotel_html(sold_out=sold_out, wrong_card=wrong_card, popup=popup)
        elif parsed.path == "/offline-fixture-results":
            html = f"""<h1>Offline fixture results in a popup</h1><script>
              const payload=new URLSearchParams(location.search).get('payload');
              fetch('{PRICE_PATH}',{{method:'POST',body:payload}});
            </script>"""
        elif parsed.path == "/search/findHotels.mi":
            code = "XIYBP" if wrong_card else "NYCTE"
            html = (
                '<div role="alert">您选择的酒店没有空房</div>'
                '<div class="property-card-container with-alerts"><div class="property-card-container">'
                f'<a href="/details?propertyCode={code}&foo=1">Hotel</a><p>售罄</p></div>'
                '<div class="property-card-container" style="display:none">'
                f'<a href="/details?propertyCode={code}&foo=1">Hidden mobile duplicate</a>售罄</div></div>'
            )
        elif parsed.path == "/favicon.ico":
            await route.fulfill(status=204)
            return
        else:
            raise AssertionError("Unexpected outbound request in offline fixture: " + parsed.path)
        await route.fulfill(content_type="text/html; charset=utf-8", body=html)

    await context.route("**/*", route)


async def test_real_ui_calendar_guests_matching_response_and_disconnect_preserves_tabs(isolated_chrome):
    chrome, seen = isolated_chrome, []
    await install_routes(chrome.context, seen)
    driver = MarriottUIDriver(port=chrome.port, timeout_seconds=30)
    try:
        result = await driver.query(task())
        assert result["status"] == "ok" and result["offers"] == 67
        assert result["source_site"] == HOST and result["hotel_id"] == "NYCTE"
        assert result["response_hash"] and result["adults"] == 1
        events = await driver._page.evaluate("events")
        assert "month:2026-1" in events and "month:2026-10" in events
        assert events[-8:] == [
            "date:2026-10-18",
            "date:2026-10-19",
            "apply-dates",
            "rooms:1",
            "Adults:1",
            "Children:0",
            "apply-guests",
            "view-rates",
        ]
        assert [p for method, p, _ in seen if method == "GET" and p != "/favicon.ico"] == ["/", HOTEL_PATH]
        requests = [json.loads(body) for method, p, body in seen if p == PRICE_PATH]
        assert len(requests) == 2 and not matches_request(requests[0], task())
        assert matches_request(requests[1], task())
        pages_before_close = len(chrome.context.pages)
        assert pages_before_close == 2
    finally:
        await driver.close()
    assert chrome.process.poll() is None and pc.debugger_info(chrome.port)["webSocketDebuggerUrl"]
    assert len(chrome.context.pages) == pages_before_close
    assert not chrome.sentinel.is_closed() and chrome.sentinel.url == "about:blank"
    assert await chrome.sentinel.locator("h1").inner_text() == "Existing user-like tab: leave unchanged"


async def test_popup_queries_only_close_older_driver_tabs_and_keep_latest_result(isolated_chrome):
    chrome, seen = isolated_chrome, []
    await install_routes(chrome.context, seen, popup=True)
    driver = MarriottUIDriver(port=chrome.port, timeout_seconds=30)
    try:
        first = await driver.query(task())
        first_page = driver._page
        # The test and driver use independent CDP transports. Allow the test's
        # connection to receive the already acknowledged tab-close event.
        for _ in range(50):
            if len(chrome.context.pages) == 2:
                break
            await asyncio.sleep(0.02)
        assert first["offers"] == 67 and len(chrome.context.pages) == 2
        assert first_page.url.startswith(f"https://{HOST}/offline-fixture-results?")
        second = await driver.query(task())
        for _ in range(50):
            if len(chrome.context.pages) == 2:
                break
            await asyncio.sleep(0.02)
        assert second["offers"] == 67 and len(chrome.context.pages) == 2
        assert first_page.is_closed() and not driver._page.is_closed()
        assert not chrome.sentinel.is_closed()
        assert len([p for _, p, _ in seen if p == "/"]) == 1
        assert len([p for _, p, _ in seen if p == PRICE_PATH]) == 2
    finally:
        await driver.close()
    assert len(chrome.context.pages) == 2 and chrome.process.poll() is None


@pytest.mark.parametrize("status", [403, 429])
async def test_http_denial_stops_before_hotel_or_price_query(isolated_chrome, status):
    chrome, seen = isolated_chrome, []
    await install_routes(chrome.context, seen, homepage_status=status)
    driver = MarriottUIDriver(port=chrome.port, timeout_seconds=5)
    try:
        with pytest.raises(BrandBlocked if status == 403 else RateLimited) as caught:
            await driver.query(task())
        if status == 429:
            assert caught.value.retry_after == 123
        assert [p for _, p, _ in seen if p != "/favicon.ico"] == ["/"]
        assert not chrome.sentinel.is_closed()
    finally:
        await driver.close()


async def test_visible_homepage_challenge_stops_before_hotel_query(isolated_chrome):
    chrome, seen = isolated_chrome, []
    await install_routes(chrome.context, seen, challenge=True)
    driver = MarriottUIDriver(port=chrome.port, timeout_seconds=5)
    try:
        with pytest.raises(BrandBlocked):
            await driver.query(task())
        assert [p for _, p, _ in seen if p != "/favicon.ico"] == ["/"]
    finally:
        await driver.close()


@pytest.mark.parametrize("wrong_card", [False, True])
async def test_sold_out_requires_matching_redirect_dates_and_hotel_card(isolated_chrome, wrong_card):
    chrome, seen = isolated_chrome, []
    await install_routes(chrome.context, seen, sold_out=True, wrong_card=wrong_card)
    driver = MarriottUIDriver(port=chrome.port, timeout_seconds=8)
    try:
        if wrong_card:
            with pytest.raises(TransientFailure):
                await driver.query(task())
        else:
            result = await driver.query(task())
            assert result["status"] == "no_availability" and result["offers"] == 0
            assert "response_hash" not in result  # Sold out is not a zero-price observation.
        assert not any(p == PRICE_PATH for _, p, _ in seen)
    finally:
        await driver.close()


@pytest.mark.parametrize(
    "changed", [{"propertyCode": "XIYBP"}, {"fromDate": "2026-10-19"}, {"numberOfAdults": "2"}]
)
async def test_sold_out_url_mismatch_short_circuits_dom(changed):
    options = {
        "propertyCode": "NYCTE",
        "fromDate": "2026-10-18",
        "toDate": "2026-10-19",
        "numberOfRooms": "1",
        "numberOfAdults": "1",
        "childrenCount": "0",
        "unsuccessfulSell": "true",
        **changed,
    }
    driver = MarriottUIDriver()
    # No DOM methods: a mismatch must return before trying to read any matching-looking alert.
    driver._page = SimpleNamespace(url=f"https://{HOST}/search/findHotels.mi?{urlencode(options)}")
    assert not await driver._sold_out(task())
