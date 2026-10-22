import json
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_hybrid import MarriottHybridProvider
from tests.test_marriott import FIXTURE, SEARCH


def renderer_provider(status=200, body=None, content_type="application/json", retry_after=None):
    result = {
        "status": status,
        "text": json.dumps(FIXTURE["rates"] if body is None else body),
        "content_type": content_type,
        "retry_after": retry_after,
    }
    page = Mock(url="about:blank")
    page.evaluate = AsyncMock(return_value=result)
    page.context.request.post = AsyncMock(
        side_effect=AssertionError("Do not use the separate HTTP transport")
    )
    page.locator.return_value.inner_text = AsyncMock(return_value="Marriott Bonvoy")
    navigation = Mock(status=200, headers={})

    async def navigate(url, **_):
        page.url = url
        return navigation

    page.goto = AsyncMock(side_effect=navigate)
    provider = MarriottHybridProvider()
    provider._get_page = AsyncMock(return_value=page)
    provider._open_hotel = AsyncMock(side_effect=AssertionError("Do not drive the booking form"))
    return provider, page, result, navigation


async def test_renderer_initializes_origin_and_uses_observed_query():
    provider, page, _, _ = renderer_provider()
    rates = await provider.search_rates(SEARCH)
    page.goto.assert_awaited_once_with(
        "https://www.marriott.com/default.mi", wait_until="domcontentloaded", timeout=45000
    )
    args = page.evaluate.call_args.args[1]
    assert args["url"] == "https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty"
    options = args["body"]["variables"]["search"]["options"]
    assert options["startDate"] == SEARCH.check_in.isoformat()
    assert options["endDate"] == SEARCH.check_out.isoformat()
    assert options["numberInParty"] == SEARCH.adults
    assert {k.lower() for k in args["headers"]}.isdisjoint({"cookie", "host", "origin", "referer"})
    assert str(rates[0].total_price) == "1377.06"
    assert rates[0].member_rate is True
    page.context.request.post.assert_not_called()
    provider._open_hotel.assert_not_called()


async def test_renderer_uses_configured_published_property_document():
    provider, page, _, _ = renderer_provider()
    url = "https://www.marriott.com/en-us/hotels/nycmq-new-york-marriott-marquis/overview/"
    provider.configure_hotel_url(url)
    await provider.search_rates(SEARCH)
    assert page.goto.call_args.args[0] == url


async def test_renderer_reuses_same_origin_page_without_reloading():
    provider, page, _, _ = renderer_provider()
    page.url = "https://www.marriott.com/reservation/rateListMenu.mi"
    await provider.search_rates(SEARCH)
    page.goto.assert_not_called()


@pytest.mark.parametrize(
    "url", ["https://www.marriott.com.cn/", "https://example.org/", "http://www.marriott.com/"]
)
async def test_renderer_does_not_send_query_from_other_origins(url):
    provider, page, _, _ = renderer_provider()
    page.url = url
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.INVALID_RESPONSE
    page.evaluate.assert_not_called()
    page.context.request.post.assert_not_called()


@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.TOKEN_EXPIRED),
        (302, ErrorCode.TOKEN_EXPIRED),
        (403, ErrorCode.BLOCKED_BY_ANTIBOT),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.HTTP_ERROR),
    ],
)
async def test_renderer_errors_are_not_retried_or_switched_to_other_transports(status, code):
    provider, page, _, _ = renderer_provider(status=status, retry_after="123")
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == code
    if status == 429:
        assert error.value.retry_after == 123
    assert page.evaluate.await_count == 1
    page.context.request.post.assert_not_called()


async def test_renderer_opaque_redirect_is_not_followed():
    provider, page, result, _ = renderer_provider()
    result.update(status=0, redirected=True)
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.TOKEN_EXPIRED
    assert "manual" in page.evaluate.call_args.args[0]


@pytest.mark.parametrize("status,code", [(403, ErrorCode.BLOCKED_BY_ANTIBOT), (429, ErrorCode.RATE_LIMITED)])
async def test_renderer_stops_before_query_when_document_is_denied(status, code):
    provider, page, _, navigation = renderer_provider()
    navigation.status = status
    navigation.headers = {"retry-after": "321"}
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == code
    page.evaluate.assert_not_called()


@pytest.mark.parametrize(
    "failure,code",
    [
        ("timeout", ErrorCode.TIMEOUT),
        ("network", ErrorCode.NETWORK_ERROR),
        ("origin_mismatch", ErrorCode.INVALID_RESPONSE),
        ("body_too_large", ErrorCode.PROVIDER_CHANGED),
    ],
)
async def test_renderer_browser_failures_are_sanitized(failure, code):
    provider, _, result, _ = renderer_provider()
    result.update(error=failure, text="Cookie=private-secret")
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == code
    assert "private-secret" not in str(error.value)


async def test_renderer_playwright_timeout_is_sanitized():
    provider, page, _, _ = renderer_provider()
    page.evaluate.side_effect = BrowserTimeout("Cookie=private-secret")
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.TIMEOUT
    assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("body", [[], "invalid", {"errors": [{"message": "bad"}]}])
async def test_renderer_incomplete_results_never_become_quotes(body):
    provider, _, _, _ = renderer_provider(body=body)
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)


async def test_renderer_html_challenge_is_not_a_zero_price():
    provider, _, result, _ = renderer_provider(content_type="text/html")
    result["text"] = "<h1>Access Denied</h1>"
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.BLOCKED_BY_ANTIBOT


async def test_renderer_hotel_mismatch_is_rejected_before_browser_start():
    provider, page, _, _ = renderer_provider()
    provider._official_code = "NKGWI"
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)
    provider._get_page.assert_not_called()
    page.evaluate.assert_not_called()


async def test_renderer_keeps_cn_transport_unchanged(monkeypatch):
    browser = AsyncMock(return_value=["CN browser result"])
    monkeypatch.setattr(MarriottBrowserProvider, "search_rates", browser)
    provider, page, _, _ = renderer_provider()
    provider.configure_hotel_url("https://www.marriott.com.cn/hotels/travel/nycmq-new-york-marriott-marquis")
    assert await provider.search_rates(SEARCH) == ["CN browser result"]
    page.evaluate.assert_not_called()
    page.context.request.post.assert_not_called()
