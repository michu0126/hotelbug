"""Recorded public DOM seams exercise the flow, not live-site access."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from test_ihg_page import HOTEL, SEARCH, URL, room_html, window_html

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, NotificationLog, PriceHistory, ProviderStatus
from app.providers.ihg_browser import SAMPLE_URL, IHGBrowserProvider
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services import jobs
from app.services.calendar import get_calendar
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel


class RenderedPage:
    def __init__(self):
        self.url = URL
        self.expanded = []
        self.public = []
        self.clicked_controls = []
        self.wait_for_function = AsyncMock()
        self.wait_for_url = AsyncMock()

    async def content(self):
        return f'<h2 data-testid="hotelDetailsInfoHotelName">{HOTEL}</h2>'

    async def title(self):
        return "Select your room"

    def locator(self, selector):
        locator = MagicMock()
        locator.first.click = AsyncMock()
        locator.click = AsyncMock()
        if selector == "input.calendar-input":
            locator.evaluate_all = AsyncMock(return_value=["10/20/2026", "10/21/2026"])
        if selector == "#room-and-guest":
            locator.input_value = AsyncMock(return_value="1 Room, 2 Guests")
        if selector.startswith("app-room-rate-item"):
            locator.evaluate_all = AsyncMock(return_value=["ROOM_CODEOAAN", "ROOM_CODEOQNN"])
        if selector.startswith("#ROOM_CODE"):
            code = selector.removeprefix("#ROOM_CODE")
            locator.get_by_role.return_value.click = AsyncMock(side_effect=lambda: self.expanded.append(code))
            locator.locator.return_value.wait_for = AsyncMock()
            locator.locator.return_value.set_checked = AsyncMock(
                side_effect=lambda checked: self.public.append((code, checked))
            )
            locator.get_by_test_id.return_value.first.wait_for = AsyncMock()
            locator.inner_html = AsyncMock(return_value=room_html().replace("OAAN", code))
        return locator

    def get_by_test_id(self, _test_id):
        locator = MagicMock()
        locator.click = AsyncMock(side_effect=lambda **kwargs: self.clicked_controls.append(_test_id))
        locator.fill = AsyncMock()
        locator.press = AsyncMock()
        locator.is_visible = AsyncMock(return_value=False)
        locator.first.wait_for = AsyncMock()
        return locator

    def get_by_role(self, _role, **_kwargs):
        locator = MagicMock()
        locator.click = AsyncMock()
        return locator


def provider_with_page(monkeypatch):
    provider = IHGBrowserProvider()
    provider.configure_hotel_url(SAMPLE_URL)
    page = RenderedPage()
    hotel = HotelData(provider="ihg", provider_hotel_id="LONLS", hotel_name=HOTEL, official_url=SAMPLE_URL)
    monkeypatch.setattr(provider, "_open_property", AsyncMock(return_value=(page, hotel)))
    monkeypatch.setattr(provider, "_choose_day", AsyncMock())
    return provider, page, hotel


def test_default_ihg_mode_matches_verified_ordinary_browser():
    assert IHGBrowserProvider().headless is False
    assert IHGBrowserProvider(headless=True).headless is True


async def test_expands_every_room_and_waits_for_public_cards(monkeypatch):
    provider, page, _ = provider_with_page(monkeypatch)
    rates = await provider.search_rates(SEARCH)
    assert page.expanded == ["OAAN", "OQNN"]
    assert page.public == [("OAAN", False), ("OQNN", False)]
    assert "consolidate-search-submit-button" in page.clicked_controls
    assert [call.kwargs["arg"] for call in page.wait_for_function.await_args_list[2:]] == [
        "ROOM_CODEOAAN",
        "ROOM_CODEOQNN",
    ]
    assert page.wait_for_function.await_args_list[0].kwargs["arg"] == {
        "dates": ["10/20/2026", "10/21/2026"],
        "guests": "1 Room, 2 Guests",
    }
    assert len(rates) == 12 and all(rate.member_rate is False and rate.total_price is None for rate in rates)
    assert {rate.room_code for rate in rates} == {"OAAN", "OQNN"}
    assert [call.args[1] for call in provider._choose_day.await_args_list] == [
        SEARCH.check_in,
        SEARCH.check_out,
    ]


async def test_actual_403_is_a_failed_lookup_not_no_rooms():
    provider = IHGBrowserProvider()
    with pytest.raises(ProviderError) as caught:
        await provider._check_access(RenderedPage(), 403)
    assert caught.value.code == ErrorCode.BLOCKED_BY_ANTIBOT


async def test_property_waits_for_visible_hotel_not_global_load_event(monkeypatch):
    provider = IHGBrowserProvider()
    page = MagicMock()
    page.url = SAMPLE_URL
    page.goto = AsyncMock(return_value=MagicMock(status=200))
    page.title = AsyncMock(return_value=HOTEL)
    page.locator.return_value.first.wait_for = AsyncMock()
    page.content = AsyncMock(return_value=f"<h1>{HOTEL}</h1>")
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    _, hotel = await provider._open_property(SAMPLE_URL)
    assert hotel.hotel_name == HOTEL
    assert page.goto.await_args.kwargs["wait_until"] == "commit"
    page.locator.return_value.first.wait_for.assert_awaited_once()


async def test_displayed_date_mismatch_fails_before_quote_collection(monkeypatch):
    provider, page, _ = provider_with_page(monkeypatch)
    page.url = URL.replace("qCiMy=092026", "qCiMy=092027")
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(SEARCH)
    assert caught.value.code == ErrorCode.INVALID_RESPONSE and not page.expanded


async def test_closed_booking_window_is_not_transport_timeout(monkeypatch):
    provider, page, _ = provider_with_page(monkeypatch)
    page.content = AsyncMock(return_value=window_html())
    # The real IHG response clears the booking URL query string.
    page.url = URL.split("?")[0]
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(SEARCH)
    assert caught.value.code == ErrorCode.BOOKING_WINDOW_CLOSED and not page.expanded


async def test_worker_records_not_open_without_quotes_or_provider_failure(sessions, queue, monkeypatch):
    class RecordedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 30)

    monkeypatch.setattr(jobs, "date", RecordedDate)
    monkeypatch.setenv("IHG_ENABLED", "true")
    provider, page, metadata = provider_with_page(monkeypatch)
    page.content = AsyncMock(return_value=window_html())
    page.url = URL.split("?")[0]
    monkeypatch.setitem(FACTORIES, "ihg", lambda: provider)
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, metadata)
        job = await enqueue(
            session,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=SEARCH.check_in,
                check_out=SEARCH.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as session:
        job = await session.get(CrawlJob, job_id)
        assert job.status == "SUCCEEDED" and job.error_type == "BOOKING_WINDOW_CLOSED"
        assert job.retry_count == 0
        state = await session.get(ProviderStatus, "ihg")
        assert state.status == "ONLINE" and state.failure_count == 0 and state.blocked_until is None
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
        assert (await get_calendar(session, hotel, "2026-10"))["days"][19]["status"] == "NOT_OPEN"


async def test_ihg_worker_persists_cash_quotes_and_calendar(sessions, queue, monkeypatch):
    class RecordedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 30)

    monkeypatch.setattr(jobs, "date", RecordedDate)
    monkeypatch.setenv("IHG_ENABLED", "true")
    provider, _, metadata = provider_with_page(monkeypatch)
    monkeypatch.setitem(FACTORIES, "ihg", lambda: provider)
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, metadata)
        job = await enqueue(
            session,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=SEARCH.check_in,
                check_out=SEARCH.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as session:
        assert (await session.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert (await session.get(ProviderStatus, "ihg")).status == "ONLINE"
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 12
        day = (await get_calendar(session, hotel, "2026-10"))["days"][19]
        assert day["status"] == "AVAILABLE" and day["price_field"] == "cash_price"
        assert float(day["current_low"]) == 368 and day["currency"] == "GBP"
