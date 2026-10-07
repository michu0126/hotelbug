import json
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from audit_ihg_catalog import run_batch
from audit_ihg_price import (
    install_stay_diagnostics,
    requeue_fixed_identity_job,
    requeue_fixed_room_job,
    run_existing_price,
)
from sqlalchemy import select
from test_domain import fixture_rate

from app.core.config import ProviderPolicy, Settings
from app.core.errors import ErrorCode, ProviderError
from app.crawler import worker
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.ihg_catalog import ROOT
from app.providers.registry import FACTORIES
from app.schemas.domain import CatalogPageData, HotelData, JobInput, JobKind, ProviderName
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel
from app.services.watchlists import expand_global


async def seed(sessions, provider="ihg"):
    settings = Settings(provider_overrides={provider: ProviderPolicy(enabled=True)}, global_jobs_per_tick=1)
    async with sessions() as db, db.begin():
        marker_key = "audit:directory-only" if provider == "ihg" else "audit:sitemap-catalog"
        db.add(AppSetting(key=marker_key, value={"provider": provider}))
        await upsert_hotel(
            db,
            HotelData(
                provider=provider,
                provider_hotel_id="TEST0",
                hotel_name="Explicit offline hotel",
                official_url="https://www.ihg.com/holidayinnexpress/hotels/us/en/offline/test0/hoteldetail",
            ),
        )
        assert await expand_global(db, settings) == 1
    return settings


def quote():
    return fixture_rate().model_copy(
        update={
            "provider": ProviderName.IHG,
            "provider_hotel_id": "TEST0",
            "check_in": date.today(),
            "check_out": date.today() + timedelta(days=1),
            "captured_at": utcnow(),
        }
    )


async def test_generated_price_job_persists_and_is_never_repeated_after_success(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    execute = AsyncMock(return_value=[quote()])
    monkeypatch.setattr(worker, "execute", execute)
    first = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert first["outcome"] == "SUCCEEDED" and first["history_rows_persisted"] == 1
    assert first["stay_scan_status"] == "AVAILABLE" and first["offers"][0]["currency"] == "USD"
    second = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert second["outcome"] == "NO_PENDING_PRICE_DUE" and execute.await_count == 1


async def test_price_report_preserves_unicode_on_windows_gbk_console(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        hotel = await db.scalar(select(Hotel))
        hotel.hotel_name = "Hotel\u2060 Name"
    unusual = quote().model_copy(update={"room_type": "Standard\u2060 Room"})
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=[unusual]))
    emitted = []

    def gbk_console(value):
        value.encode("gbk")
        emitted.append(json.loads(value))

    result = await run_existing_price(sessions, queue, settings, emit=gbk_console)
    assert result["outcome"] == "SUCCEEDED" and result["history_rows_persisted"] == 1
    assert emitted[0]["hotel"] == "Hotel\u2060 Name"
    assert emitted[-1]["offers"][0]["room_type"] == unusual.room_type


async def test_default_price_audit_skips_new_job_for_observed_stay_without_changing_job(
    sessions, queue, monkeypatch
):
    settings = await seed(sessions)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    execute = AsyncMock(return_value=[quote()])
    monkeypatch.setattr(worker, "execute", execute)
    first = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, first["job_id"])
        monitoring = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=original.hotel_id,
                check_in=original.check_in,
                check_out=original.check_out,
                payload={"adults": 2, "rooms": 1},
            ),
        )
        monitoring_id, deadline = monitoring.id, monitoring.scheduled_at
    skipped = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert skipped["outcome"] == "NO_PENDING_PRICE_DUE" and execute.await_count == 1
    async with sessions() as db:
        waiting = await db.get(CrawlJob, monitoring_id)
        assert waiting.status == "PENDING" and waiting.scheduled_at == deadline.replace(tzinfo=None)
    allowed = await run_existing_price(sessions, queue, settings, emit=lambda _: None, allow_due_recheck=True)
    assert allowed["outcome"] == "SUCCEEDED" and execute.await_count == 2


@pytest.mark.parametrize("change", ["date", "adults"])
async def test_default_price_audit_continues_unobserved_dates_and_occupancies(
    sessions, queue, monkeypatch, change
):
    settings = await seed(sessions)
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=[quote()]))
    first = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, first["job_id"])
        old_stay = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=original.hotel_id,
                check_in=original.check_in,
                check_out=original.check_out,
            ),
        )
        changed = quote().model_copy(
            update=(
                {
                    "check_in": quote().check_in + timedelta(days=1),
                    "check_out": quote().check_out + timedelta(days=1),
                }
                if change == "date"
                else {"adults": 1}
            )
        )
        new_stay = await enqueue(
            db,
            JobInput(
                provider="ihg",
                kind=JobKind.FETCH_RATE,
                hotel_id=original.hotel_id,
                check_in=changed.check_in,
                check_out=changed.check_out,
                payload={"adults": changed.adults, "rooms": changed.rooms},
            ),
        )
        old_id, new_id = old_stay.id, new_stay.id
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=[changed]))
    result = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert result["outcome"] == "SUCCEEDED" and result["job_id"] == new_id
    async with sessions() as db:
        assert (await db.get(CrawlJob, old_id)).status == "PENDING"


