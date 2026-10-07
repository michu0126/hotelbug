import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from probe_sixsenses_booking import install_script_diagnostics, public_failure_type, read_booking_controls


def fake_page():
    page = MagicMock()
    page.url = "https://www.sixsenses.com/en/observed-property/"
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="Six Senses Example")
    controls = page.locator.return_value
    controls.first.wait_for = AsyncMock()
    controls.evaluate_all = AsyncMock(return_value=[{"tag": "INPUT", "name": "arrivalDate"}])
    link = MagicMock()
    link.is_visible = AsyncMock(return_value=True)
    link.click = AsyncMock()
    controls.filter.return_value.all = AsyncMock(return_value=[link])
    page.expect_popup.return_value.__aexit__.side_effect = BrowserTimeout("No popup")
    return page, link


async def test_title_is_read_before_synchronous_check_and_inline_cta_is_clicked_once():
    page, link = fake_page()
    result = await read_booking_controls(page, page.url)
    assert result["outcome"] == "BOOKING_CONTROLS_READ"
    link.click.assert_awaited_once()
    assert page.title.await_count == 2
    assert result["controls"] == [{"tag": "INPUT", "name": "arrivalDate"}]


async def test_failed_cta_click_does_not_claim_booking_controls_were_opened():
    page, link = fake_page()
    link.click.side_effect = BrowserTimeout("Click intercepted")
    with pytest.raises(BrowserTimeout):
        await read_booking_controls(page, page.url)
    link.click.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_denied_response_never_clicks_booking_or_reads_controls(status):
    page, link = fake_page()
    page.goto.return_value.status = status
    assert (await read_booking_controls(page, page.url))["outcome"] == "ACCESS_REJECTED"
    link.click.assert_not_called()
    page.locator.assert_not_called()


async def test_ambiguous_public_ctas_are_reported_not_arbitrarily_clicked():
    page, link = fake_page()
    page.locator.return_value.filter.return_value.all.return_value = [link, link]
    result = await read_booking_controls(page, page.url)
    assert result["outcome"] == "BOOK_NOW_NOT_UNIQUE" and result["visible_ctas"] == 2
    link.click.assert_not_called()


@pytest.mark.parametrize("form_visible", [True, False])
async def test_loaded_phase_uses_observed_consent_then_waits_for_actual_form(form_visible):
    page, link = fake_page()
    controls = page.locator.return_value
    consent = MagicMock()
    consent.is_visible = AsyncMock(return_value=True)
    consent.click = AsyncMock()
    page.locator.side_effect = lambda selector: consent if selector == "#truste-consent-button" else controls
    page.wait_for_load_state = AsyncMock()
    page.wait_for_function = AsyncMock(side_effect=None if form_visible else BrowserTimeout("No date fields"))
    result = await read_booking_controls(page, page.url, settle=True)
    assert result["outcome"] == ("BOOKING_CONTROLS_READ" if form_visible else "BOOKING_FORM_NOT_VISIBLE")
    consent.click.assert_awaited_once()
    link.click.assert_awaited_once()
    page.wait_for_load_state.assert_awaited_once_with("domcontentloaded", timeout=30000)


def test_script_diagnostics_ignore_api_calls_and_strip_queries_and_credentials():
    page = MagicMock()
    events = []
    install_script_diagnostics(page, events)
    callbacks = {call.args[0]: call.args[1] for call in page.on.call_args_list}
    request = MagicMock(resource_type="fetch", url="https://private.example/api?token=SECRET")
    callbacks["requestfailed"](request)
    callbacks["response"](MagicMock(request=request, status=403, url=request.url))
    assert events == []
    request.resource_type = "script"
    request.url = "https://user:SECRET@cdn.example/site.js?token=SECRET#SECRET"
    request.failure = "net::ERR_CONNECTION_RESET"
    callbacks["requestfailed"](request)
    callbacks["response"](MagicMock(request=request, status=403, url=request.url))
    callbacks["pageerror"](RuntimeError("jQuery is not defined https://cdn.example/a?token=SECRET\nSECRET"))
    assert "SECRET" not in json.dumps(events)
    assert events[0] == {
        "event": "SCRIPT_FAILED",
        "hostname": "cdn.example",
        "path": "/site.js",
        "failure_type": "net::ERR_CONNECTION_RESET",
    }
    assert events[1]["status"] == 403 and events[2]["event"] == "PAGE_SCRIPT_ERROR"


def test_empty_public_script_error_does_not_break_diagnostic_callback():
    page = MagicMock()
    events = []
    install_script_diagnostics(page, events)
    callbacks = {call.args[0]: call.args[1] for call in page.on.call_args_list}
    callbacks["pageerror"](RuntimeError())
    assert events == [{"event": "PAGE_SCRIPT_ERROR", "message": "RuntimeError"}]


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("net::ERR_TIMED_OUT", "net::ERR_TIMED_OUT"),
        ("net::ERR_ABORTED", "net::ERR_ABORTED"),
        ("net::ERR_BLOCKED_BY_CLIENT", "net::ERR_BLOCKED_BY_CLIENT"),
        (None, "UNKNOWN"),
        ("net::ERR_FAILED https://user:SECRET@host/?token=SECRET", "UNKNOWN"),
        ("connection failed SECRET", "UNKNOWN"),
    ],
)
def test_network_failure_type_only_preserves_safe_chromium_identifiers(failure, expected):
    assert public_failure_type(failure) == expected
