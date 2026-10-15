"""HTTP response/Worker seams only; never start a browser or contact a website."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import Error as BrowserError
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import CrawlJob, NotificationLog, PriceHistory, ProviderStatus
from app.providers import retry_after
from app.providers.hilton_browser import HiltonBrowserProvider
from app.providers.hyatt_browser import HyattBrowserProvider
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind, RateRequest
from app.services.jobs import enqueue

NOW = datetime(2026, 10, 14, 4, 0, 0, 250000, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, None),
        ("", None),
        ("nonsense", None),
        ("-1", None),
        ("+1", None),
        ("1.5", None),
        ("NaN", None),
        ("١٢", None),
        ("1" * 10000, None),
        ("Wed, 14 Oct 2026 05:00:00", None),
        ("0", 0),
        ("  7200\t", 7200),
        ("999999999", 999999999),
        ("Wed, 14 Oct 2026 04:00:00 GMT", 0),
        ("Wed, 14 Oct 2026 04:00:01 GMT", 1),
        ("Wed, 14 Oct 2026 05:00:00 GMT", 3600),
    ],
)
def test_retry_after_header_seconds_and_dates(value, expected):
    assert retry_after.retry_after_seconds(value, now=NOW) == expected


@pytest.fixture
def frozen_header_clock(monkeypatch):
    class HeaderClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(retry_after, "datetime", HeaderClock)


def rejection_page(status=429, header="7200"):
    response = MagicMock(status=status, header_value=AsyncMock(return_value=header))
    page = MagicMock(goto=AsyncMock(return_value=response), title=AsyncMock(return_value=""))
    return page, response


@pytest.mark.parametrize("kind", ["hyatt", "hilton_home", "hilton_rooms", "hilton_detail"])
@pytest.mark.parametrize("header", ["7200", "Wed, 14 Oct 2026 06:00:00 GMT"])
async def test_existing_navigation_retains_retry_after_before_page_controls(
    monkeypatch, frozen_header_clock, kind, header
):
    page, response = rejection_page(header=header)
    provider = HyattBrowserProvider() if kind == "hyatt" else HiltonBrowserProvider()
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    request = RateRequest(
        provider_hotel_id="SHACP" if kind == "hyatt" else "LONCOCI",
        check_in=NOW.date(),
        check_out=NOW.date() + timedelta(days=1),
    )
    if kind == "hilton_home":
        provider.configure_hotel("Conrad London St. James")
    with pytest.raises(ProviderError) as caught:
        if kind == "hilton_detail":
            await provider.search_hotels(
                {"official_url": "https://www.hilton.com/en/hotels/loncoci-conrad-london-st-james/"}
            )
        else:
            await provider.search_rates(request)
    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert caught.value.retry_after == 7200
    response.header_value.assert_awaited_once_with("retry-after")
    page.goto.assert_awaited_once()
    page.locator.assert_not_called()
    page.get_by_role.assert_not_called()
    page.get_by_test_id.assert_not_called()
    page.title.assert_not_awaited()


@pytest.mark.parametrize("provider_type", [HyattBrowserProvider, HiltonBrowserProvider])
@pytest.mark.parametrize("header", [None, "invalid", "header_read_error"])
async def test_unusable_header_does_not_replace_known_rate_limit(monkeypatch, provider_type, header):
    page, response = rejection_page(header=header)
    if header == "header_read_error":
        response.header_value.side_effect = BrowserError("Response is no longer available")
    provider = provider_type()
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        if provider_type is HyattBrowserProvider:
            await provider._navigate("https://www.hyatt.com/zh-CN/explore-hotels/list/")
        else:
            await provider._check_response(page, response)
    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert caught.value.retry_after is None


@pytest.mark.parametrize("provider_type", [HyattBrowserProvider, HiltonBrowserProvider])
async def test_403_keeps_existing_classification_without_reading_retry_header(monkeypatch, provider_type):
    page, response = rejection_page(status=403)
    provider = provider_type()
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as caught:
        if provider_type is HyattBrowserProvider:
            await provider._navigate("https://www.hyatt.com/zh-CN/explore-hotels/list/")
        else:
            await provider._check_response(page, response)
    assert caught.value.code == ErrorCode.BLOCKED_BY_ANTIBOT
    assert caught.value.retry_after is None
    response.header_value.assert_not_awaited()


@pytest.mark.parametrize(
    "name,provider_type", [("hyatt", HyattBrowserProvider), ("hilton", HiltonBrowserProvider)]
)
@pytest.mark.parametrize("seconds,expected", [(0, 3600), (7200, 7200)])
async def test_worker_preserves_minimum_pause_and_longer_server_date(
    sessions, queue, monkeypatch, frozen_header_clock, name, provider_type, seconds, expected
):
    header = format_datetime(NOW.replace(microsecond=0) + timedelta(seconds=seconds), usegmt=True)
    page, _response = rejection_page(header=header)
    provider = provider_type()
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr(provider, "close", AsyncMock())
    monkeypatch.setitem(FACTORIES, name, lambda: provider)
    monkeypatch.setattr(worker, "utcnow", lambda: NOW)
    settings = Settings(provider_overrides={name: ProviderPolicy(enabled=True)})
    async with sessions() as db, db.begin():
        job = await enqueue(db, JobInput(provider=name, kind=JobKind.PROVIDER_HEALTHCHECK))
        job.scheduled_at = NOW  # The enqueue clock must agree with the frozen Worker/header clock.
        job_id = job.id
    await queue.put(job_id, 90)
    assert await worker.process_one(sessions, queue, settings)
    async with sessions() as db:
        job = await db.get(CrawlJob, job_id)
        state = await db.get(ProviderStatus, name)
        assert job.status == "PENDING" and job.error_type == "RATE_LIMITED"
        assert state.status == "BLOCKED"
        assert state.blocked_until.replace(tzinfo=timezone.utc) == NOW + timedelta(seconds=expected)
        assert job.scheduled_at == state.blocked_until
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0
    page.goto.assert_awaited_once()
