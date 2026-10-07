from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as BrowserTimeout
from sqlalchemy import func, select
from test_hilton_page import (
    ENGLISH_HOTEL,
    ENGLISH_ROOMS_URL,
    HOTEL,
    ROOM,
    ROOMS_URL,
    SEARCH,
    english_rendered_page,
    rendered_page,
)

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, PriceHistory, ProviderStatus
from app.providers.hilton_browser import HiltonBrowserProvider, rooms_url
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services import jobs
from app.services.calendar import get_calendar
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel


class RenderedPage:
    """Browser navigation seam; DOM fields are projected from the observed official UI."""

    def __init__(self):
        self.url = ROOMS_URL
        self.room = ROOM
        self.selections = []
        self.is_closed = lambda: False

    async def content(self):
        return rendered_page().replace(ROOM, self.room)

    async def title(self):
        return "Selected room rates"

    async def wait_for_url(self, *_args, **_kwargs):
        pass

    def locator(self, selector):
        locator = MagicMock()
        locator.first.wait_for = AsyncMock()
        locator.input_value = AsyncMock(return_value="GBP")
        if 'data-roomtypecode="' in selector:
            code = selector.split('data-roomtypecode="')[1].split('"')[0]

            async def select_room():
                self.room = ROOM if code == "K1D" else "雙床豪華客房"
                self.url = "https://www.hilton.com/zh-hant/book/reservation/rates/"
                self.selections.append(code)

            locator.locator.return_value.first.click = AsyncMock(side_effect=select_room)
        return locator

    def get_by_test_id(self, test_id):
        locator = MagicMock()
        if test_id == "roomCardTile":
            locator.evaluate_all = AsyncMock(
                return_value=[
                    {"code": "K1D", "name": ROOM},
                    {"code": "T2D", "name": "雙床豪華客房"},
                ]
            )
        if test_id == "roomSelectedLabel":

            async def return_rooms():
                self.url = ROOMS_URL

            locator.first.click = AsyncMock(side_effect=return_rooms)
        return locator

    def get_by_role(self, *_args, **_kwargs):
        locator = MagicMock()
        locator.input_value = AsyncMock(return_value="GBP")
        return locator


def provider_with_page(monkeypatch):
    provider = HiltonBrowserProvider()
    page = RenderedPage()
    monkeypatch.setattr(provider, "_open_rooms", AsyncMock(return_value=(page, HOTEL)))
    return provider, page


def test_rooms_link_has_exact_dates_and_occupancy():
    assert rooms_url(SEARCH) == ROOMS_URL


async def test_visits_every_available_room_and_retains_stable_codes(monkeypatch):
    provider, page = provider_with_page(monkeypatch)
    rates = await provider.search_rates(SEARCH)
    assert page.selections == ["K1D", "T2D"]
    assert len(rates) == 8
    assert {rate.room_code for rate in rates} == {"K1D", "T2D"}
    assert all(rate.member_rate is False and str(rate.source_url) == ROOMS_URL for rate in rates)


async def test_reference_code_http_200_is_not_success(monkeypatch):
    provider = HiltonBrowserProvider()
    page = MagicMock()
    page.title = AsyncMock(return_value="Hilton Page Reference Code")
    with pytest.raises(ProviderError) as caught:
        await provider._check_access(page, 200)
    assert caught.value.code == ErrorCode.BLOCKED_BY_ANTIBOT


async def test_rooms_page_waits_for_complete_rendered_summary(monkeypatch):
    provider = HiltonBrowserProvider()
    page = MagicMock()
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="Rooms")
    page.url = ROOMS_URL
    page.get_by_test_id.return_value.wait_for = AsyncMock()
    page.wait_for_function = AsyncMock()
    page.content = AsyncMock(return_value=rendered_page())
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    _, name = await provider._open_rooms(SEARCH)
    assert name == HOTEL
    assert page.wait_for_function.await_args.kwargs["arg"] == [
        ["2026年10月20日", "2026年10月21日", "1 間客房，2 位成人"],
        ["October 20, 2026", "October 21, 2026", "1 room for 2 adults"],
    ]


async def test_catalog_redirect_to_wrong_property_is_not_imported(monkeypatch):
    provider = HiltonBrowserProvider()
    page = MagicMock()
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value="Different hotel")
    page.locator.return_value.first.wait_for = AsyncMock()
    page.url = "https://www.hilton.com/en/hotels/abekzhx-hampton-suites-kutztown/"
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        await provider.search_hotels(
            {"official_url": "https://www.hilton.com/en/hotels/aahhxhx-hampton-aachen-tivoli/"}
        )
    assert caught.value.code == ErrorCode.INVALID_RESPONSE


