"""Offline ordinary navigation/response boundaries for the public CN directory."""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.marriott_browser import MarriottBrowserProvider
from app.providers.marriott_cn_country_response import PATH
from tests.test_marriott_cn_country_response import country_source, observed
from tests.test_marriott_cn_directory_transport import ExpectedResponse


def public_response(*, country="CN", index=0, total=855, status=200, url=None, request=None, body=None):
    default_request, default_body, _, _ = observed(country=country, index=index, total=total)
    return SimpleNamespace(
        url=url or "https://www.marriott.com.cn" + PATH,
        status=status,
        request=SimpleNamespace(post_data_json=default_request if request is None else request),
        json=AsyncMock(return_value=default_body if body is None else body),
    )


class CountryPage:
    """A page emits responses only on normal goto or a published numeric click."""

    def __init__(
        self,
        responses,
        *,
        click_responses=None,
        country="CN",
        total=855,
        navigation_status=200,
        body="酒店目录",
        final_url=None,
        clicked_hash=None,
    ):
        self.responses = responses
        self.click_responses = click_responses or {}
        self.country = country
        self.total = total
        self.navigation_status = navigation_status
        self.body = body
        self.final_url = final_url
        self.clicked_hash = clicked_hash
        self.url = "about:blank"
        self.index = 0
        self.expected = None
        self.expected_count = 0
        self.goto_calls = []
        self.clicked = []
        self.locator_calls = []
        self.waited_starts = []

    def expect_response(self, predicate, **kwargs):
        assert kwargs == {"timeout": 45000}
        self.expected_count += 1
        return ExpectedResponse(self, predicate)

    def emit(self, responses):
        for response in responses:
            if self.expected.predicate(response):
                self.expected.selected = response
                break

    async def goto(self, source, **kwargs):
        assert source == country_source(self.country)
        assert kwargs == {"wait_until": "commit", "timeout": 45000}
        self.goto_calls.append(source)
        self.url = self.final_url or source + "#/0/"
        self.emit(self.responses)
        return SimpleNamespace(status=self.navigation_status)

    async def wait_for_function(self, script, *, arg, timeout):
        assert timeout == 30000
        assert arg is None or arg == self.index * 40 + 1
        self.waited_starts.append(arg)

    def locator(self, selector):
        self.locator_calls.append(selector)
        if selector == "body":
            return SimpleNamespace(inner_text=AsyncMock(return_value=self.body))
        if selector == ".search-summary-container":
            return SimpleNamespace(
                inner_text=AsyncMock(
                    return_value=f"{self.index * 40 + 1} - {min((self.index + 1) * 40, self.total)} / {self.total} 结果"
                )
            )
        if selector == "a.shop-pagination-item[aria-label]":
            return SimpleNamespace(evaluate_all=AsyncMock(return_value=["1", "2", "3", "4", "22"]))
        control = re.fullmatch(r'a\.shop-pagination-item\[aria-label="([1-9][0-9]*)"\]', selector)
        if control:

            async def click():
                self.index = int(control[1]) - 1
                self.clicked.append(int(control[1]))
                self.url = country_source(self.country, self.index)
                if self.clicked_hash is not None:
                    self.url = self.url.split("#", 1)[0] + self.clicked_hash
                self.emit(self.click_responses.get(self.index, []))

            return SimpleNamespace(click=click)
        # The response contains all identities. A lazy-card locator or scroll
        # here means production accidentally fell back to partial DOM scraping.
        raise AssertionError(f"Unexpected DOM extraction: {selector}")


async def discover(page, *, index=0):
    provider = MarriottBrowserProvider()
    provider._page = page
    return await provider.discover_catalog({"official_url": country_source(page.country, index)})


async def test_initial_country_navigation_uses_page_issued_response_and_no_lazy_card_scraping():
    response = public_response(country="TW", total=34)
    page = CountryPage([response], country="TW", total=34)
    result = await discover(page)
    assert len(result.hotels) == result.reported_total == 34
    assert result.complete is True and result.page_complete is True
    assert page.goto_calls == [country_source("TW")]
    assert page.expected_count == 1 and not page.clicked
    assert page.locator_calls == ["body", "body", ".search-summary-container", "body"]
    assert page.waited_starts == [1]
    response.json.assert_awaited_once()


async def test_second_page_job_opens_base_and_clicks_published_page_two_with_response_offset_40():
    first = public_response()
    second = public_response(index=1)
    page = CountryPage([first], click_responses={1: [second]})
    result = await discover(page, index=1)
    assert page.goto_calls == [country_source("CN")]
    assert page.clicked == [2] and page.expected_count == 2
    assert page.waited_starts == [None, 41, 41]
    assert len(result.hotels) == 40 and result.hotels[0].provider_hotel_id == "A0041"
    assert str(result.source_url) == country_source("CN", 1)
    assert [str(url) for url in result.directory_urls] == [country_source("CN", 2)]
    assert result.page_complete is True and result.complete is False
    first.json.assert_awaited_once()
    second.json.assert_awaited_once()


