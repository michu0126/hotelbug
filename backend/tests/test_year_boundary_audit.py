"""Offline fixtures only; no website requests or fabricated live audit evidence."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from audit_ihg_price import run_existing_price
from sqlalchemy import func, select
from test_catalog_audit_price import seed
from year_boundary_audit import prepare_year_boundary

from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, ProviderStatus, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue


async def snapshot(sessions):
    async with sessions() as db:
        return {
            row.key: row.value
            for row in (await db.scalars(select(AppSetting).where(AppSetting.key.like("global%")))).all()
        }


@pytest.mark.parametrize("provider", ["ihg", "gha"])
async def test_boundary_keeps_source_and_global_progress(sessions, provider):
    settings = await seed(sessions, provider)
    before = await snapshot(sessions)
    async with sessions() as db:
        source = await db.scalar(select(CrawlJob))
        source_id, source_date, deadline = source.id, source.check_in, source.scheduled_at
    result = await prepare_year_boundary(sessions, settings, provider)
    assert result["outcome"] == "CREATED_BOUNDARY_JOB"
    async with sessions() as db:
        source = await db.get(CrawlJob, source_id)
        target = await db.get(CrawlJob, result["job_id"])
        assert (source.status, source.check_in, source.scheduled_at) == ("PENDING", source_date, deadline)
        assert target.hotel_id == source.hotel_id
        assert target.check_in == date.today() + timedelta(days=364)
        assert target.check_out == date.today() + timedelta(days=365)
        assert target.payload == {"adults": 2, "rooms": 1, "audit_year_boundary": True}
    assert await snapshot(sessions) == before


async def test_boundary_resume_never_shortens_retry(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    first = await prepare_year_boundary(sessions, settings, "ihg")
    async with sessions() as db, db.begin():
        target = await db.get(CrawlJob, first["job_id"])
        target.scheduled_at = utcnow() + timedelta(hours=1)
        target.retry_count, target.error_type = 1, "TIMEOUT"
        deadline = target.scheduled_at
    second = await prepare_year_boundary(sessions, settings, "ihg")
    assert second == {"outcome": "EXISTING_BOUNDARY_JOB", "job_id": first["job_id"]}
    execute = AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    assert (
        await run_existing_price(
            sessions, queue, settings, target_job_id=first["job_id"], emit=lambda _: None
        )
    )["outcome"] == "NO_PENDING_PRICE_DUE"
    execute.assert_not_awaited()
    async with sessions() as db:
        target = await db.get(CrawlJob, first["job_id"])
        assert target.scheduled_at == deadline.replace(tzinfo=None) and target.retry_count == 1


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED", "CANCELLED", "RUNNING"])
async def test_boundary_terminal_or_active_attempt_is_not_recreated(sessions, status):
    settings = await seed(sessions)
    first = await prepare_year_boundary(sessions, settings, "ihg")
    async with sessions() as db, db.begin():
        target = await db.get(CrawlJob, first["job_id"])
        target.status = status
    assert await prepare_year_boundary(sessions, settings, "ihg") == {
        "outcome": "BOUNDARY_ALREADY_ATTEMPTED",
        "job_status": status,
    }
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 2


async def test_boundary_honors_pause_and_disabled_group(sessions):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=6)))
    assert (await prepare_year_boundary(sessions, settings, "ihg"))["outcome"] == "PROVIDER_PAUSED"
    settings.provider_overrides["ihg"].enabled = False
    assert (await prepare_year_boundary(sessions, settings, "ihg"))["outcome"] == "PROVIDER_DISABLED"
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1


async def test_boundary_requires_isolated_marker_and_global_cursor(sessions):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        cursor = await db.scalar(select(AppSetting).where(AppSetting.key.like("global_date_cursor:%")))
        await db.delete(cursor)
    assert (await prepare_year_boundary(sessions, settings, "ihg"))["outcome"] == "NO_DUE_GLOBAL_SOURCE"
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, "audit:directory-only")
        marker.value = {"provider": "hyatt"}
    with pytest.raises(AssertionError, match="isolated"):
        await prepare_year_boundary(sessions, settings, "ihg")


async def test_boundary_respects_capacity(sessions):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        source = await db.scalar(select(CrawlJob))
        for offset in range(1, 20):
            await enqueue(
                db,
                JobInput(
                    provider="ihg",
                    kind=JobKind.FETCH_RATE,
                    hotel_id=source.hotel_id,
                    check_in=date.today() + timedelta(days=offset),
                    check_out=date.today() + timedelta(days=offset + 1),
                ),
            )
    settings.max_pending_jobs = 20
    assert (await prepare_year_boundary(sessions, settings, "ihg"))["outcome"] == "QUEUE_AT_CAPACITY"


@pytest.mark.parametrize("status", ["PENDING", "FAILED", "SUCCEEDED"])
async def test_boundary_never_duplicates_previous_same_condition(sessions, status):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        source = await db.scalar(select(CrawlJob))
        previous = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_CALENDAR,
                hotel_id=source.hotel_id,
                check_in=date.today() + timedelta(days=364),
                check_out=date.today() + timedelta(days=365),
            ),
        )
        previous.status = status
    assert (await prepare_year_boundary(sessions, settings, "ihg"))[
        "outcome"
    ] == "BOUNDARY_CONDITION_ALREADY_RECORDED"


async def test_official_closed_window_has_no_fake_quote_and_is_not_repeated(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    first = await prepare_year_boundary(sessions, settings, "ihg")
    execute = AsyncMock(
        side_effect=ProviderError(ErrorCode.BOOKING_WINDOW_CLOSED, "Offline official-window fixture")
    )
    monkeypatch.setattr(worker, "execute", execute)
    result = await run_existing_price(
        sessions, queue, settings, target_job_id=first["job_id"], emit=lambda _: None
    )
    assert result["outcome"] == "SUCCEEDED" and result["stay_scan_status"] == "NOT_OPEN"
    assert result["history_rows_persisted"] == 0 and result["offers"] == []
    assert (await prepare_year_boundary(sessions, settings, "ihg"))["outcome"] == "BOUNDARY_ALREADY_ATTEMPTED"
    assert execute.await_count == 1
