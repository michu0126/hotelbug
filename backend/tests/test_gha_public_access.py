from datetime import date, timedelta
from email.utils import format_datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import CrawlJob, ProviderStatus, utcnow
from app.providers.gha_browser import GHABrowserProvider
from app.schemas.domain import JobInput, JobKind, RateRequest
from app.services.jobs import enqueue

URL = "https://www.ghadiscovery.com/capella-hotels-and-resorts/capella-bangkok"


def page_with_response(status, retry_after=None):
    response = MagicMock(status=status)
    response.header_value = AsyncMock(return_value=retry_after)
    page = MagicMock()
    page.goto = AsyncMock(return_value=response)
    return page, response


@pytest.mark.parametrize("operation", ["detail", "booking"])
@pytest.mark.parametrize(
    "status,expected",
    [
        (401, ErrorCode.BLOCKED_BY_ANTIBOT),
        (403, ErrorCode.BLOCKED_BY_ANTIBOT),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.HTTP_ERROR),
    ],
)
async def test_public_rejection_is_classified_before_reading_hotel_or_date_controls(
    monkeypatch, operation, status, expected
):
    provider = GHABrowserProvider()
    page, response = page_with_response(status, "20000")
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    with pytest.raises(ProviderError) as rejected:
        if operation == "detail":
            await provider.search_hotels({"official_url": URL})
        else:
            await provider._open_booking(
                RateRequest(
                    provider_hotel_id="7894",
                    check_in=date.today(),
                    check_out=date.today() + timedelta(days=1),
                )
            )
    assert rejected.value.code == expected
    assert rejected.value.retry_after == (20000 if status == 429 else None)
    page.goto.assert_awaited_once()
    page.locator.assert_not_called()
    if status != 429:
        response.header_value.assert_not_awaited()


@pytest.mark.parametrize("value", [None, "", "invalid", "-20", "nan", "Infinity", "9" * 5000])
async def test_bad_retry_header_keeps_rate_limit_category_without_printing_raw_text(value):
    provider = GHABrowserProvider()
    _, response = page_with_response(429, value)
    with pytest.raises(ProviderError) as rejected:
        await provider._check_public_response(response)
    assert rejected.value.code == ErrorCode.RATE_LIMITED and rejected.value.retry_after is None
    assert str(rejected.value) == "GHA HTTP 429"


async def test_http_date_retry_header_is_honored():
    _, response = page_with_response(429, format_datetime(utcnow() + timedelta(seconds=22000), usegmt=True))
    with pytest.raises(ProviderError) as rejected:
        await GHABrowserProvider()._check_public_response(response)
    assert 21995 <= rejected.value.retry_after <= 22000


@pytest.mark.parametrize("status", [200, 204, 302])
async def test_non_error_response_does_not_pause_a_provider(status):
    _, response = page_with_response(status)
    await GHABrowserProvider()._check_public_response(response)
    response.header_value.assert_not_awaited()


@pytest.mark.parametrize("status,seconds", [(403, 21600), (429, 20000)])
async def test_real_adapter_category_reaches_worker_shared_pause_and_next_job_makes_no_request(
    sessions, queue, monkeypatch, status, seconds
):
    provider = GHABrowserProvider()
    page, _ = page_with_response(status, "20000")
    monkeypatch.setattr(provider, "_get_page", AsyncMock(return_value=page))
    monkeypatch.setattr(provider, "close", AsyncMock())
    monkeypatch.setattr(worker, "create_provider", lambda *args, **kwargs: provider)
    async with sessions() as db, db.begin():
        first = await enqueue(
            db, JobInput(provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": URL})
        )
        second = await enqueue(
            db,
            JobInput(
                provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": URL + "-different"}
            ),
        )
        first_id, second_id = first.id, second.id
    config = Settings(_env_file=None, provider_overrides={"gha": ProviderPolicy(enabled=True)})
    before = utcnow()
    await queue.put(first_id, 70)
    assert await worker.process_one(sessions, queue, config)
    await queue.put(second_id, 70)
    assert await worker.process_one(sessions, queue, config)
    page.goto.assert_awaited_once()
    async with sessions() as db:
        state = await db.get(ProviderStatus, "gha")
        assert state.status == "BLOCKED"
        delta = (state.blocked_until.replace(tzinfo=before.tzinfo) - before).total_seconds()
        assert seconds <= delta < seconds + 5
        first = await db.get(CrawlJob, first_id)
        second = await db.get(CrawlJob, second_id)
        assert first.error_type == ("BLOCKED_BY_ANTIBOT" if status == 403 else "RATE_LIMITED")
        assert second.status == "PENDING" and second.retry_count == 0
        assert second.scheduled_at == state.blocked_until
