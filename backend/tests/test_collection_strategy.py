import json
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.collection_strategy import collection_strategy
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_hybrid import MarriottHybridProvider
from app.providers.registry import create_provider
from tests.test_marriott import FIXTURE, SEARCH


def api_provider(status=200, body=None, headers=None):
    response = Mock(status=status, headers=headers or {})
    response.body = AsyncMock(return_value=json.dumps(FIXTURE["rates"] if body is None else body).encode())
    response.dispose = AsyncMock()
    page = Mock()
    page.context.request.post = AsyncMock(return_value=response)
    provider = MarriottHybridProvider()
    provider._get_page = AsyncMock(return_value=page)
    provider._open_hotel = AsyncMock(side_effect=AssertionError("API must not navigate DOM booking form"))
    return provider, page, response


async def test_english_quotes_use_observed_api_shared_cookie_context():
    provider, page, response = api_provider()
    rates = await provider.get_calendar_rates(SEARCH)
    call = page.context.request.post.call_args
    assert call.args == ("https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty",)
    options = call.kwargs["data"]["variables"]["search"]["options"]
    assert options["numberInParty"] == SEARCH.adults
    assert options["startDate"] == SEARCH.check_in.isoformat()
    assert options["endDate"] == SEARCH.check_out.isoformat()
    assert call.kwargs["max_redirects"] == 0
    assert "cookie" not in {key.lower() for key in call.kwargs["headers"]}
    assert rates[0].member_rate is True and str(rates[0].total_price) == "1377.06"
    provider._open_hotel.assert_not_called()
    response.dispose.assert_awaited_once()


async def test_chinese_source_keeps_browser_without_guessing_a_cn_api(monkeypatch):
    browser = AsyncMock(return_value=["CN browser result"])
    monkeypatch.setattr(MarriottBrowserProvider, "search_rates", browser)
    provider, page, _ = api_provider()
    provider.configure_hotel_url("https://www.marriott.com.cn/hotels/travel/nycmq-new-york-marriott-marquis")
    assert await provider.search_rates(SEARCH) == ["CN browser result"]
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
async def test_api_errors_do_not_silently_rotate_host_cookie_or_transport(status, code):
    provider, page, response = api_provider(status, headers={"retry-after": "123"})
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == code
    if status == 429:
        assert error.value.retry_after == 123
    assert page.context.request.post.await_count == 1
    provider._open_hotel.assert_not_called()
    response.dispose.assert_awaited_once()


@pytest.mark.parametrize("body", [[], "invalid", {"errors": [{"message": "bad"}]}])
async def test_invalid_or_partial_api_results_never_become_quotes(body):
    provider, _, response = api_provider(body=body)
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)
    response.dispose.assert_awaited_once()


async def test_api_timeout_is_sanitized():
    provider, page, _ = api_provider()
    page.context.request.post.side_effect = BrowserTimeout("Cookie=private-secret")
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.TIMEOUT
    assert "private-secret" not in str(error.value)


async def test_html_challenge_with_200_is_a_denial_not_a_zero_quote():
    provider, _, response = api_provider(headers={"content-type": "text/html"})
    response.body.return_value = b"<h1>Access Denied</h1>"
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    response.dispose.assert_awaited_once()


async def test_hotel_mismatch_rejected_before_api_request():
    provider, page, _ = api_provider()
    provider._official_code = "NKGWI"
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)
    page.context.request.post.assert_not_called()


def test_registry_and_public_strategy_do_not_claim_all_groups_have_api():
    assert isinstance(create_provider("marriott"), MarriottHybridProvider)
    for name in ("ihg", "hilton", "hyatt", "accor", "gha"):
        assert collection_strategy(name)["mode"] == "cookie_browser"
    assert collection_strategy("marriott")["live_api_verified"] is False
