"""Browser seams and Worker fixtures. No test here calls a hotel website."""

from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select
from test_hyatt_page import HOTEL, ROOM, SEARCH, modal_html, parse

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler.worker import process_one
from app.models.tables import CrawlJob, NotificationLog, PriceHistory, ProviderStatus, StayScan
from app.providers.hyatt_browser import FRAME, HyattBrowserProvider
from app.providers.hyatt_page import PROPERTY_URL, rooms_url
from app.providers.registry import FACTORIES, create_provider
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services import jobs
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel


def test_registry_uses_dedicated_hyatt_session_and_runtime_settings():
    provider = create_provider(
        "hyatt",
        HOTEL,
        PROPERTY_URL,
        Settings(
            browser_channel="chrome",
            browser_proxy_url="http://proxy:7890",
            browser_session_dir="isolated-test-profile",
        ),
    )
    assert isinstance(provider, HyattBrowserProvider)
    assert provider.session_provider == "hyatt" and not provider.headless
    assert provider.proxy_server == "http://proxy:7890" and provider.browser_channel == "chrome"
    assert provider.expected_hotel_name == HOTEL and provider.official_url == PROPERTY_URL


@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.BLOCKED_BY_ANTIBOT),
        (403, ErrorCode.BLOCKED_BY_ANTIBOT),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.HTTP_ERROR),
    ],
)
async def test_rejection_is_not_parsed_as_empty_availability(status, code):
    page = MagicMock(title=AsyncMock(return_value=""))
    with pytest.raises(ProviderError) as caught:
        await HyattBrowserProvider()._check_access(page, status, retry_after=600)
    assert caught.value.code == code
    if status == 429:
        assert caught.value.retry_after == 600
    page.title.assert_not_awaited()


async def test_429_stops_before_any_room_controls(monkeypatch):
    provider = HyattBrowserProvider()
    response = MagicMock(status=429, header_value=AsyncMock(return_value="1200"))
    page = MagicMock(goto=AsyncMock(return_value=response))
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        await provider.search_rates(SEARCH)
    assert caught.value.code == ErrorCode.RATE_LIMITED and caught.value.retry_after == 1200
    assert page.goto.await_count == 1
    assert page.goto.await_args.kwargs["wait_until"] == "commit"
    page.locator.assert_not_called()


class RenderedPage:
    """Small deterministic UI seam: two categories, two room codes, two plans."""

    def __init__(self):
        self.url = rooms_url(SEARCH)
        self.category = "客房 (1)"
        self.plan = "MYHI"
        self.clicks = []
        self.wait_for_function = AsyncMock()

    async def content(self):
        room = ROOM if self.category == "客房 (1)" else "嘉荟套房（1 张特大床）"
        code = "KING" if self.category == "客房 (1)" else "CAST"
        html = modal_html(self.plan, room_code=code, room_name=room)
        if code == "CAST":
            html = html.replace("¥487", "¥1,026").replace("¥497", "¥1,047")
        return html

    async def title(self):
        return "凯悦 | 选择客房"

    def locator(self, selector):
        locator = MagicMock()
        locator.first.wait_for = AsyncMock()
        locator.wait_for = AsyncMock()
        if selector == "#currency-select":
            locator.input_value = AsyncMock(return_value="")
        if selector == '[role="tabpanel"][aria-hidden="false"]':
            locator.locator.side_effect = self.locator
        elif selector == '[data-module="room-card"]':
            code = "KING" if self.category == "客房 (1)" else "CAST"
            name = ROOM if code == "KING" else "嘉荟套房（1 张特大床）"
            locator.evaluate_all = AsyncMock(
                return_value=[{"code": code, "name": name, "price": "¥487" if code == "KING" else "¥1,026"}]
            )
        elif selector == FRAME:
            locator.locator.side_effect = self.locator
            locator.get_by_role.side_effect = self.get_by_role
        elif selector == 'input[name="rate"]':
            locator.evaluate_all = AsyncMock(return_value=["MYHI", "RACK"])
        elif selector == '[role="radio"]':

            def select_plan(*, has):
                async def click():
                    self.plan = has._selector.split('"')[1]
                    self.clicks.append("plan:" + self.plan)

                return MagicMock(click=AsyncMock(side_effect=click))

            locator.filter.side_effect = select_plan
        elif "more-rates" in selector:

            async def open_dialog():
                self.plan = "MYHI"
                self.clicks.append(selector)

            locator.click = AsyncMock(side_effect=open_dialog)
        locator._selector = selector
        return locator

    def get_by_role(self, role, **kwargs):
        locator = MagicMock()
        if role == "tablist":
            locator.get_by_role.side_effect = self.get_by_role
        elif role == "tab":
            locator.all_text_contents = AsyncMock(return_value=["客房 (1)", "套房 (1)"])

            def choose_category(*, has_text):
                async def click():
                    self.category = "客房 (1)" if has_text.search("客房 (1)") else "套房 (1)"
                    self.clicks.append("category:" + self.category)

                return MagicMock(click=AsyncMock(side_effect=click))

            locator.filter.side_effect = choose_category
        elif role == "button" and kwargs.get("name") == "Close":
            locator.click = AsyncMock(side_effect=lambda: self.clicks.append("close"))
        else:
            raise AssertionError("Unexpected potentially consequential control")
        return locator