async def test_failed_future_retry_is_not_reset_or_executed_again(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    execute = AsyncMock(side_effect=ProviderError(ErrorCode.TIMEOUT, "Explicit offline timeout"))
    monkeypatch.setattr(worker, "execute", execute)
    first = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert first["outcome"] == "PENDING" and first["error"] == "TIMEOUT" and not first["offers"]
    async with sessions() as db:
        before = (await db.get(CrawlJob, first["job_id"])).scheduled_at
    second = await run_existing_price(
        sessions, queue, settings, emit=lambda _: None, target_job_id=first["job_id"]
    )
    assert second["outcome"] == "NO_PENDING_PRICE_DUE" and execute.await_count == 1
    async with sessions() as db:
        assert (await db.get(CrawlJob, first["job_id"])).scheduled_at == before


async def test_price_audit_refuses_a_database_without_its_marker(sessions, queue):
    with pytest.raises(AssertionError, match="isolated IHG audit"):
        await run_existing_price(sessions, queue, Settings(), emit=lambda _: None)


async def test_paused_provider_does_not_open_a_page_or_change_pause(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        db.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=1)))
    execute = AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    result = await run_existing_price(sessions, queue, settings, emit=lambda _: None)
    assert result["outcome"] == "PENDING" and not result["offers"]
    execute.assert_not_awaited()


async def test_directory_audit_after_saved_history_keeps_quotes_and_writes_none(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=[quote()]))
    assert (await run_existing_price(sessions, queue, settings, emit=lambda _: None))[
        "history_rows_persisted"
    ] == 1
    monkeypatch.setattr(
        worker,
        "execute",
        AsyncMock(return_value=CatalogPageData(provider="ihg", source_url=ROOT, complete=True)),
    )
    monkeypatch.setattr(queue, "rate_limit", AsyncMock(return_value=0))
    report = await run_batch(sessions, queue, settings, limit=1, emit=lambda _: None)
    assert report["directory_quotes"] == report["notifications"] == 0
    assert report["total_existing_price_history"] == 1


async def test_explicit_fixed_job_retest_preserves_failure_dedupes_and_stops_after_success(
    sessions, queue, monkeypatch
):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        original = await db.scalar(select(CrawlJob))
        original.status = "FAILED"
        original.error_type = "INVALID_RESPONSE"
        original.error_message = "IHG hotel, dates or occupancy mismatch"
        original_id = original.id
    retest = await requeue_fixed_identity_job(sessions, original_id)
    assert retest != original_id
    assert await requeue_fixed_identity_job(sessions, original_id) == retest
    monkeypatch.setattr(worker, "execute", AsyncMock(return_value=[quote()]))
    assert (await run_existing_price(sessions, queue, settings, emit=lambda _: None))[
        "outcome"
    ] == "SUCCEEDED"
    async with sessions() as db:
        failure = await db.get(CrawlJob, original_id)
        assert failure.status == "FAILED" and failure.error_type == "INVALID_RESPONSE"
    with pytest.raises(AssertionError, match="already succeeded"):
        await requeue_fixed_identity_job(sessions, original_id)


@pytest.mark.parametrize("code", ["RATE_LIMITED", "BLOCKED_BY_ANTIBOT", "TIMEOUT"])
async def test_fixed_identity_retest_is_not_an_access_or_timeout_retry(sessions, code):
    await seed(sessions)
    async with sessions() as db, db.begin():
        job = await db.scalar(select(CrawlJob))
        job.status = "FAILED"
        job.error_type = code
        job.error_message = "IHG hotel, dates or occupancy mismatch"
        job_id = job.id
    with pytest.raises(AssertionError):
        await requeue_fixed_identity_job(sessions, job_id)


async def recorded_room_failure(sessions, provider="ihg", future=False):
    await seed(sessions, provider)
    async with sessions() as db, db.begin():
        job = await db.scalar(select(CrawlJob))
        job.status = "FAILED"
        job.error_type, job.error_message = {
            "ihg": ("TIMEOUT", "IHG booking page failed at expand room plans"),
            "gha": ("PROVIDER_CHANGED", "GHA room identity or inclusive summary price missing"),
        }[provider]
        if future:
            job.scheduled_at = utcnow() + timedelta(hours=1)
        return job.id, job.scheduled_at


@pytest.mark.parametrize("provider", ["ihg", "gha"])
async def test_room_fix_retest_preserves_failure_deadline_and_only_creates_one_attempt(sessions, provider):
    original_id, deadline = await recorded_room_failure(sessions, provider, future=True)
    child_id = await requeue_fixed_room_job(sessions, provider, original_id)
    assert child_id != original_id
    assert await requeue_fixed_room_job(sessions, provider, original_id) == child_id
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, original_id)
        child = await db.get(CrawlJob, child_id)
        assert original.status == "FAILED"
        assert child.scheduled_at == deadline.replace(tzinfo=None)
        assert child.dedupe_key == original.dedupe_key and child.payload == original.payload
        child.status = "FAILED"
    with pytest.raises(AssertionError, match="already finished"):
        await requeue_fixed_room_job(sessions, provider, original_id)


