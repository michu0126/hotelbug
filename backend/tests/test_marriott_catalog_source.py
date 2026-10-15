"""Explicit source selection survives late jobs and preserves old evidence."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.models.tables import AppSetting
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.domain import CatalogPageData, JobKind
from app.services.ihg_catalogs import (
    persist_catalog,
    record_catalog_failure,
    request_directory_root,
    selected_directory_pages,
    selected_directory_root,
)


def job(source, identity):
    return SimpleNamespace(
        id=identity, provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": source}
    )


async def finish(db, task):
    await persist_catalog(
        db,
        task,
        CatalogPageData(
            provider="marriott",
            source_url=task.payload["official_url"],
            reported_total=100,
            page_complete=True,
            complete=False,
        ),
        Settings(),
    )


async def test_old_automatic_root_finishes_after_manual_choice_without_switching_back(sessions):
    async with sessions() as db, db.begin():
        manual = job(CN_ROOT, "manual-cn")
        await request_directory_root(db, manual)
        await finish(db, manual)
        await finish(db, job(EN_ROOT, "older-auto-en"))
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert selected_directory_root(state, "marriott") == CN_ROOT
        assert EN_ROOT in state["pages"]  # Retain historical evidence.
        assert set(selected_directory_pages(state, "marriott")) == {CN_ROOT}


@pytest.mark.parametrize("order", [("first", "last"), ("last", "first")])
async def test_last_submitted_manual_choice_wins_independent_of_completion_order(sessions, order):
    async with sessions() as db, db.begin():
        tasks = {"first": job(CN_ROOT, "manual-first"), "last": job(EN_ROOT, "manual-last")}
        await request_directory_root(db, tasks["first"])
        await request_directory_root(db, tasks["last"])
        for key in order:
            await finish(db, tasks[key])
        assert (
            selected_directory_root((await db.get(AppSetting, "catalog:marriott")).value, "marriott")
            == EN_ROOT
        )


async def test_failed_requested_source_does_not_switch_or_pollute_active_error(sessions):
    async with sessions() as db, db.begin():
        await finish(db, job(EN_ROOT, "auto-en"))
        manual = job(CN_ROOT, "manual-cn")
        await request_directory_root(db, manual)
        await record_catalog_failure(db, manual, ProviderError(ErrorCode.TIMEOUT, "timed out"))
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert selected_directory_root(state, "marriott") == EN_ROOT
        assert state["last_error"] == "DIRECTORY_PARTIAL"
        assert state["pages"][CN_ROOT]["last_error"] == "TIMEOUT"


async def test_legacy_job_without_admin_selection_cannot_change_default_source(sessions):
    async with sessions() as db, db.begin():
        await finish(db, job(CN_ROOT, "legacy-cn"))
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert selected_directory_root(state, "marriott") == EN_ROOT


async def test_old_source_failure_does_not_replace_current_source_error(sessions):
    async with sessions() as db, db.begin():
        task = job(CN_ROOT, "cn")
        await request_directory_root(db, task)
        await finish(db, task)
        await record_catalog_failure(db, job(EN_ROOT, "old-en"), ProviderError(ErrorCode.TIMEOUT, "timeout"))
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert state["last_error"] == "DIRECTORY_PARTIAL"
        assert len(selected_directory_pages(state, "marriott")) == 1


async def test_api_reports_selected_source_counts_only(sessions):
    import httpx

    from app.api.main import app
    from app.database.session import get_session

    async with sessions() as db, db.begin():
        task = job(CN_ROOT, "cn")
        await request_directory_root(db, task)
        await finish(db, task)
        await record_catalog_failure(db, job(EN_ROOT, "old-en"), ProviderError(ErrorCode.TIMEOUT, "timeout"))

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            result = await client.get("/api/catalogs")
            row = next(r for r in result.json() if r["provider"] == "marriott")
            assert row["directory_pages"] == row["parsed_directory_pages"] == 1
            assert row["failed_directory_pages"] == 0
            assert row["last_error"] == "DIRECTORY_PARTIAL"
            assert row["catalog_coverage_complete"] is False
    finally:
        app.dependency_overrides.clear()
