"""Real Worker dispatch/persistence for monthly hints with an offline provider."""

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import (
    AppSetting,
    CalendarHintHistory,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceAlert,
    PriceHistory,
    ProviderStatus,
    Rate,
    StayScan,
)
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.calendar_hints import CalendarHintBatch, CalendarHintRequest, month_window
from app.schemas.domain import JobKind, ProviderName
from app.services import calendar_hints

NOW = datetime(2030, 10, 14, 8, tzinfo=timezone.utc)
TODAY = NOW.date()
MONTH = "2030-11"
START, END = date(2030, 11, 1), date(2030, 12, 1)
SOURCE = (
    "https://www.marriott.com.cn/search/availabilityCalendar.mi"
    "?isRateCalendar=true&propertyCode=PBHMD&isSearch=true&currency="
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(worker, "utcnow", lambda: NOW)
    monkeypatch.setattr(calendar_hints, "utcnow", lambda: NOW)
    monkeypatch.setattr(worker, "month_window", lambda month: month_window(month, TODAY))
    monkeypatch.setattr(calendar_hints, "month_window", lambda month: month_window(month, TODAY))


def settings(**changes):
    return Settings(
        _env_file=None,
        provider_overrides={
            name: ProviderPolicy(enabled=name == ProviderName.MARRIOTT) for name in ProviderName
        },
        **changes,
    )


def batch(**changes):
    return CalendarHintBatch(
        **{
            "provider_hotel_id": "PBHMD",
            "start_date": START,
            "end_date": END,
            "currency": "USD",
            "source_url": SOURCE,
            "raw_response_hash": "a" * 64,
            "captured_at": NOW,
            "days": [
                {"check_in": date(2030, 11, 4), "displayed_price": "400"},
                {"check_in": date(2030, 11, 5), "displayed_price": "425"},
            ],
            **changes,
        }
    )


def provider(monkeypatch, *, result=None, error=None, method=True):
    stub = SimpleNamespace(close=AsyncMock())
    if method:
        stub.get_calendar_hints = AsyncMock(return_value=batch() if result is None else result)
        if error:
            stub.get_calendar_hints.side_effect = error
    factory = Mock(return_value=stub)
    monkeypatch.setattr(worker, "create_provider", factory)
    return stub, factory


def hotel(**changes):
    return Hotel(
        **{
            "id": "monthly-hotel",
            "provider": "marriott",
            "provider_hotel_id": "PBHMD",
            "hotel_name": "Offline monthly hotel",
            "official_url": SOURCE,
            **changes,
        }
    )


def job(**changes):
    return CrawlJob(
        **{
            "id": "monthly-job",
            "provider": "marriott",
            "hotel_id": "monthly-hotel",
            "kind": JobKind.FETCH_CALENDAR_HINTS,
            "dedupe_key": "monthly-job",
            "payload": {"month": MONTH, "automatic_catalog_root": CN_ROOT},
            "status": "QUEUED",
            "scheduled_at": NOW - timedelta(seconds=1),
            "created_at": NOW - timedelta(seconds=2),
            **changes,
        }
    )


async def seed(sessions, *, root=CN_ROOT, hotel_changes=None, job_changes=None, missing_hotel=False):
    async with sessions() as db, db.begin():
        if not missing_hotel:
            db.add(hotel(**(hotel_changes or {})))
            await db.flush()
        db.add(AppSetting(key="catalog:marriott", value={"root": root}))
        changes = {"hotel_id": None} if missing_hotel else {}
        db.add(job(**{**changes, **(job_changes or {})}))


async def run(sessions, queue, config=None):
    await queue.put("monthly-job", 75)
    assert await worker.process_one(sessions, queue, config or settings())


async def assert_no_room_or_outbox(db, *, no_hints=False):
    models = [Rate, PriceHistory, StayScan, NotificationLog]
    if no_hints:
        models += [CalendarHintHistory, PriceAlert]
    for model in models:
        assert await db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


async def test_execute_passes_one_natural_month_scope_and_closes_provider(monkeypatch):
    stub, factory = provider(monkeypatch)
    config = settings()
    result = await worker.execute(job(), hotel(), config)
    assert isinstance(result, CalendarHintBatch)
    stub.get_calendar_hints.assert_awaited_once_with(
        CalendarHintRequest(provider_hotel_id="PBHMD", start_date=START, end_date=END)
    )
    factory.assert_called_once_with("marriott", "Offline monthly hotel", SOURCE, settings=config)
    stub.close.assert_awaited_once()


@pytest.mark.parametrize("month", ["2030-10", "2031-10"])
async def test_execute_clips_first_and_last_month_to_year_window(monkeypatch, month):
    stub, _ = provider(monkeypatch)
    await worker.execute(job(payload={"month": month}), hotel(), settings())
    request = stub.get_calendar_hints.await_args.args[0]
    assert (request.start_date, request.end_date) == month_window(month, TODAY)
    assert TODAY <= request.start_date < request.end_date <= TODAY + timedelta(days=365)
    stub.close.assert_awaited_once()


async def test_execute_uses_supplied_request_snapshot_without_reading_clock_again(monkeypatch):
    stub, _ = provider(monkeypatch)
    get_window = Mock(side_effect=AssertionError("A frozen request must not move with the clock"))
    monkeypatch.setattr(worker, "month_window", get_window)
    snapshot = month_window("2031-10", TODAY)
    await worker.execute(job(payload={"month": "2031-10"}), hotel(), settings(), request_window=snapshot)
    request = stub.get_calendar_hints.await_args.args[0]
    assert (request.start_date, request.end_date) == snapshot
    get_window.assert_not_called()
    stub.close.assert_awaited_once()


@pytest.mark.parametrize("case", ["no_method", "expired", "missing_hotel", "provider_error"])
async def test_execute_failure_closes_provider_without_rate_fallback(monkeypatch, case):
    error = ProviderError(ErrorCode.HTTP_ERROR, "Offline HTTP error") if case == "provider_error" else None
    stub, _ = provider(monkeypatch, method=case != "no_method", error=error)
    item = None if case == "missing_hotel" else hotel()
    task = job(payload={"month": "2030-09"}) if case == "expired" else job()
    with pytest.raises(ProviderError):
        await worker.execute(task, item, settings())
    stub.close.assert_awaited_once()


async def test_process_persists_only_hint_history_and_month_success_not_verified_health(
    sessions, queue, monkeypatch
):
    stub, _ = provider(monkeypatch)
    await seed(sessions)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="marriott", status="BROKEN", last_error="Previous room issue"))
    await run(sessions, queue)
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "SUCCEEDED"
        assert task.error_type is None and task.lease_owner is None and task.lease_until is None
        hints = list(await db.scalars(select(CalendarHintHistory).order_by(CalendarHintHistory.check_in)))
        assert len(hints) == 2
        assert {hint.job_id for hint in hints} == {task.id}
        assert {hint.basis for hint in hints} == {"CALENDAR_MINIMUM_TAX_UNCONFIRMED"}
        assert [str(hint.displayed_price) for hint in hints] == ["400.0000", "425.0000"]
        progress = await db.get(AppSetting, "calendar_hint_success:monthly-hotel:" + MONTH)
        assert progress.value == {"completed_at": NOW.isoformat(), "source_root": CN_ROOT}
        state = await db.get(ProviderStatus, "marriott")
        assert state.status == "DEGRADED" and state.last_success is None
        assert state.last_error == "Previous room issue"
        assert state.success_count == 1 and state.requests_today == 1
        await assert_no_room_or_outbox(db)
    stub.close.assert_awaited_once()
    assert not await queue.redis.keys("hotelbug:slot:*")


