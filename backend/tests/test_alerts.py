import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_domain import fixture_rate
from test_pipeline import FixtureProvider

from app.api.main import app, telegram_http_client
from app.api.main import settings as api_settings
from app.core.config import Settings
from app.core.errors import ErrorCode, ProviderError
from app.core.logging import redact
from app.crawler.worker import process_one
from app.database.session import get_session
from app.models.tables import CrawlJob, NotificationLog, PriceAlert, PriceStatistics, utcnow
from app.providers.registry import FACTORIES
from app.scheduler.main import tick
from app.schemas.domain import JobInput, JobKind
from app.services.alerts import (
    confirm_drop,
    confirm_job_drops,
    detect_drops,
    reconcile_terminal_verifications,
)
from app.services.jobs import enqueue
from app.services.notifications import deliver_one, telegram_text
from app.services.rates import persist_rates, upsert_hotel


def quote(price, captured=None, **changes):
    return fixture_rate().model_copy(
        update={
            "total_price": Decimal(price),
            "cash_price": None,
            "tax": None,
            "captured_at": captured or utcnow(),
            **changes,
        }
    )


async def seed_history(session, prices=("200", "210", "190")):
    hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
    for index, price in enumerate(prices, 1):
        await persist_rates(session, hotel, [quote(price, utcnow() - timedelta(days=index))], f"old{index}")
    return hotel


