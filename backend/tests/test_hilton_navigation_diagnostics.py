import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from hilton_navigation_diagnostics import (
    attach_navigation_diagnostics,
    install_navigation_diagnostics,
    public_path,
)

from app.core.errors import ErrorCode, ProviderError
from app.providers.registry import FACTORIES


def page_probe():
    handlers, events, seen = {}, [], set()
    page = MagicMock()
    page.on.side_effect = lambda event, handler: handlers.update({event: handler})
    attach_navigation_diagnostics(page, events, seen)
    request = MagicMock(resource_type="document", url="https://www.hilton.com/en/?private=SECRET")
    request.frame = page.main_frame
    request.is_navigation_request.return_value = True
    return page, request, handlers, events, seen


def test_only_main_document_metadata_is_projected_and_query_values_are_not_read():
    page, request, handlers, events, _ = page_probe()
    handlers["request"](request)
    handlers["response"](MagicMock(request=request, status=200))
    page.main_frame.url = request.url
    handlers["framenavigated"](page.main_frame)
    handlers["requestfinished"](request)
    assert [e["event"] for e in events] == [
        "DOCUMENT_STARTED",
        "DOCUMENT_RESPONSE",
        "DOCUMENT_COMMITTED",
        "DOCUMENT_FINISHED",
    ]
    assert all(e["public_path"] == "/en/" and e["page"] == 1 for e in events)
    assert events[1]["status"] == 200 and "SECRET" not in json.dumps(events)
    request.headers.assert_not_called()
    page.goto.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"resource_type": "xhr"},
        {"resource_type": "script"},
        {"navigation": False},
        {"subframe": True},
        {"url": "https://example.com/private?secret=SECRET"},
        {"url": "https://www.hilton.com/en/signin/?secret=SECRET"},
    ],
)
def test_background_private_and_subframe_requests_are_ignored(changes):
    _, request, handlers, events, _ = page_probe()
    for key, value in changes.items():
        if key == "navigation":
            request.is_navigation_request.return_value = value
        elif key == "subframe":
            request.frame = MagicMock()
        else:
            setattr(request, key, value)
    handlers["request"](request)
    handlers["response"](MagicMock(request=request, status=403))
    handlers["requestfailed"](request)
    assert events == []


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("net::ERR_TUNNEL_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED"),
        ("net::ERR_ABORTED", "ERR_ABORTED"),
        ("net::ERR_CONNECTION_TIMED_OUT", "ERR_CONNECTION_TIMED_OUT"),
        ("net::ERR_ABORTED private SECRET", "UNKNOWN"),
        (None, "UNKNOWN"),
    ],
)
def test_transport_errors_are_bounded_codes_not_raw_browser_messages(failure, expected):
    _, request, handlers, events, _ = page_probe()
    request.failure = failure
    handlers["requestfailed"](request)
    assert events[0]["transport_error"] == expected and "SECRET" not in json.dumps(events)


def test_one_attachment_per_page_and_bounded_popup_events():
    page, request, handlers, events, seen = page_probe()
    attach_navigation_diagnostics(page, events, seen)
    assert page.on.call_count == 6
    popup = MagicMock()
    handlers["popup"](popup)
    handlers["popup"](popup)
    assert popup.on.call_count == 6
    for _ in range(100):
        handlers["request"](request)
    assert len(events) == 64


@pytest.mark.parametrize(
    "url",
    [
        "https://user:SECRET@www.hilton.com/en/",
        "http://www.hilton.com/en/",
        "https://www.hilton.com:8443/en/",
        "about:blank",
    ],
)
def test_nonpublic_authorities_are_not_recorded(url):
    assert public_path(url) is None


@pytest.mark.parametrize("attachment_fails", [False, True])
async def test_error_identity_is_preserved_without_an_extra_navigation(monkeypatch, capsys, attachment_fails):
    instance = MagicMock()
    page, _, _, _, _ = page_probe()
    if attachment_fails:
        page.on.side_effect = RuntimeError("SECRET")
    instance._get_page = AsyncMock(return_value=page)
    error = ProviderError(ErrorCode.TIMEOUT, "Recorded original timeout")

    async def rates(request):
        await instance._get_page()
        raise error

    instance.search_rates = rates
    monkeypatch.setitem(FACTORIES, "hilton", lambda: instance)
    install_navigation_diagnostics()
    with pytest.raises(ProviderError) as caught:
        await FACTORIES["hilton"]().search_rates(None)
    assert caught.value is error
    expected = [{"event": "DOCUMENT_DIAGNOSTIC_UNAVAILABLE"}] if attachment_fails else []
    assert json.loads(capsys.readouterr().out) == {"public_hilton_navigation": expected}
    page.goto.assert_not_called()


def test_disconnected_popup_diagnostic_does_not_replace_the_query_failure():
    _, _, handlers, events, _ = page_probe()
    popup = MagicMock()
    popup.on.side_effect = RuntimeError("SECRET")
    handlers["popup"](popup)
    assert events == [{"event": "DOCUMENT_DIAGNOSTIC_UNAVAILABLE"}]
