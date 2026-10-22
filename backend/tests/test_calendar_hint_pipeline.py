"""Offline calendar signals need two real public room observations before outbox."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.core.config import ProviderPolicy, Settings
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
from app.providers.marriott_cn_catalog import ROOT
from app.schemas.calendar_hints import CalendarHintBatch
from app.schemas.domain import JobInput, JobKind, ProviderName, RateData
from app.services import calendar_hints, jobs
from app.services.alerts import confirm_job_drops
from app.services.calendar_hints import (
    CANDIDATE_CURSOR,
    SIGNAL,
    WAIT_CONFIRM,
    WAIT_QUOTE,
    admit_calendar_candidates,
    hint_series_key,
    match_calendar_candidates,
    persist_calendar_hints,
)
from app.services.catalog_price_source import SOURCE_MARKER
from app.services.jobs import enqueue
from app.services.notifications import telegram_text
from app.services.rates import persist_rates

NOW = datetime(2030, 10, 14, 8, tzinfo=timezone.utc)
STAY = date(2030, 11, 20)
SOURCE = (
    "https://www.marriott.com.cn/search/availabilityCalendar.mi"
    "?isRateCalendar=true&propertyCode=PBHMD&isSearch=true&currency="
)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class ClockDate(date):
        @classmethod
        def today(cls):
            return NOW.date()

    monkeypatch.setattr(calendar_hints, "utcnow", lambda: NOW)
    monkeypatch.setattr(jobs, "utcnow", lambda: NOW)
    monkeypatch.setattr(jobs, "date", ClockDate)


def settings(**changes):
    return Settings(
        _env_file=None,
        provider_overrides={
            p.value: ProviderPolicy(enabled=p == ProviderName.MARRIOTT) for p in ProviderName
        },
        **changes,
    )


def hint(price="100", captured=NOW, **changes):
    return CalendarHintBatch(
        **{
            "provider_hotel_id": "PBHMD",
            "start_date": date(2030, 11, 1),
            "end_date": date(2030, 12, 1),
            "currency": "USD",
            "source_url": SOURCE,
            "raw_response_hash": "a" * 64,
            "captured_at": captured,
            "days": [{"check_in": STAY, "displayed_price": price}],
            **changes,
        }
    )


def quote(price="120", captured=NOW + timedelta(seconds=5), **changes):
    return RateData(
        **{
            "provider": "marriott",
            "provider_hotel_id": "PBHMD",
            "check_in": STAY,
            "check_out": STAY + timedelta(days=1),
            "room_type": "Deluxe king",
            "room_code": "DLX",
            "rate_name": "Flexible Non-Member Rate",
            "rate_code": "REG",
            "member_rate": False,
            "currency": "USD",
            "total_price": Decimal(price),
            "availability": True,
            "captured_at": captured,
            "source_url": "https://www.marriott.com.cn/reservation/rateListMenu.mi",
            "raw_response_hash": "b" * 64,
            **changes,
        }
    )


async def counts(session, *models):
    return tuple([await session.scalar(select(func.count()).select_from(model)) for model in models])


async def seed_hotel(session):
    hotel = Hotel(
        provider="marriott",
        provider_hotel_id="PBHMD",
        hotel_name="Offline calendar hotel",
        official_url=SOURCE,
    )
    session.add(hotel)
    session.add(AppSetting(key="catalog:marriott", value={"root": ROOT}))
    await session.flush()
    return hotel


async def observe(session, hotel, key, batch, config=None):
    return await persist_calendar_hints(
        session,
        hotel,
        SimpleNamespace(id=key, payload={"month": batch.start_date.strftime("%Y-%m")}),
        batch,
        config or settings(),
    )


async def seed_candidate(session, config=None):
    hotel = await seed_hotel(session)
    for age, amount in ((3, "390"), (2, "400"), (1, "410")):
        await observe(session, hotel, f"historical-{age}", hint(amount, NOW - timedelta(days=age)), config)
    await observe(session, hotel, "calendar-drop", hint(), config)
    return hotel, await session.scalar(select(PriceAlert))


@pytest.mark.parametrize("stage", ["admission", "matching", "confirmation"])
async def test_reclassified_qa_candidate_cannot_reach_notification_outbox(sessions, stage):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        if stage in ("matching", "confirmation"):
            assert await admit_calendar_candidates(session, settings()) == 1
            job = await session.get(CrawlJob, alert.payload["quote_job_id"])
            if stage == "confirmation":
                assert await match_calendar_candidates(session, hotel, job, [quote()]) == 1
                job.status = "SUCCEEDED"
                assert await admit_calendar_candidates(session, settings()) == 1
                job = await session.get(CrawlJob, alert.verification_job_id)
        hotel.provider_hotel_id, hotel.hotel_name = "AQAMC", "EMPOWER Training Hotel"
        await session.flush()
        if stage == "admission":
            assert await admit_calendar_candidates(session, settings()) == 0
        elif stage == "matching":
            assert await match_calendar_candidates(session, hotel, job, [quote()]) == 0
        else:
            assert await confirm_job_drops(session, job, [quote(captured=NOW + timedelta(minutes=5))]) == 0
        assert alert.payload["verification"] == "CANCELLED"
        assert await counts(session, CalendarHintHistory, NotificationLog) == (4, 0)
        assert not alert.confirmed and hotel.active


async def test_test_hotel_never_creates_new_calendar_or_regular_drop_candidates(sessions):
    from app.services.alerts import detect_drops

    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        hotel.provider_hotel_id, hotel.hotel_name = "AQAMC", "EMPOWER Training Hotel"
        await session.flush()
        assert (
            await observe(
                session,
                hotel,
                "qa-observation",
                hint(provider_hotel_id="AQAMC", source_url=SOURCE.replace("PBHMD", "AQAMC")),
            )
            == 0
        )
        assert await detect_drops(session, hotel, [quote(provider_hotel_id="AQAMC")], settings()) == 0
        assert await counts(session, CalendarHintHistory, PriceAlert, NotificationLog) == (0, 0, 0)


async def first_room_quote(session, hotel, alert, config=None):
    config = config or settings()
    assert await admit_calendar_candidates(session, config) == 1
    job = await session.get(CrawlJob, alert.payload["quote_job_id"])
    actual = quote()
    await persist_rates(session, hotel, [actual], job.id)
    assert await match_calendar_candidates(session, hotel, job, [actual]) == 1
    job.status = "SUCCEEDED"
    await session.flush()
    return job, actual


async def test_calendar_signal_needs_two_real_observations_before_existing_outbox(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        assert alert.event_type == SIGNAL and alert.payload["verification"] == WAIT_QUOTE
        assert alert.payload["baseline_kind"] == "CALENDAR_MINIMUM_DAILY_MEDIAN"
        assert Decimal(alert.payload["baseline"]) == 400
        assert Decimal(alert.payload["threshold"]) == 200
        assert alert.payload["history_days"] == 3
        assert await counts(session, CalendarHintHistory, Rate, PriceHistory, StayScan, NotificationLog) == (
            4,
            0,
            0,
            0,
            0,
        )
        first, actual = await first_room_quote(session, hotel, alert)
        assert alert.payload["verification"] == WAIT_CONFIRM
        assert alert.offer_key == actual.offer_key()
        assert alert.payload["price_field"] == "total_price"
        assert await counts(session, PriceHistory, NotificationLog) == (1, 0)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        assert verification.kind == JobKind.VERIFY_ANOMALY and verification.id != first.id
        assert verification.scheduled_at.replace(tzinfo=timezone.utc) >= NOW + timedelta(seconds=60)
        assert verification.payload["alert_ids"] == [alert.id]
        assert verification.payload[SOURCE_MARKER] == ROOT
        assert alert.payload["verification"] == "PENDING"
        second = quote("110", NOW + timedelta(seconds=65))
        await persist_rates(session, hotel, [second], verification.id)
        assert await confirm_job_drops(session, verification, [second]) == 1
        assert alert.confirmed and alert.payload["verification"] == "CONFIRMED"
        assert await counts(session, CalendarHintHistory, PriceHistory, NotificationLog) == (4, 2, 1)
        text = telegram_text(alert.payload)
        for expected in (hotel.hotel_name, "万豪 Marriott", STAY.isoformat(), "110", "USD", "日历"):
            assert expected in text


async def test_same_job_replay_and_pending_lower_hint_do_not_duplicate_candidate(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        assert await observe(session, hotel, "calendar-drop", hint()) == 0
        assert await observe(session, hotel, "calendar-drop-lower", hint("90")) == 1
        assert await counts(session, CalendarHintHistory, PriceAlert, NotificationLog) == (5, 1, 0)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        assert await admit_calendar_candidates(session, settings()) == 0
        verification = await session.get(CrawlJob, alert.verification_job_id)
        second = quote(captured=NOW + timedelta(seconds=65))
        assert await confirm_job_drops(session, verification, [second]) == 1
        assert await confirm_job_drops(session, verification, [second]) == 1
        assert await counts(session, CrawlJob, NotificationLog) == (2, 1)


@pytest.mark.parametrize("ages", [(1, 1, 1), (0, 0, 0), (1, 2), (91, 92, 93)])
async def test_baseline_requires_three_distinct_prior_days_inside_lookback(sessions, ages):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        for index, age in enumerate(ages):
            await observe(session, hotel, f"old-{index}", hint("400", NOW - timedelta(days=age)))
        await observe(session, hotel, "low", hint())
        assert await counts(session, PriceAlert, PriceHistory, NotificationLog) == (0, 0, 0)


@pytest.mark.parametrize("dimension", ["date", "currency", "source", "basis", "occupancy"])
async def test_other_hint_series_cannot_supply_missing_third_history_day(sessions, dimension):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        for age in (1, 2):
            await observe(session, hotel, f"old-{age}", hint("400", NOW - timedelta(days=age)))
        third = hint("400", NOW - timedelta(days=3))
        day = STAY
        if dimension == "date":
            day += timedelta(days=1)
        elif dimension == "currency":
            third = third.model_copy(update={"currency": "EUR"})
        elif dimension == "source":
            third = CalendarHintBatch(**{**third.model_dump(), "source_url": SOURCE + "&showFullPrice=false"})
        elif dimension == "basis":
            third = third.model_copy(update={"basis": "OTHER_CALENDAR_BASIS"})
        else:
            third = third.model_copy(update={"adults": 1})
        session.add(
            CalendarHintHistory(
                hotel_id=hotel.id,
                provider=hotel.provider,
                job_id="different-series",
                series_key=hint_series_key(hotel, third, day),
                check_in=day,
                adults=third.adults,
                rooms=third.rooms,
                displayed_price=Decimal("400"),
                currency=third.currency,
                basis=third.basis,
                source_url=str(third.source_url),
                captured_at=third.captured_at,
                raw_response_hash=third.raw_response_hash,
            )
        )
        await session.flush()
        await observe(session, hotel, "low", hint())
        assert await counts(session, PriceAlert, PriceHistory, NotificationLog) == (0, 0, 0)


async def test_daily_medians_prevent_high_frequency_samples_overweighting_baseline(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        for age, price in ((3, "200"), (2, "200"), (1, "1000")):
            for repeat in range(6 if age == 1 else 1):
                await observe(session, hotel, f"old-{age}-{repeat}", hint(price, NOW - timedelta(days=age)))
        await observe(session, hotel, "current", hint("120"))
        assert await counts(session, PriceAlert) == (0,)


async def test_full_capacity_preserves_signal_then_admits_when_normal_slot_frees(sessions):
    config = settings(max_pending_jobs=20)
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session, config)
        jobs_to_fill = []
        for index in range(20):
            job = CrawlJob(
                provider="marriott",
                kind=JobKind.PROVIDER_HEALTHCHECK,
                dedupe_key=f"full-{index}",
                payload={},
                status="PENDING",
            )
            session.add(job)
            jobs_to_fill.append(job)
        await session.flush()
        assert await admit_calendar_candidates(session, config) == 0
        assert alert.payload["verification"] == WAIT_QUOTE and "quote_job_id" not in alert.payload
        assert await counts(session, PriceAlert, NotificationLog) == (1, 0)
        jobs_to_fill[0].status = "SUCCEEDED"
        await session.flush()
        assert await admit_calendar_candidates(session, config) == 1
        assert (await session.get(CrawlJob, alert.payload["quote_job_id"])).hotel_id == hotel.id


@pytest.mark.parametrize("kind", [JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY])
async def test_existing_matching_live_stay_is_reused_without_an_extra_query(sessions, kind):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        existing = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=kind,
                hotel_id=hotel.id,
                check_in=STAY,
                check_out=STAY + timedelta(days=1),
                payload={SOURCE_MARKER: ROOT},
            ),
        )
        assert await admit_calendar_candidates(session, settings()) == 0
        assert alert.payload["quote_job_id"] == existing.id
        assert await counts(session, CrawlJob) == (1,)


@pytest.mark.parametrize(
    "change",
    [
        {"source_url": "https://www.marriott.com/reservation/rateListMenu.mi"},
        {"member_rate": True},
        {"member_rate": None},
        {"total_price": None, "cash_price": Decimal("100")},
        {"currency": "EUR"},
        {"captured_at": NOW - timedelta(seconds=1)},
        {"total_price": Decimal("201")},
        {"rate_name": "Corporate rate"},
        {"room_code": None, "room_type": None},
        {"availability": False},
    ],
)
async def test_first_quote_rejects_wrong_source_or_unconfirmed_public_amount(sessions, change):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        assert await admit_calendar_candidates(session, settings()) == 1
        job = await session.get(CrawlJob, alert.payload["quote_job_id"])
        assert await match_calendar_candidates(session, hotel, job, [quote(**change)]) == 0
        assert alert.payload["verification"] == "REJECTED"
        assert await counts(session, NotificationLog) == (0,)


async def test_pending_signal_waits_during_pause_then_uses_normal_eligible_lane(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        state = ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=NOW + timedelta(hours=1))
        session.add(state)
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        state.blocked_until = None
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 1


async def test_candidate_cancels_when_hotel_moves_off_selected_cn_source(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        hotel.official_url = "https://www.marriott.com/en-us/hotels/pbhmd-test/overview/"
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        assert alert.payload["verification"] == "CANCELLED"
        assert await counts(session, CrawlJob, NotificationLog) == (0, 0)


async def test_independent_confirmation_cannot_change_official_source_for_calendar_signal(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        wrong_source = quote(
            captured=NOW + timedelta(seconds=65),
            source_url="https://www.marriott.com/reservation/rateListMenu.mi",
        )
        assert await confirm_job_drops(session, verification, [wrong_source]) == 0
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


@pytest.mark.parametrize("captured", [NOW, NOW + timedelta(seconds=5)])
async def test_independent_confirmation_rejects_replayed_first_observation(sessions, captured):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        assert await confirm_job_drops(session, verification, [quote(captured=captured)]) == 0
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


async def test_independent_confirmation_rejects_source_switch_after_admission(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        catalog = await session.get(AppSetting, "catalog:marriott")
        catalog.value = {"root": EN_ROOT}
        await session.flush()
        assert (
            await confirm_job_drops(session, verification, [quote(captured=NOW + timedelta(seconds=65))]) == 0
        )
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


@pytest.mark.parametrize("first_observed", [False, True])
async def test_waiting_signal_cancels_when_explicit_accepted_source_changes(sessions, first_observed):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        if first_observed:
            await first_room_quote(session, hotel, alert)
        catalog = await session.get(AppSetting, "catalog:marriott")
        catalog.value = {"root": EN_ROOT}
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        assert alert.payload["verification"] == "CANCELLED"
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


async def test_wrong_source_live_stay_does_not_absorb_cn_hint_followup(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        existing = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=STAY,
                check_out=STAY + timedelta(days=1),
                payload={SOURCE_MARKER: EN_ROOT},
            ),
        )
        assert await admit_calendar_candidates(session, settings()) == 1
        assert alert.payload["quote_job_id"] != existing.id
        assert await counts(session, CrawlJob) == (2,)


async def test_confirmation_capacity_wait_survives_database_session_restart(sessions):
    config = settings(max_pending_jobs=20)
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session, config)
        await first_room_quote(session, hotel, alert, config)
        alert_id = alert.id
        for index in range(20):
            session.add(
                CrawlJob(
                    provider="marriott",
                    kind=JobKind.PROVIDER_HEALTHCHECK,
                    dedupe_key=f"confirmation-full-{index}",
                    payload={},
                    status="PENDING",
                )
            )
        await session.flush()
        assert await admit_calendar_candidates(session, config) == 0
        assert alert.payload["verification"] == WAIT_CONFIRM
        assert alert.verification_job_id is None
    async with sessions() as session, session.begin():
        alert = await session.get(PriceAlert, alert_id)
        assert alert.payload["verification"] == WAIT_CONFIRM
        completed = await session.scalar(
            select(CrawlJob).where(CrawlJob.kind == JobKind.PROVIDER_HEALTHCHECK)
        )
        completed.status = "SUCCEEDED"
        await session.flush()
        assert await admit_calendar_candidates(session, config) == 1
        assert alert.payload["verification"] == "PENDING"
        assert (await session.get(CrawlJob, alert.verification_job_id)).kind == JobKind.VERIFY_ANOMALY
        assert await counts(session, NotificationLog) == (0,)


@pytest.mark.parametrize(
    "start,end,days,expected",
    [
        (
            date(2030, 10, 1),
            date(2030, 11, 1),
            [date(2030, 10, 13), date(2030, 10, 14), date(2030, 10, 15)],
            2,
        ),
        (
            date(2031, 10, 1),
            date(2031, 11, 1),
            [date(2031, 10, 13), date(2031, 10, 14), date(2031, 10, 15)],
            1,
        ),
    ],
)
async def test_persistence_recuts_observation_days_at_rolling_window_edges(
    sessions, start, end, days, expected
):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        batch = hint(
            start_date=start,
            end_date=end,
            days=[{"check_in": day, "displayed_price": "100"} for day in days],
        )
        assert await observe(session, hotel, "boundary", batch) == expected
        rows = (await session.scalars(select(CalendarHintHistory))).all()
        assert len(rows) == expected
        assert all(NOW.date() <= row.check_in < NOW.date() + timedelta(days=365) for row in rows)
        assert await counts(session, PriceHistory, StayScan, NotificationLog) == (0, 0, 0)


@pytest.mark.parametrize(
    "change",
    [
        {"total_price": Decimal("250")},
        {"total_price": None, "cash_price": Decimal("100")},
        {"member_rate": True},
        {"room_code": "OTHER"},
        {"rate_code": "OTHER"},
        {"availability": False},
    ],
)
async def test_second_real_observation_must_reconfirm_same_public_room_and_low_total(sessions, change):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        second = quote(captured=NOW + timedelta(seconds=65), **change)
        assert await confirm_job_drops(session, verification, [second]) == 0
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


async def test_recent_calendar_baseline_excludes_stale_high_prices_when_three_days_exist(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        for age in (3, 2, 1):
            await observe(session, hotel, f"recent-{age}", hint("200", NOW - timedelta(days=age)))
        for age in range(40, 46):
            await observe(session, hotel, f"stale-{age}", hint("1000", NOW - timedelta(days=age)))
        await observe(session, hotel, "current", hint("120"))
        assert await counts(session, PriceAlert, NotificationLog) == (0, 0)


@pytest.mark.parametrize("changed", ["accepted_source", "hotel_source", "disabled_hotel"])
async def test_inflight_calendar_result_does_not_reintroduce_stale_source_history(sessions, changed):
    async with sessions() as session, session.begin():
        hotel = await seed_hotel(session)
        hotel_id = hotel.id
    # The browser owns the detached claim-time hotel while another transaction
    # changes current settings. The completion must re-read authoritative data.
    async with sessions() as session, session.begin():
        if changed == "accepted_source":
            catalog = await session.get(AppSetting, "catalog:marriott")
            catalog.value = {"root": EN_ROOT}
        else:
            changed_hotel = await session.get(Hotel, hotel_id)
            if changed == "hotel_source":
                changed_hotel.official_url = "https://www.marriott.com/en-us/hotels/pbhmd-offline/overview/"
            else:
                changed_hotel.active = False
    async with sessions() as session, session.begin():
        assert await observe(session, hotel, "late-calendar-result", hint()) == 0
        assert await counts(
            session, CalendarHintHistory, PriceAlert, PriceHistory, StayScan, NotificationLog
        ) == (
            0,
            0,
            0,
            0,
            0,
        )
        assert await session.get(AppSetting, f"calendar_hint_success:{hotel_id}:2030-11") is None


@pytest.mark.parametrize("changed", ["accepted_source", "hotel_source", "disabled_hotel"])
async def test_inflight_first_room_quote_cannot_match_after_source_or_hotel_change(sessions, changed):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        assert await admit_calendar_candidates(session, settings()) == 1
        job = await session.get(CrawlJob, alert.payload["quote_job_id"])
        alert_id, hotel_id = alert.id, hotel.id
    async with sessions() as session, session.begin():
        if changed == "accepted_source":
            catalog = await session.get(AppSetting, "catalog:marriott")
            catalog.value = {"root": EN_ROOT}
        else:
            changed_hotel = await session.get(Hotel, hotel_id)
            if changed == "hotel_source":
                changed_hotel.official_url = "https://www.marriott.com/en-us/hotels/pbhmd-offline/overview/"
            else:
                changed_hotel.active = False
    async with sessions() as session, session.begin():
        assert await match_calendar_candidates(session, hotel, job, [quote()]) == 0
        alert = await session.get(PriceAlert, alert_id)
        assert alert.payload["verification"] == "CANCELLED"
        assert not alert.confirmed
        assert await counts(session, NotificationLog) == (0,)


async def test_first_quote_bound_to_wrong_source_job_is_cancelled_without_reuse(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        existing = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=STAY,
                check_out=STAY + timedelta(days=1),
                payload={SOURCE_MARKER: EN_ROOT},
            ),
        )
        alert.payload = {**alert.payload, "quote_job_id": existing.id}
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        assert alert.payload["verification"] == "CANCELLED"
        assert alert.payload["verification_reason"] == "QUOTE_SOURCE_CHANGED"
        assert await counts(session, CrawlJob, NotificationLog) == (1, 0)


async def test_first_quote_for_different_hotel_cannot_support_calendar_signal(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        assert await admit_calendar_candidates(session, settings()) == 1
        job = await session.get(CrawlJob, alert.payload["quote_job_id"])
        assert await match_calendar_candidates(session, hotel, job, [quote(provider_hotel_id="PBHPR")]) == 0
        assert alert.payload["verification"] == "REJECTED"
        assert await counts(session, NotificationLog) == (0,)


@pytest.mark.parametrize("change", ["disabled", "hotel_source", "hotel_code"])
async def test_final_calendar_confirmation_rechecks_current_hotel(sessions, change):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        await first_room_quote(session, hotel, alert)
        assert await admit_calendar_candidates(session, settings()) == 1
        verification = await session.get(CrawlJob, alert.verification_job_id)
        if change == "disabled":
            hotel.active = False
        elif change == "hotel_source":
            hotel.official_url = "https://www.marriott.com/hotels/other"
        else:
            hotel.provider_hotel_id = "OTHER"
        await session.flush()
        assert (
            await confirm_job_drops(session, verification, [quote(captured=NOW + timedelta(seconds=65))]) == 0
        )
        assert not alert.confirmed and alert.payload["verification"] == "REJECTED"
        assert await counts(session, NotificationLog) == (0,)


async def test_live_bound_first_hundred_candidates_do_not_starve_later_signal_after_restart(sessions):
    async with sessions() as session, session.begin():
        hotel, newest = await seed_candidate(session)
        newest.id = "candidate-100"
        newest.created_at = NOW
        await session.flush()
        for index in range(100):
            other = Hotel(
                id=f"hotel-{index:03}",
                provider="marriott",
                provider_hotel_id=f"A{index:04}",
                hotel_name=f"Other calendar hotel {index}",
                official_url=SOURCE,
            )
            session.add(other)
            await session.flush()
            bound = CrawlJob(
                id=f"bound-{index:03}",
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=other.id,
                check_in=STAY,
                check_out=STAY + timedelta(days=1),
                dedupe_key=f"live-bound-{index}",
                status="PENDING",
                payload={SOURCE_MARKER: ROOT},
            )
            session.add(bound)
            session.add(
                PriceAlert(
                    id=f"candidate-{index:03}",
                    hotel_id=other.id,
                    offer_key=f"series-{index}",
                    event_type=SIGNAL,
                    anomaly_score=0,
                    confirmed=False,
                    dedupe_key=f"signal-{index}",
                    created_at=NOW - timedelta(hours=1) + timedelta(seconds=index),
                    payload={**newest.payload, "quote_job_id": bound.id},
                )
            )
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        assert "quote_job_id" not in newest.payload
        assert (await session.get(AppSetting, CANDIDATE_CURSOR)).value == {"alert_id": "candidate-099"}
    async with sessions() as session, session.begin():
        assert await admit_calendar_candidates(session, settings()) == 1
        newest = await session.get(PriceAlert, "candidate-100")
        assert (await session.get(CrawlJob, newest.payload["quote_job_id"])).hotel_id == hotel.id
        assert await counts(session, CrawlJob, NotificationLog) == (101, 0)


@pytest.mark.parametrize("first_observed", [False, True])
@pytest.mark.parametrize("invalid", ["expired", "hotel_disabled", "source_changed"])
async def test_invalid_waiting_candidates_are_retired_even_while_provider_paused(
    sessions, monkeypatch, first_observed, invalid
):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        if first_observed:
            await first_room_quote(session, hotel, alert)
        now = NOW
        if invalid == "expired":

            class LaterDate(date):
                @classmethod
                def today(cls):
                    return STAY + timedelta(days=1)

            now = datetime.combine(STAY + timedelta(days=1), NOW.time(), timezone.utc)
            monkeypatch.setattr(jobs, "date", LaterDate)
            monkeypatch.setattr(calendar_hints, "utcnow", lambda: now)
        elif invalid == "hotel_disabled":
            hotel.active = False
        else:
            catalog = await session.get(AppSetting, "catalog:marriott")
            catalog.value = {"root": EN_ROOT}
        session.add(
            ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=now + timedelta(hours=1))
        )
        await session.flush()
        assert await admit_calendar_candidates(session, settings()) == 0
        assert alert.payload["verification"] == "CANCELLED"
        assert await counts(session, NotificationLog) == (0,)


async def test_global_disable_preserves_waiting_signal_without_admission_or_cleanup(sessions):
    async with sessions() as session, session.begin():
        hotel, alert = await seed_candidate(session)
        hotel.active = False
        await session.flush()
        assert await admit_calendar_candidates(session, settings(global_monitoring_enabled=False)) == 0
        assert alert.payload["verification"] == WAIT_QUOTE
        assert await counts(session, CrawlJob, NotificationLog) == (0, 0)