async def test_worker_drop_confirmation_and_telegram_end_to_end(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    class LowPrice(FixtureProvider):
        async def search_rates(self, request):
            return [quote("70")]

    monkeypatch.setitem(FACTORIES, "marriott", LowPrice)
    settings = Settings(telegram_bot_token=SecretStr("test-token"), telegram_chat_id="123")
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        rate = quote("70")
        job = await enqueue(
            s,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=rate.check_in,
                check_out=rate.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    assert await process_one(sessions, queue, settings)
    async with sessions() as s, s.begin():
        alert = await s.scalar(select(PriceAlert))
        assert not alert.confirmed
        assert await s.scalar(select(func.count()).select_from(NotificationLog)) == 0
        verification = await s.get(CrawlJob, alert.verification_job_id)
        verification.scheduled_at = utcnow() - timedelta(seconds=1)
        verification_id = verification.id
    # Advance the confirmation delay without a wall-clock sleep in the test.
    await queue.redis.delete("hotelbug:limit:marriott")
    await queue.put(verification_id, 100)
    assert await process_one(sessions, queue, settings)
    requests = []

    def telegram(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        assert await deliver_one(sessions, settings, client)
        assert not await deliver_one(sessions, settings, client)
    assert len(requests) == 1
    message = requests[0]["text"]
    for field in ("Fixture hotel", "万豪 Marriott", "2027-01-07", "70", "USD", "65.0%"):
        assert field in message
    async with sessions() as s:
        assert (await s.scalar(select(PriceAlert))).confirmed
        assert (await s.scalar(select(NotificationLog))).status == "SENT"


async def test_recovered_price_does_not_notify_and_candidate_is_deduplicated(sessions):
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        assert await detect_drops(s, hotel, [quote("70")], Settings()) == 1
        assert await detect_drops(s, hotel, [quote("60")], Settings()) == 0
        alert = await s.scalar(select(PriceAlert))
        assert not await confirm_drop(s, alert.id, alert.verification_job_id, [quote("190")])
        assert alert.payload["verification"] == "REJECTED"
        assert await s.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_baseline_needs_distinct_days_and_matching_currency(sessions):
    async with sessions() as s, s.begin():
        hotel = await seed_history(s, ("200", "200"))
        assert await detect_drops(s, hotel, [quote("30")], Settings()) == 0
        await persist_rates(s, hotel, [quote("200", utcnow() - timedelta(days=3), currency="EUR")], "eur")
        assert await detect_drops(s, hotel, [quote("30")], Settings()) == 0


async def test_thirty_day_baseline_prevents_stale_ninety_day_false_positive(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session, ("200", "200", "200"))
        for day in range(40, 45):
            await persist_rates(session, hotel, [quote("500", utcnow() - timedelta(days=day))], f"older{day}")
        # A modest historical new low is a separate, configurable event, not
        # a false PRICE_DROP caused by stale 90-day prices.
        assert (
            await detect_drops(session, hotel, [quote("140")], Settings(alert_historical_lows_enabled=False))
            == 0
        )
        assert await detect_drops(session, hotel, [quote("70")], Settings()) == 1
        alert = await session.scalar(select(PriceAlert))
        assert Decimal(alert.payload["baseline"]) == Decimal("200")
        assert alert.payload["baseline_window_days"] == 30 and alert.payload["history_days"] == 3
        stats = await session.get(PriceStatistics, alert.offer_key)
        assert stats.median_90d == Decimal("500") and stats.median_30d == Decimal("200")


async def test_short_baseline_requires_enough_independent_observation_days(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session, ("100",))
        for day in (40, 41, 42):
            await persist_rates(
                session, hotel, [quote("200", utcnow() - timedelta(days=day))], f"fallback{day}"
            )
        assert await detect_drops(session, hotel, [quote("70")], Settings()) == 1
        alert = await session.scalar(select(PriceAlert))
        assert alert.payload["baseline_window_days"] == 90
        assert Decimal(alert.payload["baseline"]) == Decimal("200")


async def test_statistics_all_time_low_is_actual_observation_not_daily_median(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await persist_rates(
            session, hotel, [quote("5", utcnow() - timedelta(days=100))], "outside-window-low"
        )
        await persist_rates(
            session, hotel, [quote("1000", utcnow() - timedelta(days=100, hours=1))], "outside-window-high"
        )
        await detect_drops(session, hotel, [quote("70")], Settings())
        stats = await session.get(PriceStatistics, quote("70").offer_key())
        assert stats.historical_low == Decimal("5") and stats.historical_high == Decimal("1000")


async def test_confirmed_price_needs_material_further_drop_and_fresh_recheck(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        settings = Settings()
        await detect_drops(session, hotel, [quote("70")], settings)
        first = await session.scalar(select(PriceAlert))
        assert await confirm_drop(session, first.id, first.verification_job_id, [quote("70")])
        assert await detect_drops(session, hotel, [quote("69")], settings) == 0
        assert await detect_drops(session, hotel, [quote("60")], settings) == 0
        assert await detect_drops(session, hotel, [quote("55")], settings) == 1
        second = await session.scalar(select(PriceAlert).where(PriceAlert.id != first.id))
        assert second.verification_job_id != first.verification_job_id and not second.confirmed
        assert Decimal(second.payload["repeat_threshold"]) == Decimal("56")
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 1
        # Still below historical threshold is insufficient if extra-drop condition recovered.
        assert not await confirm_drop(session, second.id, second.verification_job_id, [quote("60")])
        assert await detect_drops(session, hotel, [quote("55")], settings) == 1
        third = await session.scalar(select(PriceAlert).where(PriceAlert.id.not_in((first.id, second.id))))
        assert await confirm_drop(session, third.id, third.verification_job_id, [quote("55")])
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 2
        assert await detect_drops(session, hotel, [quote("50")], settings) == 0


async def test_rejected_candidate_can_be_rechecked_on_later_observation_same_day(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        first = await session.scalar(select(PriceAlert))
        assert not await confirm_drop(session, first.id, first.verification_job_id, [quote("190")])
        assert await detect_drops(session, hotel, [quote("70")], Settings()) == 1
        second = await session.scalar(select(PriceAlert).where(PriceAlert.id != first.id))
        assert second.dedupe_key != first.dedupe_key
        assert await confirm_drop(session, second.id, second.verification_job_id, [quote("70")])


@pytest.mark.parametrize(
    "changes",
    [
        {"member_rate": True},
        {"room_type": None, "room_code": None},
        {"rate_name": None, "rate_code": None},
    ],
)
async def test_member_and_unidentified_aggregate_quotes_cannot_trigger_public_alert(sessions, changes):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        for day in range(1, 4):
            await persist_rates(
                session, hotel, [quote("200", utcnow() - timedelta(days=day), **changes)], f"changed{day}"
            )
        assert await detect_drops(session, hotel, [quote("70", **changes)], Settings()) == 0


async def test_repeat_threshold_is_ui_configurable_and_survives_restart(sessions):
    from app.services.runtime_settings import (
        MonitoringInput,
        load_runtime_settings,
        public_monitoring_settings,
        save_runtime_settings,
    )

    async with sessions() as session, session.begin():
        await save_runtime_settings(session, MonitoringInput(alert_renotify_drop_fraction="0.1"))
    async with sessions() as session, session.begin():
        settings = await load_runtime_settings(session, Settings(alert_renotify_drop_fraction="0.2"))
        assert settings.alert_renotify_drop_fraction == Decimal("0.1")
        assert public_monitoring_settings(settings, set())["alert_renotify_drop_fraction"] == "0.1"
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], settings)
        first = await session.scalar(select(PriceAlert))
        await confirm_drop(session, first.id, first.verification_job_id, [quote("70")])
        assert await detect_drops(session, hotel, [quote("62")], settings) == 1


@pytest.mark.parametrize("status", ["FAILED", "CANCELLED"])
async def test_terminal_verification_closes_candidate_and_later_observation_can_retry(sessions, status):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        first = await session.scalar(select(PriceAlert))
        job = await session.get(CrawlJob, first.verification_job_id)
        job.status, job.error_type = status, "TIMEOUT" if status == "FAILED" else None
        assert await reconcile_terminal_verifications(session) == 1
        assert first.payload["verification"] == status and not first.confirmed
        assert first.payload["verification_error"] == job.error_type
        assert "verification_finished_at" in first.payload
        assert await reconcile_terminal_verifications(session) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
        assert await detect_drops(session, hotel, [quote("70")], Settings()) == 1
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 2


async def test_duplicate_cards_create_only_one_candidate_and_notification(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        assert await detect_drops(session, hotel, [quote("70"), quote("70"), quote("60")], Settings()) == 1
        alert = await session.scalar(select(PriceAlert))
        assert alert.payload["price"] == "70"
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 1
        job = await session.get(CrawlJob, alert.verification_job_id)
        assert job.payload["alert_ids"] == [alert.id]
        assert await confirm_job_drops(session, job, [quote("70"), quote("70")]) == 1
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 1


@pytest.mark.parametrize("status", ["PENDING", "QUEUED", "RUNNING"])
async def test_live_verification_retries_are_not_closed(sessions, status):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        alert = await session.scalar(select(PriceAlert))
        job = await session.get(CrawlJob, alert.verification_job_id)
        job.status = status
        assert await reconcile_terminal_verifications(session) == 0
        assert alert.payload["verification"] == "PENDING"
        assert await detect_drops(session, hotel, [quote("60")], Settings()) == 0


async def test_terminal_reconciliation_is_bounded_and_preserves_finished_results(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        identities = [{"rate_code": f"reconcile-{i}"} for i in range(4)]
        for day in range(1, 4):
            await persist_rates(
                session,
                hotel,
                [quote("200", utcnow() - timedelta(days=day), **identity) for identity in identities],
                f"reconcile-history-{day}",
            )
        lows = [quote("70", **identity) for identity in identities]
        await detect_drops(session, hotel, lows, Settings())
        alerts = (await session.scalars(select(PriceAlert))).all()
        job = await session.get(CrawlJob, alerts[0].verification_job_id)
        assert await confirm_drop(session, alerts[0].id, job.id, lows)
        assert not await confirm_drop(session, alerts[1].id, job.id, [])
        job.status, job.error_type = "FAILED", "TIMEOUT"
        assert await reconcile_terminal_verifications(session, limit=1) == 1
        assert await reconcile_terminal_verifications(session, limit=1) == 1
        assert await reconcile_terminal_verifications(session, limit=1) == 0
        assert alerts[0].confirmed and alerts[0].payload["verification"] == "CONFIRMED"
        assert alerts[1].payload["verification"] == "REJECTED"
        assert {a.payload["verification"] for a in alerts[2:]} == {"FAILED"}
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 1


async def test_worker_timeout_retries_then_scheduler_closes_failed_candidate(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    class TimeoutQuery(FixtureProvider):
        async def search_rates(self, request):
            raise ProviderError(ErrorCode.TIMEOUT, "isolated test timeout")

    monkeypatch.setitem(FACTORIES, "marriott", TimeoutQuery)
    settings = Settings(_env_file=None, max_retries=1, global_monitoring_enabled=False)
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], settings)
        alert = await session.scalar(select(PriceAlert))
        alert_id, job_id = alert.id, alert.verification_job_id
        job = await session.get(CrawlJob, job_id)
        job.scheduled_at = utcnow() - timedelta(seconds=1)
    await queue.put(job_id, 100)
    await process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        job = await session.get(CrawlJob, job_id)
        assert job.status == "PENDING" and job.retry_count == 1
        assert await reconcile_terminal_verifications(session) == 0
        job.scheduled_at = utcnow() - timedelta(seconds=1)
    await queue.redis.flushdb()  # Isolated fake queue; no live service is reset.
    await queue.put(job_id, 100)
    await process_one(sessions, queue, settings)
    await tick(sessions, queue, settings)
    async with sessions() as session:
        assert (await session.get(CrawlJob, job_id)).status == "FAILED"
        alert = await session.get(PriceAlert, alert_id)
        assert not alert.confirmed and alert.payload["verification"] == "FAILED"
        assert alert.payload["verification_error"] == "TIMEOUT"
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0


@pytest.mark.parametrize("exhausted", [True, False])
async def test_scheduler_lease_recovery_only_closes_exhausted_verifications(
    sessions, queue, monkeypatch, exhausted
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    settings = Settings(_env_file=None, max_retries=1, global_monitoring_enabled=False)
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], settings)
        alert = await session.scalar(select(PriceAlert))
        alert_id, job_id = alert.id, alert.verification_job_id
        job = await session.get(CrawlJob, job_id)
        job.status, job.lease_owner = "RUNNING", "dead-test-worker"
        job.lease_until = job.scheduled_at = utcnow() - timedelta(seconds=1)
        job.retry_count = 1 if exhausted else 0
    await tick(sessions, queue, settings)
    async with sessions() as session:
        job, alert = await session.get(CrawlJob, job_id), await session.get(PriceAlert, alert_id)
        assert job.status == ("FAILED" if exhausted else "QUEUED")
        assert alert.payload["verification"] == ("FAILED" if exhausted else "PENDING")
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_scheduler_cancels_candidate_when_provider_is_disabled(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "false")
    settings = Settings(_env_file=None, global_monitoring_enabled=False)
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], settings)
        alert = await session.scalar(select(PriceAlert))
        alert_id, job_id = alert.id, alert.verification_job_id
        job = await session.get(CrawlJob, job_id)
        job.scheduled_at = utcnow() - timedelta(seconds=1)
    await tick(sessions, queue, settings)
    async with sessions() as session:
        assert (await session.get(CrawlJob, job_id)).status == "CANCELLED"
        alert = await session.get(PriceAlert, alert_id)
        assert alert.payload["verification"] == "CANCELLED"
        assert alert.payload["verification_reason"] == "PROVIDER_DISABLED"
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_twenty_offers_share_one_fresh_verification_but_keep_independent_results(sessions):
    changes = [{"room_code": f"room-{index}", "rate_code": f"plan-{index}"} for index in range(20)]
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        for day in range(1, 4):
            await persist_rates(
                session,
                hotel,
                [quote("200", utcnow() - timedelta(days=day), **identity) for identity in changes],
                f"batch-history-{day}",
            )
        lows = [quote("70", **identity) for identity in changes]
        assert await detect_drops(session, hotel, lows, Settings()) == 20
        alerts = (await session.scalars(select(PriceAlert))).all()
        jobs = (await session.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.VERIFY_ANOMALY))).all()
        assert len(jobs) == 1 and len(jobs[0].payload["alert_ids"]) == 20
        assert {alert.verification_job_id for alert in alerts} == {jobs[0].id}
        # The latest query has 18 low plans, one recovered plan and one absent plan.
        latest = lows[:18] + [quote("190", **changes[18])]
        assert await confirm_job_drops(session, jobs[0], latest) == 18
        assert sum(alert.confirmed for alert in alerts) == 18
        assert sum(alert.payload["verification"] == "REJECTED" for alert in alerts) == 2
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 18
        # Replaying a successful job must not create more notification rows.
        await confirm_job_drops(session, jobs[0], latest)
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 18


