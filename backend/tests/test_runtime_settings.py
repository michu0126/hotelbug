from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select
from test_pipeline import FixtureProvider

from app.api.main import app
from app.api.main import settings as api_settings
from app.core.config import Settings
from app.crawler.worker import process_one
from app.database.session import get_session
from app.models.tables import CrawlJob
from app.providers import registry
from app.scheduler.main import tick
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel
from app.services.runtime_settings import (
    MonitoringInput,
    RuntimeHttpClients,
    load_runtime_settings,
    public_monitoring_settings,
    save_runtime_settings,
)


async def test_database_settings_override_env_without_mutating_startup(sessions, monkeypatch):
    monkeypatch.setenv("IHG_ENABLED", "true")
    base = Settings(browser_proxy_url="http://old:7890")
    async with sessions() as db, db.begin():
        await save_runtime_settings(
            db,
            MonitoringInput(
                providers={"ihg": {"enabled": False}, "gha": {"enabled": True}},
                browser_proxy_url="http://user:private-value@proxy:7890",
                alert_drop_fraction="0.4",
            ),
        )
    async with sessions() as db:
        runtime = await load_runtime_settings(db, base)
    assert not runtime.provider_policy("ihg").enabled
    assert runtime.provider_policy("gha").enabled
    assert runtime.alert_drop_fraction == Decimal("0.4")
    assert base.provider_policy("ihg").enabled and not base.provider_policy("gha").enabled
    assert base.browser_proxy_url.get_secret_value() == "http://old:7890"
    view = public_monitoring_settings(runtime, set(registry.FACTORIES))
    assert view["browser_proxy_display"] == "http://proxy:7890"
    assert "private-value" not in str(view) and "user" not in view["browser_proxy_display"]
    assert view["monitoring_days"] == 365 and view["timezone"] == "Asia/Shanghai"


async def test_partial_save_preserves_other_groups_and_proxy_but_empty_clears(sessions):
    async with sessions() as db, db.begin():
        await save_runtime_settings(
            db,
            MonitoringInput(
                providers={"gha": {"enabled": True}, "ihg": {"enabled": False}},
                browser_proxy_url="http://proxy:7890",
            ),
        )
    async with sessions() as db, db.begin():
        await save_runtime_settings(
            db, MonitoringInput(providers={"ihg": {"enabled": True}}, browser_proxy_url=None)
        )
    async with sessions() as db, db.begin():
        runtime = await load_runtime_settings(db, Settings())
        assert runtime.provider_policy("gha").enabled and runtime.provider_policy("ihg").enabled
        assert runtime.browser_proxy_url.get_secret_value() == "http://proxy:7890"
        await save_runtime_settings(db, MonitoringInput(browser_proxy_url=""))
    async with sessions() as db:
        assert not (
            await load_runtime_settings(db, Settings(browser_proxy_url="http://env:7890"))
        ).browser_proxy_url.get_secret_value()


@pytest.mark.parametrize(
    "value",
    [
        "192.168.50.4:7890",
        "file:///tmp/proxy",
        "http://proxy",
        "http://proxy:0",
        "http://proxy:99999",
        "http://proxy:7890/path",
        "http://proxy:7890?token=x",
    ],
)
def test_invalid_proxy_is_rejected(value):
    with pytest.raises(ValidationError):
        MonitoringInput(browser_proxy_url=value)


@pytest.mark.parametrize(
    "value",
    [
        {"providers": {"gha": {"enabled": True, "concurrency": 5}}},
        {"providers": {"gha": {"interval_seconds": 5}}},
        {"providers": {"unknown": {"enabled": True}}},
        {"monitoring_days": 999},
        {"alert_drop_fraction": "1"},
        {"alert_min_history_days": 1},
    ],
)
def test_invalid_policy_and_expanded_horizon_are_rejected(value):
    with pytest.raises(ValidationError):
        MonitoringInput.model_validate(value)


async def test_saved_enable_reaches_real_scheduler_worker_factory(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "false")
    seen = []

    def factory():
        seen.append(registry.provider_settings())
        return FixtureProvider()

    monkeypatch.setitem(registry.FACTORIES, "marriott", factory)
    base = Settings(global_monitoring_enabled=False)
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, await FixtureProvider().get_hotel_details("fixture-only"))
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in="2027-01-07",
                check_out="2027-01-08",
                priority=99,
            ),
        )
        job_id = job.id
        await save_runtime_settings(
            db,
            MonitoringInput(
                providers={"marriott": {"enabled": True}},
                browser_proxy_url="http://saved:7890",
                alert_drop_fraction="0.4",
            ),
        )
    await tick(sessions, queue, base)
    assert await process_one(sessions, queue, base)
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "SUCCEEDED"
    assert seen[0].browser_proxy_url.get_secret_value() == "http://saved:7890"
    assert seen[0].alert_drop_fraction == Decimal("0.4")
    assert not base.provider_policy("marriott").enabled
    assert registry.provider_settings() is registry.get_settings()


