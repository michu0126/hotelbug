"""Global catalog refresh and paused-group scheduling must preserve usable coverage."""

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_domain import fixture_rate

from app.core.config import ProviderPolicy, Settings
from app.crawler import worker
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    PriceHistory,
    ProviderStatus,
    Rate,
    Watchlist,
    utcnow,
)
from app.providers.ihg_catalog import ROOT
from app.schemas.domain import CatalogPageData, HotelData, JobInput, JobKind, ProviderName
from app.services.jobs import enqueue
from app.services.rates import persist_rates, upsert_hotel
from app.services.watchlists import expand_global, expand_watchlists


async def test_sparse_directory_refresh_retains_verified_metadata_and_country_watch(sessions):
    settings = Settings(provider_overrides={"hyatt": ProviderPolicy(enabled=True)})
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="hyatt",
                provider_hotel_id="SHACP",
                hotel_name="Offline Hyatt",
                official_url="https://www.hyatt.com/caption-by-hyatt/zh-CN/shacp-caption-by-hyatt-zhongshan-park-shanghai",
                city="Shanghai",
                country="CN",
                address="Offline address",
                latitude=Decimal("31.22"),
                longitude=Decimal("121.42"),
                default_currency="CNY",
                brand="caption-by-hyatt",
            ),
        )
        hotel_id = hotel.id
        db.add(Watchlist(name="Offline CN", scope="country", filters={"country": "CN", "days_ahead": 365}))
    async with sessions() as db, db.begin():
        sparse = HotelData(
            provider="hyatt",
            provider_hotel_id="SHACP",
            hotel_name="Offline refreshed Hyatt",
            official_url="https://www.hyatt.com/caption-by-hyatt/zh-CN/shacp-caption-by-hyatt-zhongshan-park-shanghai",
        )
        hotel = await upsert_hotel(db, sparse)
        assert hotel.id == hotel_id and hotel.hotel_name == "Offline refreshed Hyatt"
        assert (hotel.country, hotel.city, hotel.address, hotel.default_currency, hotel.brand) == (
            "CN",
            "Shanghai",
            "Offline address",
            "CNY",
            "caption-by-hyatt",
        )
        assert hotel.latitude == Decimal("31.22") and hotel.longitude == Decimal("121.42")
        assert await expand_watchlists(db, settings) == 1


async def test_sparse_catalog_refresh_does_not_reactivate_inactive_hotel(sessions):
    async with sessions() as db, db.begin():
        url = "https://www.marriott.com/"
        await upsert_hotel(
            db,
            HotelData(
                provider="marriott",
                provider_hotel_id="TEST1",
                hotel_name="Offline",
                official_url=url,
                active=False,
            ),
        )
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="marriott", provider_hotel_id="TEST1", hotel_name="Offline new", official_url=url
            ),
        )
        assert not hotel.active


async def test_paused_large_group_cannot_consume_all_global_frontier_inspection(sessions):
    settings = Settings(
        global_jobs_per_tick=2,
        provider_overrides={name: ProviderPolicy(enabled=True) for name in ("marriott", "gha")},
    )
    async with sessions() as db, db.begin():
        for i in range(50):
            db.add(
                Hotel(
                    id=f"a-paused-{i:03d}",
                    provider="marriott",
                    provider_hotel_id=f"T{i:04d}",
                    hotel_name="Offline paused",
                    official_url="https://www.marriott.com/",
                )
            )
        db.add(
            Hotel(
                id="z-healthy",
                provider="gha",
                provider_hotel_id="10624",
                hotel_name="Offline healthy",
                official_url="https://www.ghadiscovery.com/",
            )
        )
        db.add(ProviderStatus(provider="marriott", blocked_until=utcnow() + timedelta(hours=1)))
    async with sessions() as db, db.begin():
        assert await expand_global(db, settings) == 1
        job = await db.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE))
        assert job.hotel_id == "z-healthy" and job.check_in == date.today()
        assert await db.get(AppSetting, "global_date_cursor:a-paused-000") is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("brand", None),
        ("country", None),
        ("region", None),
        ("city", None),
        ("address", None),
        ("latitude", None),
        ("longitude", None),
        ("default_currency", None),
        ("brand", "   "),
        ("country", ""),
        ("city", "\t"),
    ],
)
async def test_unknown_new_metadata_cannot_erase_known_value(sessions, field, value):
    initial = {
        "provider": "marriott",
        "provider_hotel_id": "TEST1",
        "hotel_name": "Offline",
        "official_url": "https://www.marriott.com/",
        "brand": "Known brand",
        "country": "CN",
        "region": "Known region",
        "city": "Known city",
        "address": "Known address",
        "latitude": Decimal("12.3"),
        "longitude": Decimal("45.6"),
        "default_currency": "CNY",
    }
    async with sessions() as db, db.begin():
        await upsert_hotel(db, HotelData(**initial))
    async with sessions() as db, db.begin():
        updated = await upsert_hotel(db, HotelData(**{**initial, field: value}))
        assert getattr(updated, field) == initial[field]