async def test_verification_batch_never_combines_different_dates_or_guest_counts(sessions):
    rate = quote("70")
    changes = [
        {"rate_code": "one"},
        {"rate_code": "two"},
        {"check_in": rate.check_in + timedelta(days=1), "check_out": rate.check_out + timedelta(days=1)},
        {"adults": 1},
    ]
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        for day in range(1, 4):
            await persist_rates(
                session,
                hotel,
                [quote("200", utcnow() - timedelta(days=day), **identity) for identity in changes],
                f"separate-history-{day}",
            )
        assert (
            await detect_drops(session, hotel, [quote("70", **identity) for identity in changes], Settings())
            == 4
        )
        jobs = (await session.scalars(select(CrawlJob).where(CrawlJob.kind == JobKind.VERIFY_ANOMALY))).all()
        assert len(jobs) == 3
        assert sorted(len(job.payload["alert_ids"]) for job in jobs) == [1, 1, 2]
        assert (
            len({(job.check_in, job.check_out, job.payload["adults"], job.payload["rooms"]) for job in jobs})
            == 3
        )


async def test_legacy_single_alert_jobs_still_confirm_and_other_job_cannot_confirm(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        alert = await session.scalar(select(PriceAlert))
        job = await session.get(CrawlJob, alert.verification_job_id)
        job.payload = {"alert_id": alert.id, "adults": 2, "rooms": 1}
        foreign = CrawlJob(id="wrong-job", payload={"alert_ids": [alert.id]})
        assert await confirm_job_drops(session, foreign, [quote("70")]) == 0
        assert not alert.confirmed
        assert await confirm_job_drops(session, job, [quote("70")]) == 1


async def test_worker_makes_one_confirmation_fetch_for_several_room_plans(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    calls = []
    changes = [{"rate_code": f"plan-{index}"} for index in range(3)]

    class SeveralLows(FixtureProvider):
        async def search_rates(self, request):
            calls.append(request)
            return [quote("70", **identity) for identity in changes]

    monkeypatch.setitem(FACTORIES, "marriott", SeveralLows)
    settings = Settings()
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        for day in range(1, 4):
            await persist_rates(
                session,
                hotel,
                [quote("200", utcnow() - timedelta(days=day), **identity) for identity in changes],
                f"worker-batch-history-{day}",
            )
        rate = quote("70")
        job = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=rate.check_in,
                check_out=rate.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    await process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        verification = await session.scalar(select(CrawlJob).where(CrawlJob.kind == JobKind.VERIFY_ANOMALY))
        assert len(verification.payload["alert_ids"]) == 3
        verification.scheduled_at = utcnow() - timedelta(seconds=1)
        verification_id = verification.id
    await queue.redis.delete("hotelbug:limit:marriott")
    await queue.put(verification_id, 100)
    await process_one(sessions, queue, settings)
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(PriceAlert).where(PriceAlert.confirmed.is_(True))
            )
            == 3
        )
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 3
    assert len(calls) == 2  # initial fetch + one independent confirmation, not four requests