async def test_disable_after_dispatch_prevents_browser_execution(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    factory = MagicMock(side_effect=AssertionError("A disabled group must not start a browser"))
    monkeypatch.setitem(registry.FACTORIES, "marriott", factory)
    base = Settings(global_monitoring_enabled=False)
    async with sessions() as db, db.begin():
        job = await enqueue(db, JobInput(provider="marriott", kind=JobKind.PROVIDER_HEALTHCHECK))
        job_id = job.id
    await tick(sessions, queue, base)
    async with sessions() as db, db.begin():
        await save_runtime_settings(db, MonitoringInput(providers={"marriott": {"enabled": False}}))
    assert await process_one(sessions, queue, base)
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "CANCELLED"
    factory.assert_not_called()


async def test_saved_global_switch_controls_generation_without_restart(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "false")
    base = Settings(global_jobs_per_tick=1)
    async with sessions() as db, db.begin():
        await upsert_hotel(db, await FixtureProvider().get_hotel_details("fixture-only"))
        await save_runtime_settings(
            db, MonitoringInput(providers={"marriott": {"enabled": True}}, global_monitoring_enabled=False)
        )
    await tick(sessions, queue, base)
    async with sessions() as db, db.begin():
        assert (
            await db.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE)
            )
            == 0
        )
        await save_runtime_settings(db, MonitoringInput(global_monitoring_enabled=True))
    await tick(sessions, queue, base)
    async with sessions() as db:
        assert (
            await db.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.kind == JobKind.FETCH_RATE)
            )
            == 1
        )


async def test_settings_endpoints_require_admin_and_use_saved_enable(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("fixture-admin"))
    monkeypatch.setenv("MARRIOTT_ENABLED", "false")

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/settings/monitoring")).status_code == 401
            assert (await client.put("/api/settings/monitoring", json={})).status_code == 401
            headers = {"authorization": "Bearer fixture-admin"}
            response = await client.put(
                "/api/settings/monitoring",
                headers=headers,
                json={
                    "providers": {"marriott": {"enabled": True}},
                    "browser_proxy_url": "http://name:private-value@proxy:7890",
                },
            )
            assert response.status_code == 200
            assert "private-value" not in response.text
            view = (await client.get("/api/settings/monitoring", headers=headers)).json()
            assert view["providers"]["marriott"]["enabled"]
            assert "private-value" not in str(view)
            providers = (await client.get("/api/providers")).json()
            assert next(p for p in providers if p["provider"] == "marriott")["enabled"]
            accepted = await client.post(
                "/api/jobs", headers=headers, json={"provider": "marriott", "kind": "PROVIDER_HEALTHCHECK"}
            )
            assert accepted.status_code == 200
            # Keep the unavailable-adapter rejection test independent of which
            # groups currently have implementations; do not contact a website.
            monkeypatch.delitem(registry.FACTORIES, "hyatt", raising=False)
            rejected = await client.put(
                "/api/settings/monitoring",
                headers=headers,
                json={"providers": {"hyatt": {"enabled": True}}, "browser_proxy_url": ""},
            )
            assert rejected.status_code == 409
            # Failed saves are atomic: the previous proxy must remain intact.
            saved = (await client.get("/api/settings/monitoring", headers=headers)).json()
            assert saved["browser_proxy_configured"]
            assert (
                await client.put("/api/settings/monitoring", headers=headers, json={"monitoring_days": 999})
            ).status_code == 422
    finally:
        app.dependency_overrides.clear()


async def test_proxy_clients_reuse_and_refresh_both_paths(monkeypatch):
    from app.services import runtime_settings as module

    created = []

    def factory(**options):
        client = MagicMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        created.append((options, client))
        return client

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    clients = RuntimeHttpClients()
    try:
        await clients.update(Settings(browser_proxy_url="http://first:7890"))
        assert [x[0]["proxy"] for x in created] == ["http://first:7890", "http://first:7890"]
        await clients.update(Settings(browser_proxy_url="http://first:7890"))
        assert len(created) == 2
        await clients.update(
            Settings(browser_proxy_url="http://second:7890", telegram_proxy_url="http://telegram:7890")
        )
        assert [x[0]["proxy"] for x in created[2:]] == ["http://second:7890", "http://telegram:7890"]
        for _, old in created[:2]:
            old.__aexit__.assert_awaited_once()
        await clients.update(Settings())
        assert [x[0]["proxy"] for x in created[4:]] == [None, None]
    finally:
        await clients.close()
    for _, client in created:
        client.__aexit__.assert_awaited_once()


async def test_factory_failure_restores_context_and_does_not_leak_settings(monkeypatch):
    def fail():
        assert registry.provider_settings().browser_proxy_url.get_secret_value() == "http://saved:7890"
        raise RuntimeError("fixture failure")

    monkeypatch.setitem(registry.FACTORIES, "fixture", fail)
    with pytest.raises(RuntimeError, match="fixture failure"):
        registry.create_provider("fixture", settings=Settings(browser_proxy_url="http://saved:7890"))
    assert registry.provider_settings() is registry.get_settings()


@pytest.mark.parametrize("name", ["marriott", "ihg", "gha", "accor", "hilton"])
async def test_registered_browser_factories_use_job_proxy_snapshot(name):
    runtime = Settings(browser_proxy_url="http://saved-proxy:7890", browser_channel="chrome")
    provider = registry.create_provider(name, settings=runtime)
    try:
        actual = provider._proxy_server if name == "marriott" else provider.proxy_server
        channel = provider._browser_channel if name == "marriott" else provider.browser_channel
        assert actual == "http://saved-proxy:7890" and channel == "chrome"
        assert registry.provider_settings() is registry.get_settings()
    finally:
        await provider.close()
