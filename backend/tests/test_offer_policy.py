"""Offline plan classification; synthetic labels are NOT new official observations."""

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from test_alerts import quote, seed_history
from test_domain import fixture_rate
from test_hilton_page import parse, parse_english
from test_ihg_page import SEARCH, URL, parse_room, room_html
from test_pipeline import FixtureProvider

from app.api.main import app
from app.core.config import Settings
from app.crawler.worker import process_one
from app.database.session import get_session
from app.models.tables import CrawlJob, NotificationLog, PriceAlert, PriceHistory, Rate, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.alerts import confirm_drop, detect_drops
from app.services.calendar import get_calendar
from app.services.history import get_stay_offers
from app.services.jobs import enqueue
from app.services.offer_policy import classify_offer
from app.services.rates import persist_rates, upsert_hotel


@pytest.mark.parametrize(
    "label,tag",
    [
        ("Corporate Rate", "CORPORATE_RATE"),
        ("Company Negotiated Rate", "CORPORATE_RATE"),
        ("Negotiated Rate · Public rate", "CORPORATE_RATE"),
        ("公司协议价", "CORPORATE_RATE"),
        ("企業協議價", "CORPORATE_RATE"),
        ("Advance Purchase Breakfast Included", "ADVANCE_PURCHASE"),
        ("Book Early & Save", "ADVANCE_PURCHASE"),
        ("Book Early and Save Breakfast", "ADVANCE_PURCHASE"),
        ("Early Bird Rate", "ADVANCE_PURCHASE"),
        ("提前預付", "ADVANCE_PURCHASE"),
        ("提前购买价", "ADVANCE_PURCHASE"),
        ("Spa Package", "PACKAGE"),
        ("Bed & Breakfast Package · Non-Member Rate", "PACKAGE"),
        ("周末套餐", "PACKAGE"),
        ("Points + Cash", "POINTS_PLUS_CASH"),
        ("積分加現金", "POINTS_PLUS_CASH"),
        ("Marriott Bonvoy Member Rate", "MEMBER_RATE"),
        ("Members Only Discount", "MEMBER_RATE"),
        ("会员专享价", "MEMBER_RATE"),
        ("會員優惠", "MEMBER_RATE"),
        ("SADC Local Residence Special Including Breakfast", "RESIDENT_RATE"),
        ("Florida Resident Rate", "RESIDENT_RATE"),
        ("Local Residents Offer", "RESIDENT_RATE"),
        ("当地居民优惠", "RESIDENT_RATE"),
        ("當地居民專享", "RESIDENT_RATE"),
    ],
)
def test_explicit_special_labels(label, tag):
    result = classify_offer(fixture_rate().model_copy(update={"rate_name": label, "member_rate": False}))
    assert result.tags == (tag,)
    assert not result.alert_eligible
    assert result.evidence[tag].startswith("rate_name:")


@pytest.mark.parametrize(
    "label",
    [
        "Best Flexible Rate · Non-Member Rate",
        "NON–MEMBER RATE",  # typography, not a membership restriction
        "Non Member Rate",
        "非会员价",
        "非會員價",
        "Breakfast Included",
        "IHG Flexible Saver Breakfast",
        "Semi-Flex",
        "SAVER RATE · Public rate",
        "Business Traveller Offer",  # not evidence of a company agreement
        "Avani Flexi",
        "Pay now",  # not alone proof of an advance-purchase plan
        "Room only",
        "Packageway special",  # word boundary, not an explicit package
        "Summer Discount",  # no whitelist of ordinary public plans
        "Garden Residence Breakfast",  # residence room wording is not an eligibility restriction
        "Resident Artist Experience",  # not a resident-only rate label
    ],
)
def test_unknown_or_public_labels_are_not_over_defensively_blocked(label):
    rate = fixture_rate().model_copy(
        update={"rate_name": label, "member_rate": None, "refundable": False, "breakfast_included": True}
    )
    assert classify_offer(rate).as_dict() == {"alert_eligible": True, "tags": [], "evidence": {}}