async def test_new_hotel_unknown_strings_become_null_and_nonempty_updates_apply(sessions):
    identity = {
        "provider": "marriott",
        "provider_hotel_id": "TEST1",
        "hotel_name": "Offline",
        "official_url": "https://www.marriott.com/",
    }
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, HotelData(**identity, country="   ", city="", brand="\t"))
        assert hotel.active and hotel.country is None and hotel.city is None and hotel.brand is None
        original = hotel.id
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                **{**identity, "hotel_name": "Offline new"},
                country=" US ",
                city=" New York ",
                brand=" Known ",
                latitude=0,
                longitude=0,
            ),
        )
        assert hotel.id == original and hotel.hotel_name == "Offline new"
        assert (hotel.country, hotel.city, hotel.brand) == ("US", "New York", "Known")
        assert hotel.latitude == hotel.longitude == 0


@pytest.mark.parametrize("active", [True, False])
async def test_explicit_activity_update_remains_authoritative(sessions, active):
    identity = {
        "provider": "marriott",
        "provider_hotel_id": "TEST1",
        "hotel_name": "Offline",
        "official_url": "https://www.marriott.com/",
    }
    async with sessions() as db, db.begin():
        await upsert_hotel(db, HotelData(**identity, active=not active))
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, HotelData(**identity, active=active))
        assert hotel.active is active


async def test_paused_global_cursor_resumes_saved_date_after_pause_expires(sessions):
    settings = Settings(
        global_jobs_per_tick=1,
        provider_overrides={name: ProviderPolicy(enabled=True) for name in ("marriott", "gha")},
    )
    async with sessions() as db, db.begin():
        db.add(
            Hotel(
                id="a-paused",
                provider="marriott",
                provider_hotel_id="TEST1",
                hotel_name="Offline paused",
                official_url="https://www.marriott.com/",
            )
        )
        db.add(
            Hotel(
                id="z-healthy",
                provider="gha",
                provider_hotel_id="10624",
                hotel_name="Offline healthy",
                official_url="https://www.ghadiscovery.com/",
            )
        )
        db.add(ProviderStatus(provider="marriott", blocked_until=utcnow() + timedelta(hours=1)))
        db.add(
            AppSetting(
                key="global_date_cursor:a-paused",
                value={"next_check_in": (date.today() + timedelta(days=100)).isoformat(), "offset": 100},
            )
        )
    async with sessions() as db, db.begin():
        assert await expand_global(db, settings) == 1
        assert (await db.get(AppSetting, "global_date_cursor:a-paused")).value["offset"] == 100
        (await db.get(ProviderStatus, "marriott")).blocked_until = utcnow() - timedelta(seconds=1)
    async with sessions() as db, db.begin():
        assert await expand_global(db, settings) == 1
        job = await db.scalar(select(CrawlJob).where(CrawlJob.hotel_id == "a-paused"))
        assert job.check_in == date.today() + timedelta(days=100)
        assert (await db.get(AppSetting, "global_date_cursor:a-paused")).value["offset"] == 101


async def test_all_groups_paused_preserves_rotation_and_dates_without_fake_work(sessions):
    settings = Settings(provider_overrides={"marriott": ProviderPolicy(enabled=True)})
    async with sessions() as db, db.begin():
        db.add(
            Hotel(
                id="only",
                provider="marriott",
                provider_hotel_id="TEST1",
                hotel_name="Offline",
                official_url="https://www.marriott.com/",
            )
        )
        db.add(ProviderStatus(provider="marriott", blocked_until=utcnow() + timedelta(hours=1)))
        db.add(AppSetting(key="global_hotel_cursor", value={"hotel_id": "only"}))
        db.add(AppSetting(key="global_date_cursor:only", value={"offset": 123}))
    async with sessions() as db, db.begin():
        assert await expand_global(db, settings) == 0
        assert (await db.get(AppSetting, "global_hotel_cursor")).value == {"hotel_id": "only"}
        assert (await db.get(AppSetting, "global_date_cursor:only")).value == {"offset": 123}
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0


async def test_real_worker_catalog_refresh_retains_metadata_and_existing_price_history(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("IHG_ENABLED", "true")
    identity = {
        "provider": "ihg",
        "provider_hotel_id": "HSVPP",
        "hotel_name": "Offline IHG",
        "official_url": "https://www.ihg.com/holidayinnexpress/hotels/us/en/huntsville/hsvpp/hoteldetail",
    }
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db, HotelData(**identity, country="United States", city="Huntsville", default_currency="USD")
        )
        hotel_id = hotel.id
        quote = fixture_rate().model_copy(update={"provider": ProviderName.IHG, "provider_hotel_id": "HSVPP"})
        await persist_rates(db, hotel, [quote], "offline-existing-quote")
        db.add(
            Watchlist(
                name="Offline country",
                scope="country",
                filters={"country": "United States", "days_ahead": 365},
            )
        )
        job = await enqueue(
            db, JobInput(provider="ihg", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ROOT})
        )
        job_id = job.id
    result = CatalogPageData(
        provider="ihg", source_url=ROOT, complete=False, page_complete=True, hotels=[HotelData(**identity)]
    )
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=result))
    await queue.put(job_id, 70)
    assert await worker.process_one(sessions, queue, Settings())
    async with sessions() as db, db.begin():
        assert (await db.get(CrawlJob, job_id)).status == "SUCCEEDED"
        hotel = await db.get(Hotel, hotel_id)
        assert (hotel.country, hotel.city, hotel.default_currency) == ("United States", "Huntsville", "USD")
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 1
        assert await db.scalar(select(Rate.total_price)) == quote.total_price
        assert await expand_watchlists(db, Settings()) == 1
