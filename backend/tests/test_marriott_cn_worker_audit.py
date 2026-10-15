"""Offline verification of catalog-generated Marriott task persistence."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from audit_ihg_price import run_existing_price
from sqlalchemy import select
from test_domain import fixture_rate

from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory
from app.providers.marriott_cn_catalog import ROOT
from app.schemas.domain import HotelData, ProviderName
from app.services.rates import upsert_hotel
from app.services.watchlists import expand_global


async def test_automatic_marriott_price_job_is_written_once_without_notification_delivery(
    sessions, queue, monkeypatch
):
    settings = Settings(provider_overrides={"marriott": ProviderPolicy(enabled=True)}, global_jobs_per_tick=1)
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="audit:directory-only", value={"provider": "marriott"}))
        db.add(AppSetting(key="catalog:marriott", value={"root": ROOT, "pages": {}}))
        await upsert_hotel(
            db,
            HotelData(
                provider="marriott",
                provider_hotel_id="TEST0",
                hotel_name="Offline official identity",
                official_url="https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=TEST0&isSearch=true&currency=",
            ),
        )
        assert await expand_global(db, settings) == 1
        task = await db.scalar(select(CrawlJob))
        assert task.payload["automatic_catalog_root"] == ROOT
    quote = fixture_rate().model_copy(
        update={
            "provider": ProviderName.MARRIOTT,
            "provider_hotel_id": "TEST0",
            "check_in": date.today(),
            "check_out": date.today() + timedelta(days=1),
        }
    )
    execute = AsyncMock(return_value=[quote])
    monkeypatch.setattr(worker, "execute", execute)
    result = await run_existing_price(sessions, queue, settings, provider="marriott", emit=lambda _: None)
    assert result["outcome"] == "SUCCEEDED" and result["history_rows_persisted"] == 1
    assert result["telegram_delivery_called"] is False
    assert (await run_existing_price(sessions, queue, settings, provider="marriott", emit=lambda _: None))[
        "outcome"
    ] == "NO_PENDING_PRICE_DUE"
    execute.assert_awaited_once()
    async with sessions() as db:
        assert len((await db.scalars(select(PriceHistory))).all()) == 1
        assert (await db.scalar(select(Hotel))).active is True


async def test_marriott_audit_will_not_execute_an_unmarked_database(sessions, queue, monkeypatch):
    execute = AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    with pytest.raises(AssertionError, match="isolated MARRIOTT"):
        await run_existing_price(sessions, queue, Settings(), provider="marriott", emit=lambda _: None)
    execute.assert_not_awaited()