def test_only_plan_name_and_explicit_flags_not_rate_codes_or_room_names():
    rate = fixture_rate().model_copy(
        update={"rate_name": "Best Flexible Rate", "rate_code": "IGCOR", "room_type": "Corporate Suite"}
    )
    original_key = rate.offer_key()
    assert classify_offer(rate).alert_eligible
    assert rate.offer_key() == original_key
    # An explicitly true flag wins over a contradictory Non-Member label.
    explicit = rate.model_copy(update={"member_rate": True, "rate_name": "Non-Member Rate"})
    assert classify_offer(explicit).evidence == {"MEMBER_RATE": "member_rate=true"}


def test_multiple_special_conditions_are_explained_without_changing_price():
    rate = fixture_rate().model_copy(
        update={"rate_name": "Member Rate · Advance Purchase Spa Package", "points_price": 5000}
    )
    before = rate.model_dump()
    result = classify_offer(rate)
    assert set(result.tags) == {"MEMBER_RATE", "ADVANCE_PURCHASE", "PACKAGE", "POINTS_PLUS_CASH"}
    assert result.evidence["POINTS_PLUS_CASH"] == "points_price_present"
    assert rate.model_dump() == before
    points = rate.model_copy(update={"cash_price": None, "total_price": None, "rate_name": "Standard"})
    assert classify_offer(points).tags == ("POINTS_ONLY",)
    unknown = rate.model_copy(update={"room_type": None, "room_code": None, "points_price": None})
    assert "UNIDENTIFIED_OFFER" in classify_offer(unknown).tags


def test_existing_observed_ihg_and_hilton_projections_keep_all_quotes_but_classify_special_plans():
    ihg = parse_room(room_html(), SEARCH, URL, "OAAN")
    assert len(ihg) == 6
    assert [classify_offer(rate).alert_eligible for rate in ihg] == [False, True, False, True, True, True]
    # IHG IGCOR is Best Flexible Rate, not evidence of Corporate Rate.
    assert ihg[3].rate_code == "IGCOR" and classify_offer(ihg[3]).alert_eligible
    hilton = parse_english()
    assert len(hilton) == 6
    assert [classify_offer(rate).alert_eligible for rate in hilton] == [True, True, False, True, True, False]
    chinese = parse()
    assert classify_offer(chinese[2]).tags == ("ADVANCE_PURCHASE",)


