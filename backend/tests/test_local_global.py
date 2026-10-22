import asyncio
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.crawler.worker import process_one
from app.local_global import (
    BRANDS,
    CONTROL,
    PAUSES,
    GlobalConfig,
    control,
    dispatch,
    initialize,
    interruptible,
    load_config,
    official_hotel,
    pin_denials,
    read_catalog,
    report,
    set_control,
)
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, RateData
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel
from app.services.sqlite_queue import SQLiteQueue

URLS = {
    "marriott": "https://www.marriott.com.cn/hotels/xiybp-four-points-xian-bell-tower/overview/",
    "ihg": "https://www.ihg.com/holidayinn/hotels/us/en/test/testa/hoteldetail",
    "hilton": "https://www.hilton.com/en/hotels/ausaqaq-austin-downtown/",
    "hyatt": "https://www.hyatt.com/en-US/hotel/united-states/test/nycph",
    "gha": "https://www.ghadiscovery.com/anantara/anantara-angkor-resort",
    "accor": "https://all.accor.com/hotel/0339/index.en.shtml",
}


@pytest_asyncio.fixture
async def sessions(tmp_path):
    # This runner is SQLite-only, including when the wider suite uses PostgreSQL.
    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "local.sqlite").as_posix())
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def config(tmp_path):
    return GlobalConfig(database=tmp_path / "data.sqlite", queue=tmp_path / "queue.sqlite")


async def add_hotels(sessions, brands=BRANDS):
    async with sessions() as db, db.begin():
        for brand in brands:
            await upsert_hotel(
                db,
                HotelData(
                    provider=brand,
                    provider_hotel_id=brand + "-test",
                    hotel_name=brand + " Hotel",
                    official_url=URLS[brand],
                    country="TEST",
                ),
            )


def test_configuration_enables_all_six_with_single_brand_concurrency(tmp_path):
    settings = config(tmp_path).settings()
    assert settings.global_monitoring_enabled and settings.max_pending_jobs == 120
    assert settings.alert_drop_fraction == Decimal("0.35")
    assert settings.alert_min_history_days == 5
    assert all(settings.provider_policy(b).enabled for b in BRANDS)
    assert all(settings.provider_policy(b).concurrency == 1 for b in BRANDS)
    assert not settings.telegram_bot_token.get_secret_value()


@pytest.mark.parametrize(
    "change",
    [
        {"brands": ()},
        {"brands": ("fake",)},
        {"brands": ("ihg", "ihg")},
        {"min_delay_seconds": float("nan")},
        {"min_delay_seconds": True},
        {"max_pending_jobs": 5},
        {"chrome_port": 0},
        {"alert_drop_fraction": 1},
    ],
)
def test_invalid_config(change, tmp_path):
    with pytest.raises(ValueError):
        replace(config(tmp_path), **change)


def test_report_does_not_create_missing_files(tmp_path):
    result = report(config(tmp_path))
    assert result["scope"] == "WORLDWIDE" and result["brands"] == list(BRANDS)
    assert not list(tmp_path.iterdir())


def test_import_rejects_non_official_source():
    with pytest.raises(ValueError):
        official_hotel(
            {
                "provider": "ihg",
                "provider_hotel_id": "TESTA",
                "hotel_name": "Test",
                "official_url": "https://www.ihg.com.example.org/hotel",
            }
        )


async def test_six_brands_fair_bounded_dispatch_and_real_persistence(sessions, tmp_path, monkeypatch):
    settings = replace(config(tmp_path), max_pending_jobs=20).settings()
    await add_hotels(sessions)
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    calls = []

    class Provider:
        async def search_rates(self, request):
            calls.append(self.brand)
            return [
                RateData(
                    **request.model_dump(),
                    provider=self.brand,
                    room_type="King",
                    room_code="K",
                    rate_name="Public flexible",
                    rate_code="FLEX",
                    cash_price="100",
                    total_price="120",
                    currency="USD",
                    member_rate=False,
                    availability=True,
                    source_url=URLS[self.brand],
                    raw_response_hash="a" * 64,
                )
            ]

        async def close(self):
            pass

    for brand in BRANDS:

        def factory(brand=brand):
            instance = Provider()
            instance.brand = brand
            return instance

        monkeypatch.setitem(FACTORIES, brand, factory)
    try:
        for _ in BRANDS:
            await dispatch(sessions, queue, settings)
            assert await process_one(sessions, queue, settings)
        assert set(calls) == set(BRANDS) and len(calls) == 6
        async with sessions() as db:
            counts = dict(
                (
                    await db.execute(
                        select(
                            PriceHistory.provider,
                            func.count(),
                        ).group_by(PriceHistory.provider)
                    )
                ).all()
            )
            assert counts == dict.fromkeys(BRANDS, 1)
            live = await db.scalar(
                select(func.count())
                .select_from(CrawlJob)
                .where(
                    CrawlJob.status.in_(("PENDING", "QUEUED", "RUNNING")),
                )
            )
            assert live <= 20
            jobs = (await db.scalars(select(CrawlJob))).all()
            assert all(
                date.today() <= job.check_in < job.check_out <= date.today() + timedelta(days=365)
                for job in jobs
            )
    finally:
        await queue.close()