async def test_displayed_cash_drop_is_confirmed_and_labels_unverified_total(sessions):
    def displayed(price, captured=None):
        return quote(price, captured, cash_price=Decimal(price), total_price=None, price_basis="nightly")

    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        for index, amount in enumerate(("200", "210", "190"), 1):
            await persist_rates(
                s,
                hotel,
                [quote(amount, utcnow() - timedelta(days=index), price_basis="nightly")],
                f"total-nightly{index}",
            )
        # The all-fees observations alone cannot provide a displayed-only baseline.
        assert await detect_drops(s, hotel, [displayed("70")], Settings()) == 0
        for index, amount in enumerate(("200", "210", "190"), 1):
            await persist_rates(
                s, hotel, [displayed(amount, utcnow() - timedelta(days=index))], f"cash{index}"
            )
        assert await detect_drops(s, hotel, [displayed("70")], Settings()) == 1
        alert = await s.scalar(select(PriceAlert))
        assert alert.payload["price_field"] == "cash_price"
        assert not await confirm_drop(
            s, alert.id, alert.verification_job_id, [quote("70", price_basis="nightly")]
        )
        assert await confirm_drop(s, alert.id, alert.verification_job_id, [displayed("65")])
        text = telegram_text(alert.payload)
        assert "65" in text and "每晚官网展示价，全部税费未确认" in text
        assert "每晚含税费" not in text
        assert await s.scalar(select(func.count()).select_from(NotificationLog)) == 1


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 500}}, "PENDING"),
        (401, {"ok": False, "error_code": 401}, "FAILED"),
        (200, {"ok": False, "error_code": 500}, "PENDING"),
    ],
)
async def test_telegram_failure_is_not_marked_sent(sessions, status, body, expected):
    settings = Settings(telegram_bot_token=SecretStr("test-token"), telegram_chat_id="123")
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        await detect_drops(s, hotel, [quote("70")], settings)
        alert = await s.scalar(select(PriceAlert))
        await confirm_drop(s, alert.id, alert.verification_job_id, [quote("70")])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    ) as client:
        assert await deliver_one(sessions, settings, client)
    async with sessions() as s:
        log = await s.scalar(select(NotificationLog))
        assert log.status == expected and log.sent_at is None and log.attempts == 1
        if status == 429:
            assert log.next_retry_at.replace(tzinfo=utcnow().tzinfo) > utcnow() + timedelta(seconds=490)


