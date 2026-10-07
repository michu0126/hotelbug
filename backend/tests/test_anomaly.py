"""Offline anomaly and historical-low acceptance; no official requests or live Telegram."""

from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from test_alerts import quote, seed_history
from test_pipeline import FixtureProvider

from app.core.anomaly_config import AnomalyPolicy
from app.core.config import Settings
from app.models.tables import CrawlJob, NotificationLog, PriceAlert, PriceStatistics, utcnow
from app.services.alerts import confirm_drop, confirm_job_drops, detect_drops
from app.services.anomaly import anomaly_score, calendar_features
from app.services.notifications import telegram_text
from app.services.rates import persist_rates, upsert_hotel
from app.services.runtime_settings import (
    MonitoringInput,
    load_runtime_settings,
    public_monitoring_settings,
    save_runtime_settings,
)


def analysis(**changes):
    return {"baseline": "100", "drop_fraction": "0.5", "historical_low": None, **changes}


@pytest.mark.parametrize(
    "amount,band,weight",
    [
        ("80", None, 0),
        ("79.99", "drop_20", 10),
        ("70", "drop_20", 10),
        ("69.99", "drop_30", 15),
        ("59.99", "drop_40", 20),
        ("50", "drop_40", 20),
        ("49.99", "drop_50", 25),
        ("30", "drop_50", 25),
        ("29.99", "drop_70", 30),
    ],
)
def test_only_one_drop_band_with_exact_decimal_boundaries(amount, band, weight):
    score, level, contributions = anomaly_score(Decimal(amount), analysis(), AnomalyPolicy())
    assert score == weight and level == "NORMAL"
    assert contributions == ({band: weight} if band else {})


def test_evidence_is_exclusive_and_confirmation_adds_weight_once():
    features = analysis(
        historical_low="90",
        neighbor_date_median="100",
        medians={"7": "100", "30": "100"},
        history_counts={"7": 6, "30": 6},
    )
    score, level, parts = anomaly_score(Decimal("29"), features, AnomalyPolicy(), confirmed=True)
    assert score == 90 and level == "POSSIBLE_BUG_PRICE"
    assert parts == {"drop_70": 30, "new_low": 20, "neighbor": 15, "consistent_history": 15, "verified": 10}
    unconfirmed, _, before = anomaly_score(Decimal("29"), features, AnomalyPolicy())
    assert unconfirmed == 80 and "verified" not in before and "near_low" not in parts
    features["history_counts"]["7"] = 5
    assert "consistent_history" not in anomaly_score(Decimal("29"), features, AnomalyPolicy())[2]


def test_weights_levels_clamping_and_near_low_are_configurable():
    custom = AnomalyPolicy(
        drop_70=100,
        new_low=100,
        verified=100,
        low_threshold=10,
        very_low_threshold=20,
        possible_bug_threshold=30,
    )
    assert anomaly_score(Decimal("29"), analysis(historical_low="90"), custom, confirmed=True)[:2] == (
        100,
        "POSSIBLE_BUG_PRICE",
    )
    _, _, parts = anomaly_score(Decimal("51"), analysis(historical_low="50"), AnomalyPolicy())
    assert parts == {"drop_40": 20, "near_low": 10}
    assert "near_low" not in anomaly_score(Decimal("53"), analysis(historical_low="50"), AnomalyPolicy())[2]


@pytest.mark.parametrize(
    "changes",
    [
        {"drop_70": -1},
        {"verified": 101},
        {"support_min_days": 2},
        {"neighbor_drop_fraction": "NaN"},
        {"near_low_margin": "1"},
        {"calendar_max_age_hours": 0},
        {"low_threshold": 80},
        {"invented_weight": 30},
    ],
)
def test_invalid_policy_cannot_be_saved(changes):
    with pytest.raises(ValidationError):
        MonitoringInput(anomaly_policy=changes)