@pytest.mark.parametrize(
    "mismatch", ["list", "hotel", "provider", "late_start", "short_end", "long_end", "different_month"]
)
async def test_process_rejects_wrong_scope_without_persisting_success(sessions, queue, monkeypatch, mismatch):
    result = batch()
    if mismatch == "list":
        result = []
    elif mismatch == "hotel":
        result = batch(provider_hotel_id="SINNV", source_url=SOURCE.replace("PBHMD", "SINNV"))
    elif mismatch == "provider":
        result = result.model_copy(update={"provider": "ihg"})
    elif mismatch == "late_start":
        result = batch(start_date=START + timedelta(days=1))
    elif mismatch == "short_end":
        result = batch(end_date=END - timedelta(days=1))
    elif mismatch == "long_end":
        result = result.model_copy(update={"end_date": END + timedelta(days=1)})
    else:
        result = batch(
            start_date=date(2030, 10, 14),
            end_date=date(2030, 11, 1),
            days=[{"check_in": TODAY, "displayed_price": "400"}],
        )
    stub, _ = provider(monkeypatch, result=result)
    await seed(sessions)
    await run(sessions, queue)
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "FAILED" and task.error_type == ErrorCode.INVALID_RESPONSE
        assert await db.get(AppSetting, "calendar_hint_success:monthly-hotel:" + MONTH) is None
        await assert_no_room_or_outbox(db, no_hints=True)
    stub.close.assert_awaited_once()


