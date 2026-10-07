"""Public room-selection journey, observed on Accor's normal booking page."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout

from app.core.errors import ErrorCode, ProviderError
from app.providers.accor_browser import AccorBrowserProvider, parse_public_rates
from tests.test_accor_browser import rendered_page, request


class RoomPage:
    def __init__(self, names, *, no_progress=False, changed=False, malformed=False):
        self.names = names
        self.selected = 0
        self.clicks = []
        self.no_progress, self.changed, self.malformed = no_progress, changed, malformed
        self.url = "https://all.accor.com/booking/en/accor/hotel/0338"
        self.snapshots = 0

    def locator(self, selector):
        return RoomControl(self, selector)

    async def content(self):
        self.snapshots += 1
        html = rendered_page().replace("Standard Room with 1 double bed", self.names[self.selected])
        if self.malformed and self.selected:
            html = html.replace("Taxes and fees included.", "Excluding taxes")
        return html


class RoomControl:
    def __init__(self, page, selector, index=None):
        self.page, self.selector, self.index = page, selector, index

    def nth(self, index):
        assert self.selector == ".hotel-accommodations-offers__item"
        return RoomControl(self.page, self.selector, index)

    def locator(self, selector):
        return RoomControl(self.page, selector, self.index)

    @property
    def first(self):
        return self

    def get_by_role(self, role, *, name, exact):
        assert role == "button" and name == "Choose this room" and exact
        return RoomControl(self.page, name, self.index)

    async def count(self):
        return len(self.page.names) if self.index is None else 1

    async def all_text_contents(self):
        assert self.selector == ".hotel-accommodations-offers__item-title"
        if self.page.changed and self.page.snapshots:
            return [*self.page.names, "new room"]
        return self.page.names

    async def inner_text(self):
        return self.page.names[self.index]

    async def is_visible(self):
        return self.index == self.page.selected

    async def click(self, *, timeout):
        assert 0 < timeout <= 45000
        assert self.selector == "Choose this room"
        self.page.clicks.append(self.index)
        if not self.page.no_progress:
            self.page.selected = self.index

    async def wait_for(self, *, state, timeout):
        assert state == "visible" and 0 < timeout <= 45000
        if not await self.is_visible():
            raise BrowserTimeout("Room selection did not expose public plans")


@pytest.mark.parametrize("count", [1, 2, 15])
async def test_all_rooms_read_before_collapse_without_ten_room_limit(count):
    page = RoomPage([f"Official room {index}" for index in range(count)])
    rates = await AccorBrowserProvider()._collect_public_rates(page, request())
    assert len(rates) == count
    assert {rate.room_type for rate in rates} == set(page.names)
    assert len({rate.offer_key() for rate in rates}) == count
    assert page.snapshots == count
    assert page.clicks == list(range(1, count))
    assert all(rate.member_rate is False for rate in rates)


@pytest.mark.parametrize("names", [[], [""], ["Same room", "Same room"]])
async def test_missing_room_identity_not_reported_as_complete(names):
    with pytest.raises(ProviderError) as caught:
        await AccorBrowserProvider()._collect_public_rates(RoomPage(names), request())
    assert caught.value.code == ErrorCode.PROVIDER_CHANGED


async def test_final_room_list_change_rejects_partial_result():
    with pytest.raises(ProviderError) as caught:
        await AccorBrowserProvider()._collect_public_rates(RoomPage(["A", "B"], changed=True), request())
    assert caught.value.code == ErrorCode.PROVIDER_CHANGED


async def test_stalled_room_selection_returns_no_partial_success():
    page = RoomPage(["A", "B"], no_progress=True)
    with pytest.raises(BrowserTimeout):
        await AccorBrowserProvider()._collect_public_rates(page, request())
    assert page.snapshots == 1 and page.clicks == [1]


async def test_second_room_unverified_tax_terms_returns_no_partial_success():
    with pytest.raises(ProviderError):
        await AccorBrowserProvider()._collect_public_rates(RoomPage(["A", "B"], malformed=True), request())


async def test_all_room_collection_has_one_shared_deadline(monkeypatch):
    timeline = iter([0, 46])
    monkeypatch.setattr("app.providers.accor_browser.monotonic", lambda: next(timeline))
    with pytest.raises(BrowserTimeout):
        await AccorBrowserProvider()._collect_public_rates(RoomPage(["A", "B"]), request())


def test_collapsed_room_summary_not_imported_as_named_public_plan():
    html = (
        rendered_page()
        + """<section><h3 class="hotel-accommodations-offers__item-title">Second room</h3>
        <p class="offer-price offer-price--alternative">Public rate from €1.00</p>
        <p>1 night 2 adults Taxes and fees included.</p><button>Choose this room</button></section>"""
    )
    rates = parse_public_rates(html, request(), "https://all.accor.com/booking/en/accor/hotel/0338")
    assert len(rates) == 1 and rates[0].room_type != "Second room"


def test_room_and_plan_whitespace_normalization_preserves_offer_identity():
    url = "https://all.accor.com/booking/en/accor/hotel/0338"
    original = parse_public_rates(rendered_page(), request(), url)[0]
    html = rendered_page().replace("Standard Room with 1 double bed", " Standard   Room\nwith 1 double bed ")
    html = html.replace("SAVER RATE</span>", " SAVER\n  RATE </span>")
    changed = parse_public_rates(html, request(), url)[0]
    assert changed.offer_key() == original.offer_key()


async def test_all_room_timeout_is_typed_at_real_search_boundary(monkeypatch):
    provider = AccorBrowserProvider()
    page = MagicMock()
    control = MagicMock()
    control.filter.return_value = control
    control.first = control
    control.get_by_role.return_value = control
    for action in ("fill", "press", "click", "wait_for"):
        setattr(control, action, AsyncMock())
    page.locator.return_value = control
    page.wait_for_url = AsyncMock()
    monkeypatch.setattr(provider, "_open", AsyncMock(return_value=page))
    monkeypatch.setattr(provider, "_set_occupancy", AsyncMock())
    monkeypatch.setattr(provider, "_collect_public_rates", AsyncMock(side_effect=BrowserTimeout("stalled")))
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(request())
    assert caught.value.code == ErrorCode.TIMEOUT
    assert "all room offers" in str(caught.value)