async def test_reads_every_category_room_and_selected_plan_without_booking(monkeypatch):
    provider = HyattBrowserProvider()
    provider.configure_hotel(HOTEL)
    page = RenderedPage()
    monkeypatch.setattr(provider, "_navigate", AsyncMock(return_value=page))
    rates = await provider.search_rates(SEARCH)
    assert len(rates) == 4
    assert [rate.room_code for rate in rates] == ["KING", "KING", "CAST", "CAST"]
    assert [rate.rate_code for rate in rates] == ["MYHI", "RACK", "MYHI", "RACK"]
    assert [str(rate.cash_price) for rate in rates] == ["487", "497", "1026", "1047"]
    assert page.clicks.count("close") == 2
    assert [c for c in page.clicks if c.startswith("category:")] == ["category:客房 (1)", "category:套房 (1)"]
    assert provider._navigate.await_count == 1


async def test_wrong_configured_property_fails_before_navigation(monkeypatch):
    provider = HyattBrowserProvider()
    provider.configure_hotel_url(PROPERTY_URL.replace("shacp-caption", "sydph-caption"))
    navigate = AsyncMock()
    monkeypatch.setattr(provider, "_navigate", navigate)
    with pytest.raises(ProviderError):
        await provider.search_rates(SEARCH)
    navigate.assert_not_awaited()


async def test_remembered_converted_currency_is_not_mislabeled_native():
    page = MagicMock()
    page.locator.return_value.input_value = AsyncMock(return_value="USD")
    with pytest.raises(ProviderError) as caught:
        await HyattBrowserProvider()._verify_native_currency(page)
    assert caught.value.code == ErrorCode.INVALID_RESPONSE


async def test_offline_selected_quote_reaches_worker_history_calendar(sessions, queue, monkeypatch):
    class FixtureDate(date):
        @classmethod
        def today(cls):
            return SEARCH.check_in

    # This is a recorded 2026-10-06 fixture, not a new live stay. Freeze only
    # its test clock; production still rejects expired jobs after midnight.
    monkeypatch.setattr(jobs, "date", FixtureDate)

    class FixtureHyatt(HyattBrowserProvider):
        async def search_rates(self, request):
            assert request == SEARCH
            return [parse()]

    monkeypatch.setitem(FACTORIES, "hyatt", FixtureHyatt)
    monkeypatch.setenv("HYATT_ENABLED", "true")
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="hyatt", provider_hotel_id="SHACP", hotel_name=HOTEL, official_url=PROPERTY_URL
            ),
        )
        job = await enqueue(
            db,
            JobInput(
                provider="hyatt",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=SEARCH.check_in,
                check_out=SEARCH.check_out,
                payload={"adults": 1, "rooms": 1},
            ),
        )
        job_id = job.id
    await queue.put(job_id, 90)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "SUCCEEDED"
        row = await db.scalar(select(PriceHistory).where(PriceHistory.job_id == job_id))
        assert row.cash_price == 497 and row.provider == "hyatt" and row.adults == 1
        assert await db.scalar(select(func.count()).select_from(StayScan)) == 1
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_offline_429_worker_pauses_provider_without_fake_prices(sessions, queue, monkeypatch):
    class LimitedHyatt(HyattBrowserProvider):
        async def health_check(self):
            raise ProviderError(ErrorCode.RATE_LIMITED, "Hyatt returned HTTP 429", retry_after=1200)

    monkeypatch.setitem(FACTORIES, "hyatt", LimitedHyatt)
    monkeypatch.setenv("HYATT_ENABLED", "true")
    async with sessions() as db, db.begin():
        job = await enqueue(db, JobInput(provider="hyatt", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = job.id
    await queue.put(job_id, 90)
    assert await process_one(sessions, queue, Settings())
    async with sessions() as db:
        job = await db.get(CrawlJob, job_id)
        assert job.status == "PENDING" and job.error_type == "RATE_LIMITED"
        assert job.retry_count == 1
        assert (await db.get(ProviderStatus, "hyatt")).blocked_until is not None
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0