async def test_current_month_response_cannot_start_before_original_request(sessions, queue, monkeypatch):
    result = batch(
        start_date=TODAY - timedelta(days=1),
        end_date=date(2030, 11, 1),
        days=[{"check_in": TODAY, "displayed_price": "100"}],
    )
    stub, _ = provider(monkeypatch, result=result)
    await seed(sessions, job_changes={"payload": {"month": "2030-10", "automatic_catalog_root": CN_ROOT}})
    await run(sessions, queue)
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "FAILED" and task.error_type == ErrorCode.INVALID_RESPONSE
        await assert_no_room_or_outbox(db, no_hints=True)
    stub.close.assert_awaited_once()


@pytest.mark.parametrize("month", ["2030-09", "2031-11", "invalid", None])
async def test_expired_or_invalid_month_cancels_before_browser_preserving_retry_evidence(
    sessions, queue, monkeypatch, month
):
    _, factory = provider(monkeypatch)
    await seed(
        sessions,
        job_changes={
            "payload": {"month": month, "automatic_catalog_root": CN_ROOT},
            "error_type": "TIMEOUT",
            "error_message": "Old attempt",
            "retry_count": 2,
        },
    )
    await run(sessions, queue)
    factory.assert_not_called()
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "CANCELLED" and task.retry_count == 2 and task.error_type == "TIMEOUT"
        assert task.error_message.startswith("Old attempt; ")
        assert await db.get(ProviderStatus, "marriott") is None
        await assert_no_room_or_outbox(db, no_hints=True)


@pytest.mark.parametrize("changed", ["source", "hotel_host", "inactive", "provider_disabled"])
async def test_changed_scope_cancels_before_browser(sessions, queue, monkeypatch, changed):
    _, factory = provider(monkeypatch)
    await seed(
        sessions,
        root=EN_ROOT if changed == "source" else CN_ROOT,
        hotel_changes={"official_url": SOURCE.replace(".com.cn", ".com")}
        if changed == "hotel_host"
        else {"active": False}
        if changed == "inactive"
        else {},
    )
    config = settings()
    if changed == "provider_disabled":
        config.provider_overrides["marriott"] = ProviderPolicy(enabled=False)
    await run(sessions, queue, config)
    factory.assert_not_called()
    async with sessions() as db:
        assert (await db.get(CrawlJob, "monthly-job")).status == "CANCELLED"
        await assert_no_room_or_outbox(db, no_hints=True)