def test_telegram_token_redacted_from_url():
    assert "private-token" not in redact("POST https://api.telegram.org/botprivate-token/sendMessage")


async def test_ui_saved_telegram_config_is_used_for_delivery(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("admin-test"))

    async def dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.put(
                "/api/settings/telegram",
                headers={"authorization": "Bearer admin-test"},
                json={"bot_token": "ui-test-token", "chat_id": "987", "enabled": True},
            )
            assert response.status_code == 200 and response.json()["configured"]
            saved = await api.get("/api/settings/telegram", headers={"authorization": "Bearer admin-test"})
            assert saved.json()["chat_id"] == "987" and "ui-test-token" not in saved.text
    finally:
        app.dependency_overrides.clear()

    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        alert = await session.scalar(select(PriceAlert))
        await confirm_drop(session, alert.id, alert.verification_job_id, [quote("70")])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await deliver_one(
            sessions, Settings(telegram_bot_token=SecretStr(""), telegram_chat_id=""), client
        )
    assert len(requests) == 1
    assert "ui-test-token" in str(requests[0].url)
    assert json.loads(requests[0].content)["chat_id"] == "987"


async def test_manual_telegram_message_uses_saved_config_and_reports_delivery(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("admin-test"))
    sent = []

    async def database():
        async with sessions() as session:
            yield session

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def telegram():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            yield client

    app.dependency_overrides[get_session] = database
    app.dependency_overrides[telegram_http_client] = telegram
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            headers = {"authorization": "Bearer admin-test"}
            assert (await api.post("/api/settings/telegram/test", headers=headers)).status_code == 422
            assert (await api.post("/api/settings/telegram/test")).status_code == 401
            saved = await api.put(
                "/api/settings/telegram",
                headers=headers,
                json={"bot_token": "saved-test-token", "chat_id": "987", "enabled": False},
            )
            assert saved.status_code == 200
            response = await api.post("/api/settings/telegram/test", headers=headers)
            assert response.status_code == 200 and response.json() == {"delivered": True}
            assert "saved-test-token" not in response.text
    finally:
        app.dependency_overrides.clear()
    assert len(sent) == 1
    assert "saved-test-token" in str(sent[0].url)
    assert json.loads(sent[0].content)["chat_id"] == "987"
    assert "测试消息" in json.loads(sent[0].content)["text"]