async def test_calendar_comparisons_require_same_conditions_freshness_and_both_neighbors(sessions):
    moment = utcnow()
    original = quote("70", moment)
    rate = original.model_copy(
        update={
            "check_in": original.check_in + timedelta(days=7),
            "check_out": original.check_out + timedelta(days=7),
        }
    )
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        for offset, price in ((-1, "200"), (1, "220"), (-7, "180"), (7, "220")):
            await persist_rates(
                db,
                hotel,
                [
                    quote(
                        price,
                        moment - timedelta(hours=1),
                        check_in=rate.check_in + timedelta(days=offset),
                        check_out=rate.check_out + timedelta(days=offset),
                    )
                ],
                f"calendar-{offset}",
            )
        exclusions = [
            {"currency": "EUR"},
            {"room_code": "SUITE"},
            {"rate_code": "OTHER"},
            {"member_rate": True},
            {"refundable": None},
            {"breakfast_included": True},
            {"adults": 1},
            {"rooms": 2},
            {"price_basis": "nightly"},
            {"total_price": None, "cash_price": Decimal("1")},
            {"points_price": 1000},
            {"availability": False},
            {"check_out": rate.check_out + timedelta(days=3)},
        ]
        for i, change in enumerate(exclusions):
            await persist_rates(
                db,
                hotel,
                [
                    quote(
                        "1",
                        moment - timedelta(hours=1),
                        **{
                            "check_in": rate.check_in + timedelta(days=2),
                            "check_out": rate.check_out + timedelta(days=2),
                            **change,
                        },
                    )
                ],
                f"excluded-{i}",
            )
        for offset, captured in ((3, moment - timedelta(hours=73)), (4, moment + timedelta(seconds=1))):
            await persist_rates(
                db,
                hotel,
                [
                    quote(
                        "1",
                        captured,
                        check_in=rate.check_in + timedelta(days=offset),
                        check_out=rate.check_out + timedelta(days=offset),
                    )
                ],
                f"time-{offset}",
            )
        other = await upsert_hotel(db, await FixtureProvider().get_hotel_details("another-hotel"))
        await persist_rates(
            db,
            other,
            [
                quote(
                    "1",
                    moment - timedelta(hours=1),
                    provider_hotel_id="another-hotel",
                    check_in=rate.check_in + timedelta(days=2),
                    check_out=rate.check_out + timedelta(days=2),
                )
            ],
            "other-hotel",
        )
        result = await calendar_features(db, hotel, rate, AnomalyPolicy())
        assert Decimal(result["neighbor_date_median"]) == 210 and result["neighbor_days"] == 2
        assert Decimal(result["same_weekday_median"]) == 200 and result["same_weekday_days"] == 2
        assert Decimal(result["month_mean"]) == 205 and result["month_days"] == 4


async def test_single_neighbor_does_not_invent_two_sided_calendar_evidence(sessions):
    rate = quote("70")
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        await persist_rates(
            db,
            hotel,
            [
                quote(
                    "200",
                    rate.captured_at - timedelta(minutes=1),
                    check_in=rate.check_in + timedelta(days=1),
                    check_out=rate.check_out + timedelta(days=1),
                )
            ],
            "one-neighbor",
        )
        result = await calendar_features(db, hotel, rate, AnomalyPolicy())
        assert result["neighbor_days"] == 1 and result["neighbor_date_median"] is None
        assert result["same_weekday_median"] is None


async def test_modest_historical_new_low_is_independently_rechecked_not_labeled_bug(sessions):
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        assert await detect_drops(db, hotel, [quote("180")], Settings()) == 1
        alert = await db.scalar(select(PriceAlert))
        assert alert.event_type == "NEW_HISTORICAL_LOW" and not alert.confirmed
        assert alert.payload["historical_low"] == "190.0000"
        assert alert.payload["anomaly_level"] == "NORMAL"
        assert "verified" not in alert.payload["score_breakdown"]
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 0
        assert not await confirm_drop(db, alert.id, alert.verification_job_id, [quote("190")])
        assert alert.payload["verification"] == "REJECTED"
        assert await detect_drops(db, hotel, [quote("180")], Settings()) == 1
        second = await db.scalar(select(PriceAlert).where(PriceAlert.id != alert.id))
        assert await confirm_drop(db, second.id, second.verification_job_id, [quote("185")])
        assert second.payload["anomaly_score"] == 30
        assert second.payload["score_breakdown"]["verified"] == 10
        message = telegram_text(second.payload)
        for field in (
            "酒店历史新低提醒",
            "Fixture hotel",
            "万豪 Marriott",
            "2027-01-07",
            "185 USD",
            "历史最低",
            "30 / 100",
        ):
            assert field in message
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 1


async def test_new_lows_toggle_and_all_time_reference_not_ninety_day_minimum(sessions):
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        assert (
            await detect_drops(db, hotel, [quote("180")], Settings(alert_historical_lows_enabled=False)) == 0
        )
        await persist_rates(db, hotel, [quote("50", utcnow() - timedelta(days=100))], "ancient-low")
        assert await detect_drops(db, hotel, [quote("180")], Settings()) == 0
        stats = await db.get(PriceStatistics, quote("180").offer_key())
        assert stats.historical_low == Decimal("50")


