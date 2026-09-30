from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from test_hilton_page import HOTEL, ROOM, ROOMS_URL, SEARCH, rendered_page

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
        "2026年10月20日",
        "2026年10月21日",
        "1 間客房，2 位成人",
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
