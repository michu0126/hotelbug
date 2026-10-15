"""Offline browser-response boundaries for the public CN directory root."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_catalog import ROOT
from app.providers.marriott_cn_directory_response import PATH
from tests.test_marriott_cn_directory_response import observed


class ExpectedResponse:
    def __init__(self, page, predicate):
        self.page = page
        self.predicate = predicate
        self.selected = None

    async def __aenter__(self):
        self.page.expected = self
        return self

    async def __aexit__(self, error_type, error, traceback):
        if error_type is None and self.selected is None:
            raise PlaywrightTimeout("No matching public directory response")

    @property
    def value(self):
        async def result():
            return self.selected

        return result()


class DirectoryPage:
    def __init__(self, responses=(), *, navigation_status=200, final_url=ROOT):
        self.responses = responses
        self.navigation_status = navigation_status
        self.final_url = final_url
        self.url = "about:blank"
        self.expected = None

    def expect_response(self, predicate, **kwargs):
        return ExpectedResponse(self, predicate)

    async def goto(self, source, **kwargs):
        assert source == ROOT
        self.url = self.final_url
        for response in self.responses:
            if self.expected.predicate(response):
                self.expected.selected = response
                break
        return SimpleNamespace(status=self.navigation_status)

    def locator(self, selector):
        assert selector == "body"
        return SimpleNamespace(inner_text=AsyncMock(return_value="酒店目录"))


def public_response(*, status=200, origin="https://www.marriott.com.cn", request=None, body=None):
    default_request, default_body, _ = observed()
    return SimpleNamespace(
        url=origin + PATH,
        status=status,
        request=SimpleNamespace(post_data_json=default_request if request is None else request),
        json=AsyncMock(return_value=default_body if body is None else body),
    )


async def discover(page):
    provider = MarriottBrowserProvider()
    provider._page = page
    return await provider.discover_catalog({"official_url": ROOT})


async def test_root_uses_matching_official_response_without_emitting_hotel_or_price_rows():
    unrelated = public_response(origin="https://unrelated.example")
    official = public_response()
    result = await discover(DirectoryPage([unrelated, official]))
    assert str(result.source_url) == ROOT
    assert result.reported_total == 24
    assert result.hotels == []
    assert len(result.directory_urls) == 2
    assert result.page_complete is True and result.complete is False
    unrelated.json.assert_not_awaited()
    official.json.assert_awaited_once()


@pytest.mark.parametrize(
    "status,code",
    [(403, ErrorCode.BLOCKED_BY_ANTIBOT), (429, ErrorCode.RATE_LIMITED)],
)
@pytest.mark.parametrize("stage", ["navigation", "response"])
async def test_root_rejection_keeps_access_error_and_never_parses_counts(status, code, stage):
    response = public_response(status=status if stage == "response" else 200)
    page = DirectoryPage([response], navigation_status=status if stage == "navigation" else 200)
    with pytest.raises(ProviderError) as error:
        await discover(page)
    assert error.value.code == code
    response.json.assert_not_awaited()


async def test_root_missing_matching_response_is_timeout():
    with pytest.raises(ProviderError) as error:
        await discover(DirectoryPage([public_response(origin="https://unrelated.example")]))
    assert error.value.code == ErrorCode.TIMEOUT


async def test_root_json_timeout_does_not_return_successful_catalog():
    response = public_response()
    response.json.side_effect = TimeoutError("Response body did not finish")
    with pytest.raises(TimeoutError):
        await discover(DirectoryPage([response]))


async def test_root_invalid_json_does_not_return_successful_catalog():
    response = public_response()
    response.json.side_effect = ValueError("The response is HTML, not JSON")
    with pytest.raises(ValueError):
        await discover(DirectoryPage([response]))


async def test_root_navigation_to_another_site_rejects_even_valid_country_counts():
    with pytest.raises(ProviderError) as error:
        await discover(DirectoryPage([public_response()], final_url="https://unrelated.example/"))
    assert error.value.code == ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize("change", ["operation", "country_filter"])
async def test_root_same_endpoint_cannot_turn_an_unrelated_or_filtered_request_into_global_counts(change):
    request, _, _ = observed()
    if change == "operation":
        request["operationName"] = "otherOperation"
    else:
        request["variables"]["search"]["facets"]["terms"][0]["dimensions"] = ["SG"]
    with pytest.raises(ProviderError) as error:
        await discover(DirectoryPage([public_response(request=request)]))
    assert error.value.code == ErrorCode.INVALID_RESPONSE
