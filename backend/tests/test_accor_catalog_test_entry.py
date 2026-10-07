from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.models.tables import CrawlJob, Hotel, PriceHistory, StayScan
from app.providers.accor_browser import AccorBrowserProvider
from app.schemas.domain import HotelData, JobInput, JobKind
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel


@pytest.mark.parametrize(
    "name,test_entry",
    [
        ("Test Hotel for the helpdesk resynch", True),
        ("  TEST HOTEL for the helpdesk resynch  ", True),
        ("Test Hotel for the helpdesk resynch 4 stars", True),
        ("Test Hotel", False),
        ("Mercure Annecy Sud Hotel", False),
    ],
)
async def test_only_explicit_helpdesk_placeholder_is_disabled(monkeypatch, name, test_entry):
    provider = AccorBrowserProvider()
    heading = MagicMock()
    heading.first = heading
    heading.inner_text = AsyncMock(return_value=name)
    page = MagicMock()
    page.locator.return_value = heading
    page.url = "https://all.accor.com/hotel/0339/index.en.shtml"
    page.content = AsyncMock(return_value="<h1>Explicit offline heading-only hotel</h1>")
    monkeypatch.setattr(provider, "_open", AsyncMock(return_value=page))
    data = await provider.get_hotel_details("0339")
    assert data.active is not test_entry
    assert ("active" in data.model_fields_set) is test_entry


@pytest.mark.parametrize("kind", [JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY])
async def test_queued_prices_for_disabled_hotel_do_not_open_official_site(sessions, queue, monkeypatch, kind):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="accor",
                provider_hotel_id="0339",
                hotel_name="Explicit offline test entry",
                official_url="https://all.accor.com/hotel/0339/index.en.shtml",
                active=False,
            ),
        )
        job = await enqueue(
            db,
            JobInput(
                provider="accor",
                kind=kind,
                hotel_id=hotel.id,
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
            ),
        )
        job_id = job.id
    execute = AsyncMock(side_effect=AssertionError("Disabled hotel must not issue a price request"))
    monkeypatch.setattr(worker, "execute", execute)
    config = Settings(_env_file=None, provider_overrides={"accor": ProviderPolicy(enabled=True)})
    await queue.put(job_id, 80)
    assert await worker.process_one(sessions, queue, config)
    execute.assert_not_awaited()
    async with sessions() as db:
        saved = await db.get(CrawlJob, job_id)
        assert saved.status == "CANCELLED" and saved.error_message == "Hotel disabled"
        assert saved.lease_owner is saved.lease_until is None
        assert (await db.get(Hotel, hotel.id)).active is False
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await db.scalar(select(func.count()).select_from(StayScan)) == 0