async def test_existing_group_pause_defers_without_browser_or_mutating_failure(sessions, queue, monkeypatch):
    _, factory = provider(monkeypatch)
    await seed(sessions, job_changes={"error_type": "TIMEOUT", "retry_count": 2})
    until = NOW + timedelta(hours=4)
    async with sessions() as db, db.begin():
        db.add(
            ProviderStatus(
                provider="marriott", status="BLOCKED", blocked_until=until, last_error="RATE_LIMITED"
            )
        )
    await run(sessions, queue)
    factory.assert_not_called()
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "PENDING" and task.retry_count == 2 and task.error_type == "TIMEOUT"
        assert task.scheduled_at.replace(tzinfo=timezone.utc) == until
        state = await db.get(ProviderStatus, "marriott")
        assert state.status == "BLOCKED" and state.last_error == "RATE_LIMITED"
        await assert_no_room_or_outbox(db, no_hints=True)


async def test_inflight_success_cannot_clear_concurrent_group_pause(sessions, queue, monkeypatch):
    stub, _ = provider(monkeypatch)
    await seed(sessions)
    until = NOW + timedelta(hours=6)

    async def pause_while_fetching(_):
        async with sessions() as db, db.begin():
            db.add(
                ProviderStatus(
                    provider="marriott",
                    status="BLOCKED",
                    blocked_until=until,
                    last_error="BLOCKED_BY_ANTIBOT",
                )
            )
        return batch()

    stub.get_calendar_hints.side_effect = pause_while_fetching
    await run(sessions, queue)
    async with sessions() as db:
        assert (await db.get(CrawlJob, "monthly-job")).status == "SUCCEEDED"
        state = await db.get(ProviderStatus, "marriott")
        assert state.status == "BLOCKED" and state.last_error == "BLOCKED_BY_ANTIBOT"
        assert state.blocked_until.replace(tzinfo=timezone.utc) == until
        assert await db.scalar(select(func.count()).select_from(CalendarHintHistory)) == 2
        await assert_no_room_or_outbox(db)


@pytest.mark.parametrize(
    "changed",
    ["source", "removed_source", "inactive", "hotel_host", "hotel_code", "hotel_provider", "invalid_marker"],
)
async def test_inflight_target_change_cancels_without_success_history_or_health(
    sessions, queue, monkeypatch, changed
):
    stub, _ = provider(monkeypatch)
    await seed(
        sessions, job_changes={"error_type": "TIMEOUT", "error_message": "Earlier retry", "retry_count": 2}
    )
    last_success = NOW - timedelta(days=1)
    async with sessions() as db, db.begin():
        db.add(
            ProviderStatus(
                provider="marriott",
                status="ONLINE",
                last_success=last_success,
                last_error="Earlier room error",
                metrics_date=NOW.date(),
                success_count=4,
                requests_today=5,
            )
        )

    async def change_while_fetching(_):
        async with sessions() as db, db.begin():
            current = await db.get(Hotel, "monthly-hotel")
            if changed == "source":
                (await db.get(AppSetting, "catalog:marriott")).value = {"root": EN_ROOT}
            elif changed == "removed_source":
                (await db.get(AppSetting, "catalog:marriott")).value = {}
            elif changed == "inactive":
                current.active = False
            elif changed == "hotel_host":
                current.official_url = SOURCE.replace(".com.cn", ".com")
            elif changed == "hotel_code":
                current.provider_hotel_id = "SINNV"
            elif changed == "hotel_provider":
                current.provider = "ihg"
            else:
                task = await db.get(CrawlJob, "monthly-job")
                task.payload = {**task.payload, "automatic_catalog_root": "unsupported"}
        return batch()

    stub.get_calendar_hints.side_effect = change_while_fetching
    await run(sessions, queue)
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        assert task.status == "CANCELLED"
        assert task.error_type == "TIMEOUT" and task.retry_count == 2
        assert task.error_message == "Earlier retry; Calendar hint target changed during request"
        assert task.lease_owner is None and task.lease_until is None
        assert await db.get(AppSetting, "calendar_hint_success:monthly-hotel:" + MONTH) is None
        state = await db.get(ProviderStatus, "marriott")
        assert state.status == "ONLINE" and state.success_count == 4 and state.requests_today == 6
        assert state.last_error == "Earlier room error"
        assert state.last_success.replace(tzinfo=timezone.utc) == last_success
        await assert_no_room_or_outbox(db, no_hints=True)
    stub.close.assert_awaited_once()