async def test_points_plus_cash_does_not_provide_public_cash_baseline_or_alert(sessions):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, await FixtureProvider().get_hotel_details("fixture-only"))
        for day in (1, 2, 3):
            await persist_rates(
                db, hotel, [quote("200", utcnow() - timedelta(days=day), points_price=1000)], f"points-{day}"
            )
        assert await detect_drops(db, hotel, [quote("70")], Settings()) == 0
        assert await detect_drops(db, hotel, [quote("70", points_price=1000)], Settings()) == 0
        assert await db.scalar(select(func.count()).select_from(PriceAlert)) == 0


async def test_all_time_record_can_notify_without_a_recent_median(sessions):
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(db, await FixtureProvider().get_hotel_details("fixture-only"))
        for age in (100, 101, 102):
            await persist_rates(db, hotel, [quote("200", utcnow() - timedelta(days=age))], f"old-{age}")
        assert await detect_drops(db, hotel, [quote("180")], Settings()) == 1
        alert = await db.scalar(select(PriceAlert))
        assert alert.event_type == "NEW_HISTORICAL_LOW"
        assert alert.payload["baseline_window_days"] is None
        assert alert.payload["baseline_kind"] == "PREVIOUS_HISTORICAL_LOW"
        assert alert.payload["history_days"] == 3
        assert all(v is None for v in alert.payload["analysis"]["medians"].values())
        assert await confirm_drop(db, alert.id, alert.verification_job_id, [quote("180")])


async def test_large_drop_and_new_low_have_one_candidate_one_query_one_outbox(sessions):
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        assert await detect_drops(db, hotel, [quote("70")], Settings()) == 1
        alert = await db.scalar(select(PriceAlert))
        assert alert.event_type == "PRICE_DROP" and alert.payload["new_historical_low"]
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 1
        job = await db.get(CrawlJob, alert.verification_job_id)
        assert await confirm_job_drops(db, job, [quote("65")]) == 1
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 1
        assert Decimal(alert.payload["analysis"]["drop_from_median_30d_percent"]) == Decimal("67.50")
        assert alert.payload["score_breakdown"]["verified"] == 10
        assert (await db.get(PriceStatistics, alert.offer_key)).historical_low == Decimal("70")


async def test_scoring_never_gates_valid_drop_notifications_and_policy_is_frozen(sessions):
    zeros = {
        key: 0
        for key in (
            "drop_20",
            "drop_30",
            "drop_40",
            "drop_50",
            "drop_70",
            "neighbor",
            "near_low",
            "new_low",
            "verified",
            "consistent_history",
        )
    }
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        settings = Settings(anomaly_policy=AnomalyPolicy(**zeros))
        assert await detect_drops(db, hotel, [quote("70")], settings) == 1
        alert = await db.scalar(select(PriceAlert))
        await save_runtime_settings(db, MonitoringInput(anomaly_policy=AnomalyPolicy(verified=100)))
        assert await confirm_drop(db, alert.id, alert.verification_job_id, [quote("70")])
        assert alert.anomaly_score == 0 and alert.payload["anomaly_level"] == "NORMAL"
        assert await db.scalar(select(func.count()).select_from(NotificationLog)) == 1


async def test_scoring_policy_and_historical_switch_are_database_backed(sessions):
    policy = AnomalyPolicy(drop_70=35, neighbor_drop_fraction="0.4", low_threshold=45)
    async with sessions() as db, db.begin():
        await save_runtime_settings(
            db, MonitoringInput(anomaly_policy=policy, alert_historical_lows_enabled=False)
        )
    async with sessions() as db, db.begin():
        await save_runtime_settings(db, MonitoringInput(alert_confirmation_seconds=30))
    async with sessions() as db:
        base = Settings()
        restored = await load_runtime_settings(db, base)
        assert restored.anomaly_policy == policy and not restored.alert_historical_lows_enabled
        assert base.anomaly_policy.drop_70 == 30 and base.alert_historical_lows_enabled
        public = public_monitoring_settings(restored, set())
        assert public["anomaly_policy"]["neighbor_drop_fraction"] == "0.4"
        assert not public["alert_historical_lows_enabled"]