@pytest.mark.parametrize(
    "label",
    [
        "Corporate Rate",
        "Advance Purchase",
        "Spa Package",
        "Member Rate",
        "SADC Local Residence Special Including Breakfast",
    ],
)
async def test_special_plans_are_saved_and_readable_but_create_no_alert_or_recheck(sessions, label):
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
        for day in (1, 2, 3):
            old = quote("200", utcnow() - timedelta(days=day), rate_name=label)
            await persist_rates(session, hotel, [old], f"history-{day}")
        current = quote("20", rate_name=label)
        await persist_rates(session, hotel, [current], "current")
        assert await detect_drops(session, hotel, [current], Settings()) == 0
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 4
        assert await session.scalar(select(func.count()).select_from(Rate)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 0
        assert await session.scalar(select(func.count()).select_from(CrawlJob)) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
        offers = await get_stay_offers(session, hotel, current.check_in)
        assert len(offers["offers"]) == 1
        assert not offers["offers"][0]["offer_classification"]["alert_eligible"]
        day = (await get_calendar(session, hotel, current.check_in.strftime("%Y-%m")))["days"][6]
        assert day["status"] == "AVAILABLE" and Decimal(day["current_low"]) == 20
        assert not day["offer_classification"]["alert_eligible"]


async def test_public_breakfast_nonrefundable_unknown_label_still_detects_and_confirms(sessions):
    async with sessions() as session, session.begin():
        hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
        changes = {"rate_name": "Summer Breakfast Offer · Non-Member Rate", "refundable": False}
        for day in (1, 2, 3):
            await persist_rates(
                session, hotel, [quote("200", utcnow() - timedelta(days=day), **changes)], str(day)
            )
        low = quote("20", **changes)
        assert await detect_drops(session, hotel, [low], Settings()) == 1
        alert = await session.scalar(select(PriceAlert))
        assert alert.payload["offer_classification"]["alert_eligible"]
        assert await confirm_drop(session, alert.id, alert.verification_job_id, [low])
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 1


@pytest.mark.parametrize("points", [0, 5000])
async def test_recheck_cannot_confirm_mixed_points_cash_with_legacy_cash_identity(sessions, points):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        low = quote("20")
        assert await detect_drops(session, hotel, [low], Settings()) == 1
        alert = await session.scalar(select(PriceAlert))
        mixed = low.model_copy(update={"points_price": points})
        assert mixed.offer_key() == low.offer_key()  # legacy key alone is insufficient
        assert not await confirm_drop(session, alert.id, alert.verification_job_id, [mixed])
        assert alert.payload["verification"] == "REJECTED"
        assert alert.payload["verification_reason"] == "EXCLUDED_OFFER"
        assert alert.payload["offer_classification"]["tags"] == ["POINTS_PLUS_CASH"]
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0


@pytest.mark.parametrize(
    "label,tag",
    [
        ("Advance Purchase", "ADVANCE_PURCHASE"),
        ("SADC Local Residence Special Including Breakfast", "RESIDENT_RATE"),
    ],
)
async def test_legacy_special_candidate_is_rejected_at_confirmation(sessions, label, tag):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        low = quote("20", rate_name=label)
        alert = PriceAlert(
            hotel_id=hotel.id,
            offer_key=low.offer_key(),
            event_type="PRICE_DROP",
            anomaly_score=45,
            confirmed=False,
            dedupe_key="offline-old-special",
            verification_job_id="old-job",
            payload={
                "threshold": "100",
                "baseline": "200",
                "price_field": "total_price",
                "verification": "PENDING",
            },
        )
        session.add(alert)
        await session.flush()
        assert not await confirm_drop(session, alert.id, "old-job", [low])
        assert alert.payload["offer_classification"]["tags"] == [tag]
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0


@pytest.mark.parametrize("first_change", [{"points_price": 5000}, {"availability": False}])
async def test_first_ineligible_duplicate_matches_persistence_and_cannot_be_replaced(sessions, first_change):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        low = quote("20")
        first = low.model_copy(update=first_change)
        assert first.offer_key() == low.offer_key()
        assert await persist_rates(session, hotel, [first, low], "duplicate-job") == 1
        assert await detect_drops(session, hotel, [first, low], Settings()) == 0
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 0
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 0


async def test_detail_and_history_api_return_explanation_without_removing_special_quote(sessions):
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await persist_rates(session, hotel, [quote("20", rate_name="Spa Package")], "package")
        hotel_id = hotel.id

    async def dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            detail = (await client.get(f"/api/hotels/{hotel_id}")).json()
            package = next(row for row in detail["latest_rates"] if row["rate_name"] == "Spa Package")
            assert package["offer_classification"]["tags"] == ["PACKAGE"]
            history = (await client.get(f"/api/hotels/{hotel_id}/history")).json()
            assert any(row["offer_classification"]["tags"] == ["PACKAGE"] for row in history)
    finally:
        app.dependency_overrides.clear()


async def test_worker_keeps_special_and_public_quotes_but_rechecks_only_eligible_plan(
    sessions, queue, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    public, package = quote("70"), quote("20", rate_name="Spa Package")

    class MixedPlans(FixtureProvider):
        async def search_rates(self, request):
            return [package, public]

    monkeypatch.setitem(FACTORIES, "marriott", MixedPlans)
    settings = Settings()
    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        for day in (1, 2, 3):
            await persist_rates(
                session,
                hotel,
                [quote("200", utcnow() - timedelta(days=day), rate_name="Spa Package")],
                f"package-old-{day}",
            )
        job = await enqueue(
            session,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=public.check_in,
                check_out=public.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    assert await process_one(sessions, queue, settings)
    async with sessions() as session, session.begin():
        assert (await session.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 8
        assert await session.scalar(select(func.count()).select_from(Rate)) == 2
        assert await session.scalar(select(func.count()).select_from(PriceAlert)) == 1
        alert = await session.scalar(select(PriceAlert))
        assert alert.offer_key == public.offer_key()
        recheck = await session.get(CrawlJob, alert.verification_job_id)
        assert recheck.payload["alert_ids"] == [alert.id]
        recheck.scheduled_at = utcnow() - timedelta(seconds=1)
        recheck_id = recheck.id
    await queue.redis.delete("hotelbug:limit:marriott")
    await queue.put(recheck_id, 100)
    assert await process_one(sessions, queue, settings)
    async with sessions() as session:
        assert (await session.scalar(select(PriceAlert))).confirmed
        assert await session.scalar(select(func.count()).select_from(NotificationLog)) == 1
        assert await session.scalar(select(func.count()).select_from(PriceHistory)) == 10