@pytest.mark.parametrize(
    "error_code", [ErrorCode.TIMEOUT, ErrorCode.RATE_LIMITED, ErrorCode.BLOCKED_BY_ANTIBOT]
)
async def test_provider_failures_use_existing_retry_and_group_cooldown(
    sessions, queue, monkeypatch, error_code
):
    stub, _ = provider(monkeypatch, error=ProviderError(error_code, "Offline failure", retry_after=4000))
    await seed(sessions)
    await run(sessions, queue)
    async with sessions() as db:
        task = await db.get(CrawlJob, "monthly-job")
        state = await db.get(ProviderStatus, "marriott")
        assert task.retry_count == 1 and task.error_type == error_code
        assert task.status == ("FAILED" if error_code == ErrorCode.BLOCKED_BY_ANTIBOT else "PENDING")
        assert task.scheduled_at.replace(tzinfo=timezone.utc) >= NOW + timedelta(seconds=4000)
        assert state.status == ("DEGRADED" if error_code == ErrorCode.TIMEOUT else "BLOCKED")
        await assert_no_room_or_outbox(db, no_hints=True)
    stub.close.assert_awaited_once()


async def test_cross_midnight_completed_hints_drop_only_now_expired_day(sessions, queue, monkeypatch):
    stub, _ = provider(monkeypatch)
    await seed(sessions, job_changes={"payload": {"month": "2030-10", "automatic_catalog_root": CN_ROOT}})

    async def cross_midnight(_):
        next_day = NOW + timedelta(days=1)
        monkeypatch.setattr(calendar_hints, "utcnow", lambda: next_day)
        monkeypatch.setattr(
            worker, "month_window", lambda month: month_window(month, TODAY + timedelta(days=1))
        )
        return batch(
            start_date=TODAY,
            end_date=date(2030, 11, 1),
            days=[
                {"check_in": TODAY, "displayed_price": "100"},
                {"check_in": TODAY + timedelta(days=1), "displayed_price": "200"},
            ],
        )

    stub.get_calendar_hints.side_effect = cross_midnight
    await run(sessions, queue)
    async with sessions() as db:
        assert (await db.get(CrawlJob, "monthly-job")).status == "SUCCEEDED"
        hints = list(await db.scalars(select(CalendarHintHistory)))
        assert [hint.check_in for hint in hints] == [TODAY + timedelta(days=1)]
        await assert_no_room_or_outbox(db)


async def test_cross_midnight_last_partial_month_accepts_exact_original_request(sessions, queue, monkeypatch):
    stub, _ = provider(monkeypatch)
    await seed(
        sessions,
        job_changes={"payload": {"month": "2031-10", "automatic_catalog_root": CN_ROOT}},
    )
    original = month_window("2031-10", TODAY)

    async def cross_midnight(request):
        assert (request.start_date, request.end_date) == original
        next_day = NOW + timedelta(days=1)
        monkeypatch.setattr(calendar_hints, "utcnow", lambda: next_day)
        monkeypatch.setattr(
            worker, "month_window", lambda month: month_window(month, TODAY + timedelta(days=1))
        )
        return batch(
            start_date=request.start_date,
            end_date=request.end_date,
            days=[{"check_in": request.end_date - timedelta(days=1), "displayed_price": "200"}],
        )

    stub.get_calendar_hints.side_effect = cross_midnight
    await run(sessions, queue)
    async with sessions() as db:
        assert (await db.get(CrawlJob, "monthly-job")).status == "SUCCEEDED"
        hints = list(await db.scalars(select(CalendarHintHistory)))
        assert [hint.check_in for hint in hints] == [original[1] - timedelta(days=1)]
        await assert_no_room_or_outbox(db)