async def test_hilton_worker_persists_public_room_plans(sessions, queue, monkeypatch):
    # Recorded DOM has fixed stay dates; pin only the date-window clock, not its validation.
    class RecordedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 30)

    monkeypatch.setattr(jobs, "date", RecordedDate)
    monkeypatch.setenv("HILTON_ENABLED", "true")
    provider, _ = provider_with_page(monkeypatch)
    monkeypatch.setitem(FACTORIES, "hilton", lambda: provider)
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(
            session,
            HotelData(
                provider="hilton", provider_hotel_id="LONCOCI", hotel_name=HOTEL, official_url=ROOMS_URL
            ),
        )
        job = await enqueue(
            session,
            JobInput(
                provider="hilton",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=SEARCH.check_in,
                check_out=SEARCH.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    await process_one(sessions, queue, Settings())
    async with sessions() as session:
        assert (await session.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert (await session.get(ProviderStatus, "hilton")).status == "ONLINE"
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 8
        day = (await get_calendar(session, hotel, "2026-10"))["days"][19]
        assert day["status"] == "AVAILABLE" and day["currency"] == "GBP"
        assert float(day["current_low"]) == 548
        assert day["price_field"] == "cash_price"


class Popup:
    def __init__(self, page):
        self.page = page

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    @property
    def value(self):
        async def read():
            return self.page

        return read()


def homepage_journey(
    monkeypatch, *, results_url="https://www.hilton.com/en/search/", rooms_page_url=ENGLISH_ROOMS_URL
):
    provider = HiltonBrowserProvider()
    provider.configure_hotel(ENGLISH_HOTEL)
    pages = [MagicMock() for _ in range(3)]
    home, results, rooms = pages
    for page in pages:
        page.title = AsyncMock(return_value="Hilton")
        page.add_locator_handler = AsyncMock()
        page.wait_for_load_state = AsyncMock()
        page.is_closed.return_value = False
        page.close = AsyncMock()
    home.goto = AsyncMock(return_value=MagicMock(status=200))
    home.locator.return_value.wait_for = AsyncMock()
    home.locator.return_value.fill = AsyncMock()
    suggestion = home.get_by_role.return_value.filter.return_value
    suggestion.first.wait_for = AsyncMock()
    suggestion.count = AsyncMock(return_value=1)
    suggestion.click = AsyncMock()
    home.get_by_role.return_value.click = AsyncMock()
    home.get_by_role.return_value.get_by_role.return_value.click = AsyncMock()
    home.get_by_test_id.return_value.click = AsyncMock()
    home.expect_popup.return_value = Popup(results)
    results.url = results_url
    results.get_by_test_id.return_value.wait_for = AsyncMock()
    results.get_by_test_id.return_value.get_by_role.return_value.click = AsyncMock()
    results.expect_popup.return_value = Popup(rooms)
    rooms.url = rooms_page_url
    rooms.get_by_test_id.return_value.wait_for = AsyncMock()
    rooms.wait_for_function = AsyncMock()
    rooms.content = AsyncMock(return_value=english_rendered_page())
    provider._page = home
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=home))
    monkeypatch.setattr(provider, "_choose_day", AsyncMock())
    monkeypatch.setattr(provider, "_set_adults", AsyncMock())
    return provider, pages


async def test_catalog_hotel_uses_normal_home_search_then_exact_hotel_card(monkeypatch):
    provider, (home, results, rooms) = homepage_journey(monkeypatch)
    page, name = await provider._open_rooms(SEARCH)
    assert page is rooms and name == ENGLISH_HOTEL
    assert provider.headless is False
    assert home.goto.await_args.args == ("https://www.hilton.com/en/",)
    assert home.goto.await_args.kwargs["wait_until"] == "commit"
    home.locator.assert_called_once_with("#location-input")
    home.locator.return_value.fill.assert_awaited_once_with(ENGLISH_HOTEL)
    matcher = home.get_by_role.return_value.filter.call_args.kwargs["has_text"]
    assert matcher.search("Conrad London St. James London, United Kingdom")
    assert not matcher.search("The Trafalgar St. James London, Curio Collection by Hilton")
    assert provider._choose_day.await_args_list[0].args == (home, SEARCH.check_in, "in")
    assert provider._choose_day.await_args_list[1].args == (home, SEARCH.check_out, "out")
    provider._set_adults.assert_awaited_once_with(home, 2)
    home.get_by_test_id.assert_called_once_with("search-submit-button")
    results.get_by_test_id.assert_called_once_with("hotel-card-LONCOCI")
    assert provider._page is rooms
    await provider.close()
    assert all(page.close.await_count == 1 for page in (home, results, rooms))


@pytest.mark.parametrize("changed", ["search_host", "hotel", "date", "adults"])
async def test_home_search_changed_identity_is_rejected(monkeypatch, changed):
    results_url = (
        "https://other.example/en/search/"
        if changed == "search_host"
        else "https://www.hilton.com/en/search/"
    )
    room_url = ENGLISH_ROOMS_URL
    for field, old, new in (
        ("hotel", "LONCOCI", "ABEKZHX"),
        ("date", "2026-10-21", "2027-10-21"),
        ("adults", "room1NumAdults=2", "room1NumAdults=1"),
    ):
        if field == changed:
            room_url = room_url.replace(old, new)
    provider, _ = homepage_journey(monkeypatch, results_url=results_url, rooms_page_url=room_url)
    with pytest.raises(ProviderError) as caught:
        await provider._open_rooms(SEARCH)
    assert caught.value.code == ErrorCode.INVALID_RESPONSE
    await provider.close()


async def test_home_search_timeout_does_not_retry_a_deeplink(monkeypatch):
    provider, (home, _results, _rooms) = homepage_journey(monkeypatch)
    home.locator.return_value.wait_for.side_effect = BrowserTimeout("homepage controls absent")
    with pytest.raises(BrowserTimeout):
        await provider._open_rooms(SEARCH)
    assert home.goto.await_count == 1
    assert home.goto.await_args.args == ("https://www.hilton.com/en/",)
    assert not home.expect_popup.called


@pytest.mark.parametrize("failed_at", ["home navigation", "hotel search input"])
async def test_booking_timeout_identifies_actual_home_phase_without_another_navigation(
    monkeypatch, failed_at
):
    provider, (home, _results, _rooms) = homepage_journey(monkeypatch)
    if failed_at == "home navigation":
        home.goto.side_effect = BrowserTimeout("Recorded timeout")
    else:
        home.locator.return_value.wait_for.side_effect = BrowserTimeout("Recorded timeout")
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(SEARCH)
    assert caught.value.code == ErrorCode.TIMEOUT
    assert str(caught.value) == "Hilton booking page failed at " + failed_at
    assert home.goto.await_count == 1 and not home.expect_popup.called


async def test_home_search_ambiguous_suggestion_is_not_selected(monkeypatch):
    provider, (home, _results, _rooms) = homepage_journey(monkeypatch)
    suggestion = home.get_by_role.return_value.filter.return_value
    suggestion.count.return_value = 2
    with pytest.raises(ProviderError):
        await provider._open_rooms(SEARCH)
    suggestion.click.assert_not_awaited()
    assert not home.expect_popup.called


@pytest.mark.parametrize(
    "day,phase,expected",
    [
        (date(2026, 10, 20), "in", "Choose Tuesday, October 20, 2026 as your Check-in date."),
        (date(2027, 9, 29), "out", "CHOOSE WEDNESDAY, SEPTEMBER 29, 2027 AS YOUR CHECK-OUT DATE."),
    ],
)
async def test_calendar_uses_actual_english_labels_across_months(day, phase, expected):
    page, target, next_month = MagicMock(), MagicMock(), MagicMock()
    target.count = AsyncMock(side_effect=[0, 0, 1])
    target.click = AsyncMock()
    next_month.click = AsyncMock()
    labels = []

    def role(_role, *, name, **_kwargs):
        if isinstance(name, str):
            assert name == "Next Month"
            return next_month
        labels.append(name)
        return target

    page.get_by_role.side_effect = role
    await HiltonBrowserProvider()._choose_day(page, day, phase)
    assert labels[0].fullmatch(expected)
    assert next_month.click.await_count == 2 and target.click.await_count == 1


@pytest.mark.parametrize("start,adults", [(1, 2), (3, 1), (2, 2)])
async def test_adult_counter_reads_saved_count_instead_of_assuming_one(start, adults):
    page, dialog, add, remove, done = (MagicMock() for _ in range(5))
    state = {"count": start}

    async def increase():
        state["count"] += 1

    async def decrease():
        state["count"] -= 1

    add.wait_for = AsyncMock()
    add.evaluate = AsyncMock(
        side_effect=lambda *_args: f"{state['count']} adult" + ("s" if state["count"] > 1 else "")
    )
    add.click = AsyncMock(side_effect=increase)
    remove.click = AsyncMock(side_effect=decrease)
    done.click = AsyncMock()
    opener = MagicMock(click=AsyncMock())
    page.get_by_role.side_effect = lambda role, **_kwargs: dialog if role == "dialog" else opener
    buttons = {"Add adult to room 1": add, "Remove adult from room 1": remove, "Done": done}
    dialog.get_by_role.side_effect = lambda _role, *, name, **_kwargs: buttons[name]
    await HiltonBrowserProvider()._set_adults(page, adults)
    assert state["count"] == adults
    assert add.click.await_count + remove.click.await_count == abs(adults - start)
    done.click.assert_awaited_once()
