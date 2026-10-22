from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.collection_strategy import collection_strategy
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_hybrid import MarriottHybridProvider
from app.providers.registry import create_provider
from tests.test_marriott import SEARCH
from tests.test_marriott_renderer import renderer_provider


def api_provider(status=200, body=None, headers=None):
    headers = headers or {}
    provider, page, result, _ = renderer_provider(
        status, body, headers.get("content-type", "application/json"), headers.get("retry-after")
    )
    return provider, page, result


async def test_english_quotes_use_observed_api_in_official_page():
    provider, page, _ = api_provider()
    rates = await provider.get_calendar_rates(SEARCH)
    call = page.evaluate.call_args.args[1]
    assert call["url"] == "https://www.marriott.com/mi/query/PhoenixBookDTTSearchProductsByProperty"
    options = call["body"]["variables"]["search"]["options"]
    assert options["numberInParty"] == SEARCH.adults
    assert options["startDate"] == SEARCH.check_in.isoformat()
    assert options["endDate"] == SEARCH.check_out.isoformat()
    assert "redirect:'manual'" in page.evaluate.call_args.args[0]
    assert "cookie" not in {key.lower() for key in call["headers"]}
    assert rates[0].member_rate is True and str(rates[0].total_price) == "1377.06"
    provider._open_hotel.assert_not_called()
    page.context.request.post.assert_not_called()


async def test_chinese_source_keeps_browser_without_guessing_a_cn_api(monkeypatch):
    browser = AsyncMock(return_value=["CN browser result"])
    monkeypatch.setattr(MarriottBrowserProvider, "search_rates", browser)
    provider, page, _ = api_provider()
    provider.configure_hotel_url("https://www.marriott.com.cn/hotels/travel/nycmq-new-york-marriott-marquis")
    assert await provider.search_rates(SEARCH) == ["CN browser result"]
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
async def test_api_errors_do_not_silently_rotate_host_cookie_or_transport(status, code):
    provider, page, _ = api_provider(status, headers={"retry-after": "123"})
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == code
    if status == 429:
        assert error.value.retry_after == 123
    assert page.evaluate.await_count == 1
    provider._open_hotel.assert_not_called()
    page.context.request.post.assert_not_called()


@pytest.mark.parametrize("body", [[], "invalid", {"errors": [{"message": "bad"}]}])
async def test_invalid_or_partial_api_results_never_become_quotes(body):
    provider, _, _ = api_provider(body=body)
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)


async def test_api_timeout_is_sanitized():
    provider, page, _ = api_provider()
    page.evaluate.side_effect = BrowserTimeout("Cookie=private-secret")
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.TIMEOUT
    assert "private-secret" not in str(error.value)


async def test_html_challenge_with_200_is_a_denial_not_a_zero_quote():
    provider, _, response = api_provider(headers={"content-type": "text/html"})
    response["text"] = "<h1>Access Denied</h1>"
    with pytest.raises(ProviderError) as error:
        await provider.search_rates(SEARCH)
    assert error.value.code == ErrorCode.BLOCKED_BY_ANTIBOT


async def test_hotel_mismatch_rejected_before_api_request():
    provider, page, _ = api_provider()
    provider._official_code = "NKGWI"
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)
    page.evaluate.assert_not_called()
    page.context.request.post.assert_not_called()


def test_registry_and_public_strategy_do_not_claim_all_groups_have_api():
    assert isinstance(create_provider("marriott"), MarriottHybridProvider)
    for name in ("ihg", "hilton", "hyatt", "accor", "gha"):
        assert collection_strategy(name)["mode"] == "cookie_browser"
    assert collection_strategy("marriott")["live_api_verified"] is False
