from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors import ProviderError
from app.providers.marriott_cn_catalog import navigate_country_page

SOURCE = (
    "https://www.marriott.com.cn/search/findHotels.mi?destinationAddress.destination=China&"
    "destinationAddress.country=CN&searchType=InCity&deviceType=desktop-web"
)


def mocked_page(monkeypatch, ranges, labels):
    page = MagicMock()
    page.url = SOURCE
    page.locator.return_value.evaluate_all = AsyncMock(side_effect=labels)
    page.locator.return_value.click = AsyncMock()
    wait = AsyncMock(side_effect=ranges)
    monkeypatch.setattr("app.providers.marriott_cn_catalog.wait_country_range", wait)
    provider = MagicMock()
    provider._check_access = AsyncMock()
    return provider, page, wait


async def test_fresh_second_page_job_clicks_the_published_page_control(monkeypatch):
    provider, page, wait = mocked_page(
        monkeypatch, [(1, 40, 855), (41, 80, 855)], [["1", "2", "3", "4", "22"]]
    )
    assert await navigate_country_page(provider, page, SOURCE + "#/1/") == (41, 80, 855)
    page.locator.assert_any_call('a.shop-pagination-item[aria-label="2"]')
    page.locator.return_value.click.assert_awaited_once()
    assert wait.await_args.args == (page, 41)


async def test_last_page_is_reached_by_one_published_jump_not_21_next_clicks(monkeypatch):
    provider, page, _ = mocked_page(
        monkeypatch, [(1, 40, 855), (841, 855, 855)], [["1", "2", "3", "4", "22"]]
    )
    assert await navigate_country_page(provider, page, SOURCE + "#/21/") == (841, 855, 855)
    page.locator.assert_any_call('a.shop-pagination-item[aria-label="22"]')
    page.locator.return_value.click.assert_awaited_once()


async def test_unpublished_page_or_out_of_range_is_not_invented(monkeypatch):
    provider, page, _ = mocked_page(monkeypatch, [(1, 40, 855)], [["1"]])
    with pytest.raises(ProviderError, match="control missing"):
        await navigate_country_page(provider, page, SOURCE + "#/1/")
    provider, page, _ = mocked_page(monkeypatch, [(1, 40, 855)], [])
    with pytest.raises(ProviderError, match="outside results"):
        await navigate_country_page(provider, page, SOURCE + "#/22/")
