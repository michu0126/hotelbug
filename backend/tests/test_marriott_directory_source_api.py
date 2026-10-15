"""Public source status and explicit authenticated source requests stay separate."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api.main import app, redis
from app.api.main import settings as api_settings
from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.domain import CatalogPageData
from app.services.ihg_catalogs import persist_catalog, record_catalog_failure
from app.services.runtime_settings import MonitoringInput, save_runtime_settings

HEADERS = {"authorization": "Bearer offline-source-admin"}


@pytest_asyncio.fixture
async def source_client(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("offline-source-admin"))
    async with sessions() as db, db.begin():
        await save_runtime_settings(db, MonitoringInput(providers={"marriott": {"enabled": True}}))

    async def dependency():
        async with sessions() as db:
            yield db

    previous = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client
    finally:
        if previous is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = previous


async def source(client):
    response = await client.get("/api/catalogs")
    assert response.status_code == 200
    return next(item for item in response.json() if item["provider"] == "marriott")


async def request(client, root, *, headers=HEADERS):
    return await client.post(
        "/api/jobs",
        headers=headers,
        json={
            "provider": "marriott",
            "kind": "DISCOVER_CATALOG",
            "priority": 70,
            "payload": {"official_url": root},
        },
    )


async def succeed(db, identity):
    job = await db.get(CrawlJob, identity)
    await persist_catalog(
        db,
        job,
        CatalogPageData(
            provider="marriott",
            source_url=job.payload["official_url"],
            reported_total=100,
            complete=False,
            page_complete=True,
        ),
        Settings(),
    )
    job.status = "SUCCEEDED"


async def test_default_source_and_choices_come_from_official_provider_constants(source_client):
    item = await source(source_client)
    assert item["selected_directory_root"] == EN_ROOT
    assert item["requested_directory_root"] is None
    assert item["requested_directory_root_status"] is None
    assert item["directory_root_choices"] == [
        {"label": "中文官网（全球目录）", "url": CN_ROOT},
        {"label": "英文官网（全球目录）", "url": EN_ROOT},
    ]
    assert item["hotels"] == item["hotels_with_quotes"] == 0
    assert item["price_coverage"]["required_stays"] == 0
    assert item["price_coverage"]["collected_fresh_coverage_complete"] is False


async def test_source_request_requires_existing_management_auth(source_client, sessions):
    response = await request(source_client, CN_ROOT, headers={})
    assert response.status_code == 401
    assert (await source(source_client))["requested_directory_root"] is None
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 0


async def test_disabled_provider_request_does_not_enable_or_change_source(source_client, sessions):
    async with sessions() as db, db.begin():
        await save_runtime_settings(db, MonitoringInput(providers={"marriott": {"enabled": False}}))
    response = await request(source_client, CN_ROOT)
    assert response.status_code == 409
    item = await source(source_client)
    assert item["enabled"] is False
    assert item["selected_directory_root"] == EN_ROOT and item["requested_directory_root"] is None


async def test_repeated_pending_source_request_reuses_live_task(source_client, sessions):
    first = await request(source_client, CN_ROOT)
    duplicate = await request(source_client, CN_ROOT)
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["id"] == duplicate.json()["id"]
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1
    assert (await source(source_client))["requested_directory_root_status"] == "PENDING"


async def test_request_is_pending_until_success_and_preserves_existing_records(source_client, sessions):
    pause = utcnow() + timedelta(hours=6)
    async with sessions() as db, db.begin():
        db.add(
            Hotel(
                id="offline-hotel",
                provider="marriott",
                provider_hotel_id="OFF01",
                hotel_name="Offline preserved hotel",
                official_url=EN_ROOT,
            )
        )
        await db.flush()
        db.add(
            PriceHistory(
                id="offline-price",
                hotel_id="offline-hotel",
                provider="marriott",
                offer_key="a" * 64,
                check_in=date.today(),
                check_out=date.today() + timedelta(days=1),
                currency="USD",
                cash_price=100,
                availability=True,
                adults=2,
                rooms=1,
                price_basis="PER_NIGHT",
                captured_at=utcnow(),
                source_url=EN_ROOT,
                raw_response_hash="b" * 64,
                job_id="offline-history-job",
            )
        )
        db.add(
            CrawlJob(
                id="existing-job",
                provider="marriott",
                kind="DISCOVER_HOTELS",
                status="QUEUED",
                dedupe_key="existing-job",
                payload={"provider_hotel_id": "OFF01"},
            )
        )
        db.add(ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=pause))
    response = await request(source_client, CN_ROOT)
    assert response.status_code == 200
    identity = response.json()["id"]
    item = await source(source_client)
    assert item["selected_directory_root"] == EN_ROOT
    assert item["requested_directory_root"] == CN_ROOT
    assert item["requested_directory_root_status"] == "PENDING"
    assert item["hotels"] == item["hotels_with_quotes"] == 1
    assert item["price_coverage"]["eligible_hotels"] == 1
    assert item["price_coverage"]["required_stays"] == 365
    assert item["price_coverage"]["fresh_stays"] == 0  # History without a stay observation is not coverage.
    async with sessions() as db, db.begin():
        assert (await db.get(CrawlJob, "existing-job")).status == "QUEUED"
        assert (await db.get(Hotel, "offline-hotel")).hotel_name == "Offline preserved hotel"
        assert (await db.get(PriceHistory, "offline-price")).cash_price == 100
        assert (await db.get(ProviderStatus, "marriott")).blocked_until.replace(tzinfo=pause.tzinfo) == pause
        await succeed(db, identity)
    adopted = await source(source_client)
    assert adopted["selected_directory_root"] == adopted["requested_directory_root"] == CN_ROOT
    assert adopted["requested_directory_root_status"] == "SUCCEEDED"
    assert adopted["price_coverage"]["eligible_hotels"] == 0  # Existing hotel is still on the old source.


async def test_failed_source_request_does_not_replace_current_source(source_client, sessions):
    response = await request(source_client, CN_ROOT)
    async with sessions() as db, db.begin():
        job = await db.get(CrawlJob, response.json()["id"])
        job.status, job.error_type = "FAILED", "TIMEOUT"
        await record_catalog_failure(db, job, ProviderError(ErrorCode.TIMEOUT, "Offline timeout"))
    item = await source(source_client)
    assert item["selected_directory_root"] == EN_ROOT
    assert item["requested_directory_root"] == CN_ROOT
    assert item["requested_directory_root_status"] == "FAILED"
    assert item["requested_directory_root_error"] == "TIMEOUT"


async def test_newer_source_request_wins_without_resetting_older_jobs(source_client, sessions):
    first = await request(source_client, CN_ROOT)
    last = await request(source_client, EN_ROOT)
    async with sessions() as db, db.begin():
        await succeed(db, first.json()["id"])
    item = await source(source_client)
    assert item["selected_directory_root"] == item["requested_directory_root"] == EN_ROOT
    assert item["requested_directory_root_status"] == "PENDING"
    async with sessions() as db:
        assert (await db.get(CrawlJob, first.json()["id"])).status == "SUCCEEDED"
        assert (await db.get(CrawlJob, last.json()["id"])).status == "PENDING"


async def test_invalid_legacy_request_is_not_exposed_as_a_source_option(source_client, sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="catalog:marriott", value={"requested_root": "https://example.com/"}))
    item = await source(source_client)
    assert item["selected_directory_root"] == EN_ROOT
    assert item["requested_directory_root"] is None


async def test_dashboard_separates_local_quote_sample_from_current_database(source_client, monkeypatch):
    monkeypatch.setattr(redis, "get", AsyncMock(return_value=None))
    response = await source_client.get("/api/dashboard")
    assert response.status_code == 200
    item = response.json()
    assert "同一晚 7 个房型" in item["message"]
    assert "真实 Worker 任务已入库 16 条" in item["message"]
    assert "不代表当前部署或群晖已采到报价" in item["message"]
    assert item["rates_today"] == item["hotels"] == 0