async def test_unrelated_origin_path_offset_and_malformed_request_events_are_ignored():
    ignored = [
        public_response(url="https://unrelated.example" + PATH),
        public_response(url="https://www.marriott.com.cn/mi/query/anotherOperation"),
        public_response(index=1),
        public_response(request={"variables": {}}),
    ]
    official = public_response()
    result = await discover(CountryPage([*ignored, official]))
    assert result.page_complete is True and len(result.hotels) == 40
    for response in ignored:
        response.json.assert_not_awaited()
    official.json.assert_awaited_once()


async def test_stale_first_page_response_after_click_is_ignored_before_offset_40_response():
    first, stale, second = public_response(), public_response(), public_response(index=1)
    page = CountryPage([first], click_responses={1: [stale, second]})
    result = await discover(page, index=1)
    assert result.hotels[0].provider_hotel_id == "A0041"
    stale.json.assert_not_awaited()
    second.json.assert_awaited_once()


@pytest.mark.parametrize("status,code", [(403, ErrorCode.BLOCKED_BY_ANTIBOT), (429, ErrorCode.RATE_LIMITED)])
@pytest.mark.parametrize("stage", ["navigation", "initial_response", "clicked_response"])
async def test_rejection_preserves_access_error_without_parsing_denied_response(status, code, stage):
    first = public_response(status=status if stage == "initial_response" else 200)
    second = public_response(index=1, status=status if stage == "clicked_response" else 200)
    page = CountryPage(
        [first],
        navigation_status=status if stage == "navigation" else 200,
        click_responses={1: [second]},
    )
    with pytest.raises(ProviderError) as error:
        await discover(page, index=1 if stage == "clicked_response" else 0)
    assert error.value.code == code
    if stage != "clicked_response":
        first.json.assert_not_awaited()
    second.json.assert_not_awaited()


async def test_challenge_body_is_not_a_successful_directory_even_when_status_is_200():
    response = public_response()
    with pytest.raises(ProviderError) as error:
        await discover(CountryPage([response], body="Access denied"))
    assert error.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    response.json.assert_not_awaited()


@pytest.mark.parametrize("stage", ["initial", "clicked"])
async def test_missing_matching_offset_response_remains_timeout(stage):
    first = public_response()
    page = CountryPage(
        [first] if stage == "clicked" else [public_response(index=1)],
        click_responses={1: [public_response()]},
    )
    with pytest.raises(ProviderError) as error:
        await discover(page, index=1 if stage == "clicked" else 0)
    assert error.value.code == ErrorCode.TIMEOUT


@pytest.mark.parametrize("failure", [TimeoutError("Response body stalled"), ValueError("Not JSON")])
async def test_country_json_failure_does_not_return_complete_catalog(failure):
    response = public_response()
    response.json.side_effect = failure
    with pytest.raises(type(failure)):
        await discover(CountryPage([response]))


@pytest.mark.parametrize("mutation", ["scope", "operation", "offset_in_body", "partial_edges"])
async def test_matching_endpoint_does_not_accept_wrong_scope_or_inconsistent_response(mutation):
    request, body, data, _ = observed(country="CN", total=855)
    if mutation == "scope":
        countries = next(
            term for term in request["variables"]["search"]["facets"]["terms"] if term["type"] == "COUNTRIES"
        )
        countries["dimensions"] = ["SG"]
    elif mutation == "operation":
        request["operationName"] = "anotherOperation"
    elif mutation == "offset_in_body":
        data["pageInfo"]["currentOffset"] = 40
    else:
        data["edges"].pop()
    response = public_response(request=request, body=body)
    with pytest.raises(ProviderError) as error:
        await discover(CountryPage([response]))
    assert error.value.code == ErrorCode.INVALID_RESPONSE


@pytest.mark.parametrize(
    "final_url", ["https://unrelated.example/", country_source("SG"), country_source("CN") + "#unsupported"]
)
async def test_final_navigation_must_keep_requested_origin_country_and_supported_route(final_url):
    with pytest.raises(ProviderError) as error:
        await discover(CountryPage([public_response()], final_url=final_url))
    assert error.value.code == ErrorCode.INVALID_RESPONSE


async def test_observed_third_page_uses_response_and_rendered_range_not_navigation_hash():
    page = CountryPage(
        [public_response()], click_responses={2: [public_response(index=2)]}, clicked_hash="#/1/"
    )
    result = await discover(page, index=2)
    assert page.clicked == [3]
    assert page.url.endswith("#/1/")
    assert page.waited_starts == [None, 81, 81]
    assert str(result.source_url) == country_source("CN", 2)
    assert result.hotels[0].provider_hotel_id == "A0081"
    assert [str(url) for url in result.directory_urls] == [country_source("CN", 3)]
    assert result.page_complete


async def test_response_total_must_also_match_rendered_counter():
    with pytest.raises(ProviderError, match="response and page range disagree"):
        await discover(CountryPage([public_response(total=855)], total=854))
