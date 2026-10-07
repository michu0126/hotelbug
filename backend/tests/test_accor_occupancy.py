"""Stateful projection of the public guest controls observed 2026-10-02."""

from unittest.mock import AsyncMock

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider
from tests.test_accor_browser import request


class GuestControl:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    async def input_value(self):
        return str(self.page.values[self.selector])

    async def get_attribute(self, attribute):
        assert self.selector == "#compo-summary" and attribute == "aria-expanded"
        return str(self.page.expanded).lower()

    async def count(self):
        return self.page.values["#search-room-number"]

    async def wait_for(self, *, state):
        assert self.selector == "#booking-compo" and state == "visible"
        assert self.page.expanded

    @property
    def last(self):
        assert self.selector == "#booking-compo .booking__room"
        return self

    def get_by_role(self, role, *, name, exact):
        assert role == "button" and exact
        return GuestControl(self.page, name)

    async def click(self):
        self.page.clicks.append(self.selector)
        if self.selector == "#compo-summary":
            self.page.expanded = not self.page.expanded
            return
        assert self.page.expanded
        if self.page.no_progress:
            return
        if self.selector == "Remove the room":
            assert self.page.values["#search-room-number"] > 1
            self.page.values["#search-room-number"] -= 1
            return
        target = "#search-adult-room-0" if "adult" in self.selector else "#search-children-room-0"
        self.page.values[target] += 1 if self.selector.startswith("Add") else -1


class GuestPage:
    def __init__(self, adults=1, children=0, rooms=1, expanded=False, no_progress=False):
        self.values = {
            "#search-adult-room-0": adults,
            "#search-children-room-0": children,
            "#search-room-number": rooms,
        }
        self.expanded, self.no_progress = expanded, no_progress
        self.clicks = []

    def locator(self, selector):
        return GuestControl(self, selector)


class GuestExpectation:
    def __init__(self, control):
        self.control = control

    async def to_have_value(self, expected, *, timeout):
        assert timeout == 5000
        assert await self.control.input_value() == expected

    async def to_have_count(self, expected, *, timeout):
        assert timeout == 5000
        assert await self.control.count() == expected

    async def to_have_attribute(self, name, expected, *, timeout):
        assert timeout == 5000
        assert await self.control.get_attribute(name) == expected


@pytest.fixture(autouse=True)
def projected_expectations(monkeypatch):
    monkeypatch.setattr("app.providers.accor_browser.expect", GuestExpectation)


@pytest.mark.parametrize(
    "adults,children,rooms,target,expanded,expected_clicks",
    [
        (1, 0, 1, 2, False, ["Add an adult"]),
        (3, 0, 1, 1, False, ["Remove an adult", "Remove an adult"]),
        (2, 0, 1, 2, False, []),
        (1, 2, 1, 1, False, ["Remove a child", "Remove a child"]),
        (2, 1, 3, 3, True, ["Remove the room", "Remove the room", "Add an adult", "Remove a child"]),
        (1, 0, 1, 8, False, ["Add an adult"] * 7),
        (9, 6, 1, 2, False, ["Remove an adult"] * 7 + ["Remove a child"] * 6),
    ],
)
async def test_guest_controls_use_current_property_not_fixed_initial_state(
    adults, children, rooms, target, expanded, expected_clicks
):
    page = GuestPage(adults, children, rooms, expanded)
    await AccorBrowserProvider()._set_occupancy(page, request().model_copy(update={"adults": target}))
    assert page.values == {
        "#search-room-number": 1,
        "#search-adult-room-0": target,
        "#search-children-room-0": 0,
    }
    assert page.expanded is False
    assert [click for click in page.clicks if click != "#compo-summary"] == expected_clicks
    assert page.clicks.count("#compo-summary") == (1 if expanded else 2)


@pytest.mark.parametrize("adults,children,rooms", [(0, 0, 1), (10, 0, 1), (1, 7, 1), (1, 0, 0), ("?", 0, 1)])
async def test_changed_controls_fail_before_search_submission(adults, children, rooms):
    with pytest.raises(ProviderError) as caught:
        await AccorBrowserProvider()._set_occupancy(GuestPage(adults, children, rooms), request())
    assert caught.value.code == ErrorCode.PROVIDER_CHANGED


@pytest.mark.parametrize("rooms,adults", [(1, 1), (2, 2)])
async def test_no_progress_is_reported_without_submitting_or_clicking_repeatedly(monkeypatch, rooms, adults):
    provider = AccorBrowserProvider()
    page = GuestPage(adults=adults, rooms=rooms, no_progress=True)
    # Dates are already set by this projection. Exercise the real search error
    # mapping without replacing the occupancy normalization itself.
    dates = AsyncMock()
    original_locator = page.locator
    page.locator = lambda selector: (
        dates if selector.startswith('[name="search.date') else original_locator(selector)
    )
    monkeypatch.setattr(provider, "_open", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(request())
    assert caught.value.code == ErrorCode.INVALID_RESPONSE
    assert "guest count" in str(caught.value)
    assert len(page.clicks) == 2
    assert not any("rates" in action for action in page.clicks)