async def test_rejected_group_stays_paused_other_groups_progress(sessions, tmp_path):
    await add_hotels(sessions)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="hyatt", status="BLOCKED", last_error="RATE_LIMITED"))
    await pin_denials(sessions)
    await pin_denials(sessions)
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    try:
        await dispatch(sessions, queue, config(tmp_path).settings())
        async with sessions() as db:
            paused = await db.get(AppSetting, PAUSES)
            assert paused.value["hyatt"]["reason"] == "RATE_LIMITED"
            jobs = set(await db.scalars(select(CrawlJob.provider)))
            assert jobs == set(BRANDS) - {"hyatt"}
            assert (await db.get(ProviderStatus, "hyatt")).blocked_until.year == 2099
    finally:
        await queue.close()


async def test_stop_interrupts_browser_work_and_awaits_cleanup(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key=CONTROL, value={"stop": True}))
    cleaned = asyncio.Event()

    async def operation():
        try:
            await asyncio.sleep(100)
        finally:
            cleaned.set()

    # Give operation a chance to own resources before cancellation.
    task = asyncio.create_task(operation())
    await asyncio.sleep(0)
    assert await interruptible(sessions, task) is None
    assert cleaned.is_set()


async def test_catalog_bootstrap_preserves_disabled_hotels_and_never_imports_secrets(tmp_path):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    source = tmp_path / "source.sqlite"
    target = tmp_path / "target.sqlite"
    engines = [create_async_engine("sqlite+aiosqlite:///" + p.as_posix()) for p in (source, target)]
    try:
        for engine in engines:
            async with engine.begin() as db:
                await db.run_sync(Base.metadata.create_all)
        source_sessions, target_sessions = [async_sessionmaker(e, expire_on_commit=False) for e in engines]
        await add_hotels(source_sessions, ("ihg", "gha"))
        async with source_sessions() as db, db.begin():
            db.add(AppSetting(key="telegram", value={"bot_token": "private"}))
            db.add(AppSetting(key="catalog:gha", value={"urls": [], "cursor": 0}))
        await add_hotels(target_sessions, ("ihg",))
        async with target_sessions() as db, db.begin():
            hotel = await db.scalar(select(Hotel).where(Hotel.provider == "ihg"))
            hotel.active = False
        cfg = replace(config(tmp_path), database=target, seed_sources=(source,))
        result = await initialize(target_sessions, cfg)
        assert sum(result.values()) == 1
        assert await initialize(target_sessions, cfg) == {}
        assert len(read_catalog(source)[0]) == 2
        async with target_sessions() as db:
            assert await db.get(AppSetting, "telegram") is None
            assert await db.get(AppSetting, "catalog:gha") is not None
            assert (await db.scalar(select(Hotel).where(Hotel.provider == "ihg"))).active is False
        state = report(cfg)
        assert state["brands"]["gha"]["hotels"] == 1
        assert state["brands"]["gha"]["price_records"] == 0
        assert not state["coverage_complete"]
        await control(cfg, stop=True)
        assert report(cfg)["runtime"]["stop"] is True
    finally:
        for engine in engines:
            await engine.dispose()


def test_toml_resolves_paths(tmp_path):
    path = tmp_path / "global.toml"
    path.write_text('database="data.sqlite"\nqueue="queue.sqlite"\nprofile="profile"\n', encoding="utf-8")
    cfg = load_config(path)
    assert cfg.database == tmp_path / "data.sqlite"
    assert cfg.profile == tmp_path / "profile"


async def test_expired_worker_lease_has_a_bounded_recovery(sessions, tmp_path):
    queue = SQLiteQueue(tmp_path / "queue.sqlite")
    try:
        async with sessions() as db, db.begin():
            exhausted = await enqueue(db, JobInput(provider="gha", kind="DISCOVER_HOTELS", payload={"id": 1}))
            retry = await enqueue(db, JobInput(provider="gha", kind="DISCOVER_HOTELS", payload={"id": 2}))
            for job, count in ((exhausted, 3), (retry, 1)):
                job.status, job.retry_count = "RUNNING", count
                job.lease_owner, job.lease_until = "stopped-worker", utcnow() - timedelta(seconds=1)
        await dispatch(sessions, queue, config(tmp_path).settings())
        async with sessions() as db:
            failed = await db.get(CrawlJob, exhausted.id)
            recovered = await db.get(CrawlJob, retry.id)
            assert failed.status == "FAILED" and failed.error_type == "TIMEOUT"
            assert failed.lease_owner is None
            assert recovered.status == "QUEUED" and recovered.retry_count == 2
            assert recovered.lease_owner is None
    finally:
        await queue.close()


async def test_phase_updates_preserve_requested_stop(sessions):
    await set_control(sessions, stop=False, running=True)
    await set_control(sessions, stop=True)
    for index in range(5):
        await set_control(sessions, phase=f"phase-{index}")
    async with sessions() as db:
        state = (await db.get(AppSetting, CONTROL)).value
        assert state["stop"] is True and state["phase"] == "phase-4"
