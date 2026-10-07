import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from probe_ihg_sixsenses import booking_link_projection, follow_public_hotel, install_public_link_probe
from sqlalchemy import select

from app.models.tables import AppSetting
from app.providers.registry import FACTORIES


@pytest.mark.parametrize(
    "text,expected",
    [
        ("BOOK NOW", True),
        ("RESERVATION ENQUIRY", True),
        ("Check availability", True),
        ("facebook", False),
        ("About our hotel", False),
    ],
)
def test_public_booking_links_do_not_include_social_links_or_unrequested_fields(text, expected):
    item = {"text": text, "hostname": "www.sixsenses.com", "path": "/example", "query": "SECRET"}
    result = booking_link_projection([item])
    assert bool(result) is expected
    assert "SECRET" not in json.dumps(result)


@pytest.mark.parametrize("count,visible", [(0, True), (2, True), (1, False)])
async def test_ambiguous_or_hidden_hotel_link_is_not_clicked(count, visible):
    page = MagicMock()
    link = page.locator.return_value.filter.return_value
    link.count = AsyncMock(return_value=count)
    link.is_visible = AsyncMock(return_value=visible)
    result = await follow_public_hotel(page, "Six Senses Example")
    assert result == {"outcome": "LINK_NOT_UNIQUE_OR_VISIBLE", "matching_links": count}
    link.click.assert_not_called()


async def test_link_must_still_target_the_observed_official_brand():
    page = MagicMock()
    link = page.locator.return_value.filter.return_value
    link.count = AsyncMock(return_value=1)
    link.is_visible = AsyncMock(return_value=True)
    link.get_attribute = AsyncMock(return_value="https://other.example/private")
    assert (await follow_public_hotel(page, "Six Senses Example"))["outcome"] == "LINK_TARGET_UNVERIFIED"
    link.click.assert_not_called()


@pytest.mark.parametrize("destination", ["official", "foreign", "denied"])
async def test_one_normal_popup_click_reads_only_official_public_page_and_closes_it(destination):
    page, popup = MagicMock(), MagicMock()
    link = page.locator.return_value.filter.return_value
    link.count = AsyncMock(return_value=1)
    link.is_visible = AsyncMock(return_value=True)
    link.get_attribute = AsyncMock(side_effect=["https://www.sixsenses.com/observed-link", "_blank"])
    link.click = AsyncMock()
    handle = asyncio.Future()
    handle.set_result(popup)
    page.expect_popup.return_value.__aenter__.return_value.value = handle
    popup.url = (
        "https://other.example/property?private=SECRET"
        if destination == "foreign"
        else "https://www.sixsenses.com/observed-property?private=SECRET"
    )
    popup.wait_for_load_state = AsyncMock()
    popup.close = AsyncMock()
    popup.title = AsyncMock(return_value="Access Denied" if destination == "denied" else "Example")
    popup.locator.return_value.first.wait_for = AsyncMock()
    popup.locator.return_value.all_inner_texts = AsyncMock(return_value=["Six Senses Example"])
    popup.locator.return_value.evaluate_all = AsyncMock(return_value=[])
    result = await follow_public_hotel(page, "Six Senses Example")
    assert "SECRET" not in json.dumps(result)
    expected = {
        "official": "PUBLIC_PAGE_READ",
        "foreign": "DESTINATION_UNVERIFIED",
        "denied": "ACCESS_REJECTED",
    }
    assert result["outcome"] == expected[destination]
    link.click.assert_awaited_once()
    page.goto.assert_not_called()
    popup.close.assert_awaited_once()
    if destination != "official":
        popup.locator.assert_not_called()


async def test_probe_preserves_real_catalog_and_cannot_repeat_across_instances_or_restart(
    sessions, monkeypatch, capsys
):
    import probe_ihg_sixsenses as probe

    result = object()
    follow = AsyncMock(return_value={"outcome": "PUBLIC_PAGE_READ"})
    monkeypatch.setattr(probe, "follow_public_hotel", follow)
    names = iter(["Six Senses Example", "Six Senses Another", "Six Senses Example"])

    def original_factory():
        instance = MagicMock()
        instance._page.locator.return_value.evaluate_all = AsyncMock(return_value=[next(names)])
        instance.discover_catalog = AsyncMock(return_value=result)
        return instance

    monkeypatch.setitem(FACTORIES, "ihg", original_factory)
    install_public_link_probe(sessions)
    assert await FACTORIES["ihg"]().discover_catalog({}) is result
    assert await FACTORIES["ihg"]().discover_catalog({}) is result
    # A new invocation can inspect unvisited names, not repeat persisted evidence.
    monkeypatch.setitem(FACTORIES, "ihg", original_factory)
    install_public_link_probe(sessions)
    assert await FACTORIES["ihg"]().discover_catalog({}) is result
    assert follow.await_count == 1
    async with sessions() as db:
        marker = await db.scalar(select(AppSetting))
        assert marker.value == {"hotel_name": "Six Senses Example", "outcome": "PUBLIC_PAGE_READ"}
    assert "six_senses_public_link" in capsys.readouterr().out