@pytest.mark.parametrize(
    "error,message",
    [
        ("RATE_LIMITED", "IHG booking page failed at expand room plans"),
        ("BLOCKED_BY_ANTIBOT", "IHG booking page failed at expand room plans"),
        ("TIMEOUT", "IHG booking page failed at selected stay confirmation"),
    ],
)
async def test_room_fix_is_not_access_or_unrelated_timeout_retry(sessions, error, message):
    original_id, _ = await recorded_room_failure(sessions)
    async with sessions() as db, db.begin():
        original = await db.get(CrawlJob, original_id)
        original.error_type, original.error_message = error, message
    with pytest.raises(AssertionError):
        await requeue_fixed_room_job(sessions, "ihg", original_id)


async def test_room_fix_refuses_already_succeeded_identity(sessions):
    original_id, _ = await recorded_room_failure(sessions)
    child_id = await requeue_fixed_room_job(sessions, "ihg", original_id)
    async with sessions() as db, db.begin():
        await db.delete(await db.get(AppSetting, "audit:room-fix-retest:" + original_id))
        (await db.get(CrawlJob, child_id)).status = "SUCCEEDED"
    with pytest.raises(AssertionError, match="already succeeded"):
        await requeue_fixed_room_job(sessions, "ihg", original_id)


async def test_targeted_retest_does_not_execute_other_older_due_jobs(sessions, queue, monkeypatch):
    settings = await seed(sessions)
    execute = AsyncMock(return_value=[quote()])
    monkeypatch.setattr(worker, "execute", execute)
    assert (await run_existing_price(sessions, queue, settings, target_job_id="different-id")) == {
        "outcome": "NO_PENDING_PRICE_DUE"
    }
    execute.assert_not_awaited()


@pytest.mark.parametrize("targeted", [False, True])
async def test_expired_job_cannot_start_a_price_request(sessions, queue, monkeypatch, targeted):
    settings = await seed(sessions)
    async with sessions() as db, db.begin():
        job = await db.scalar(select(CrawlJob))
        job.check_in = date.today() - timedelta(days=2)
        job.check_out = date.today() - timedelta(days=1)
        job_id = job.id
    execute = AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    result = await run_existing_price(
        sessions, queue, settings, emit=lambda _: None, target_job_id=job_id if targeted else None
    )
    assert result["outcome"] == ("CANCELLED" if targeted else "NO_PENDING_PRICE_DUE")
    execute.assert_not_awaited()
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == ("CANCELLED" if targeted else "PENDING")


@pytest.mark.parametrize("fail_diagnostic", [False, True])
async def test_optional_public_controls_do_not_change_original_failure_or_open_another_page(
    monkeypatch, capsys, fail_diagnostic
):
    instance = MagicMock()
    page = instance._page
    page.url = "https://www.ihg.com/find-hotels/select-roomrate?private=SECRET&qAdlt=SECRET&qSlH=DENLT&qCiD=05&qRms=1&qRms=2"
    page.is_closed.return_value = False
    observed = {
        "input.calendar-input": [{"value": "10/05/2026", "visible": True}],
        "#room-and-guest": [{"value": "1 Room, 2 Guests", "visible": True}],
        'app-room-rate-item[id^="ROOM_CODE"]': [],
        "h1,h2,h3,[role='alert'],[role='status']": [
            {"tag": "H2", "role": None, "text": "No rooms available"}
        ],
        "body": ["No rooms available"],
        'app-hotel-card-list-view[data-testid="hotel-card"]': [],
    }
    selectors = []

    def locator(selector):
        selectors.append(selector)
        control = MagicMock()
        control.evaluate_all = AsyncMock(
            side_effect=RuntimeError("SECRET") if fail_diagnostic else None, return_value=observed[selector]
        )
        return control

    page.locator.side_effect = locator
    error = ProviderError(ErrorCode.TIMEOUT, "Offline original failure")
    original = AsyncMock(side_effect=error)
    instance.search_rates = original
    monkeypatch.setitem(FACTORIES, "ihg", lambda: instance)
    install_stay_diagnostics("ihg")
    with pytest.raises(ProviderError) as raised:
        await FACTORIES["ihg"]().search_rates(quote())
    assert raised.value is error and original.await_count == 1
    output = capsys.readouterr().out
    assert "SECRET" not in output
    projected = json.loads(output)
    if fail_diagnostic:
        assert projected == {"public_ihg_stay_controls_unavailable": True}
    else:
        assert projected["public_ihg_stay_controls"]["guests"] == observed["#room-and-guest"]
        assert projected["public_ihg_stay_controls"]["result_messages"] == observed["body"]
        assert projected["public_ihg_stay_controls"]["public_stay_query"] == {
            "qAdlt": "other",
            "qSlH": "DENLT",
            "qCiD": "05",
            "qRms": "other",
        }
        assert selectors == list(observed)
    page.goto.assert_not_called()
