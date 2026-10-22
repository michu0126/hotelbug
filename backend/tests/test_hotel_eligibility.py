"""Conservative QA exclusion without deleting official catalog/history or watches."""

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import select
from test_catalog_price_source import config, expand, seed, set_source

from app.crawler import worker
from app.models.tables import CrawlJob, Hotel, Watchlist, utcnow
from app.providers.marriott_cn_catalog import ROOT
from app.schemas.domain import JobKind
from app.services import hotel_eligibility as policy
from app.services.calendar_hint_jobs import expand_calendar_hints
from app.services.jobs import LIVE
from app.services.provider_health import marriott_probe_hotel
from app.services.watchlists import expand_watchlists


@pytest.mark.parametrize(
    "provider,code,name,excluded",
    [
        ("marriott", "AQABA", "BA Growth Test Hotel", True),
        ("marriott", "aqacy", "CY赋能培训酒店", True),
        ("marriott", "AQAES", "TEST-Residence Inn Hotel for Alpha", True),
        ("marriott", "AQAIF", "BRT IF FOR TESTING ONLY", True),
        ("marriott", "AQAMC", "EMPOWER Training Hotel Marriott", True),
        ("marriott", "AQAMV", "MV Empower", True),
        ("marriott", "AQAOF", "测试 - Alpha万豪酒店", True),
        ("marriott", "AQASI", "BRT SI FOR TESTING ONLY", True),
        ("marriott", "AQAXF", "XF Growth Test Hotel", True),
        ("marriott", "AQAXN", "CE Growth Test Hotel", True),
        ("marriott", "AQA01", "Testaccio Grand Hotel", False),
        ("marriott", "AQA02", "Greatest Hotel", False),
        ("marriott", "AQA03", "Hotel by Trainington", False),
        ("marriott", "AQA04", "Normal Official Hotel", False),
        ("marriott", "NYCMQ", "Training Hotel", False),
        ("marriott", "AQABA1", "Test Hotel", False),
        ("ihg", "AQABA", "Test Hotel", False),
    ],
)
async def test_sql_and_python_policy_agree_without_substring_false_positives(
    sessions, provider, code, name, excluded
):
    hotel = Hotel(
        provider=provider,
        provider_hotel_id=code,
        hotel_name=name,
        official_url="https://www.marriott.com.cn/",
    )
    assert policy.is_test_hotel(hotel) is excluded
    async with sessions() as db, db.begin():
        db.add(hotel)
        await db.flush()
        matches = (await db.scalars(select(Hotel).where(policy.test_hotel_predicate()))).all()
        assert bool(matches) is excluded


@pytest.mark.parametrize("lane", ["global", "recheck", "failed", "monthly", "probe"])
async def test_qa_filter_precedes_rotation_limit_and_keeps_catalog_and_history(sessions, lane):
    async with sessions() as db, db.begin():
        await set_source(db, ROOT)
        for i in range(10):
            hotel = await seed(
                db, f"a-qa-{i}", ROOT, lane if lane in ("global", "recheck", "failed") else "global"
            )
            hotel.provider_hotel_id = f"AQA{i:02}"
            hotel.hotel_name = "Official Training Hotel"
        await seed(db, "z-real", ROOT, lane if lane in ("global", "recheck", "failed") else "global")
        if lane == "monthly":
            assert await expand_calendar_hints(db, config()) == 1
        elif lane == "probe":
            assert (await marriott_probe_hotel(db)).id == "z-real"
        else:
            assert await expand(db, lane) == 1
        if lane != "probe":
            jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status.in_(LIVE)))).all()
            assert len(jobs) == 1 and jobs[0].hotel_id == "z-real"
        assert len((await db.scalars(select(Hotel))).all()) == 11
        assert (await db.get(Hotel, "a-qa-0")).active
        if lane == "failed":
            assert (await db.get(CrawlJob, "failure-a-qa-0")).status == "FAILED"


@pytest.mark.parametrize(
    "kind,payload,excluded",
    [
        (JobKind.FETCH_RATE, {}, False),
        (JobKind.FETCH_RATE, {"automatic_catalog_root": ROOT}, True),
        (JobKind.FETCH_RATE, {"connection_test": True, "automatic_catalog_root": ROOT}, False),
        (JobKind.VERIFY_ANOMALY, {"calendar_signal": True}, True),
        (JobKind.FETCH_CALENDAR_HINTS, {"month": "2026-11"}, True),
        (JobKind.PROVIDER_HEALTHCHECK, {"scheduled_probe": True}, True),
        (JobKind.PROVIDER_HEALTHCHECK, {}, False),
        (JobKind.DISCOVER_HOTELS, {}, False),
    ],
)
def test_only_identifiably_automatic_price_jobs_are_retired(kind, payload, excluded):
    hotel = SimpleNamespace(
        provider="marriott", provider_hotel_id="AQAMC", hotel_name="EMPOWER Training Hotel"
    )
    assert policy.automatic_job_excluded(SimpleNamespace(kind=kind, payload=payload), hotel) is excluded


async def test_explicit_watch_is_preserved_and_unmarked(sessions):
    async with sessions() as db, db.begin():
        hotel = await seed(db, "qa", ROOT, "global")
        hotel.provider_hotel_id, hotel.hotel_name = "AQAMC", "EMPOWER Training Hotel"
        db.add(
            Watchlist(
                id="watch-qa",
                name="Explicit",
                scope="hotel",
                filters={"hotel_id": hotel.id, "days_ahead": 365},
            )
        )
        assert await expand_watchlists(db, config()) == 1
        job = await db.scalar(select(CrawlJob))
        assert not policy.automatic_job_excluded(job, hotel)
        assert (await db.get(Watchlist, "watch-qa")).enabled


async def test_worker_cancels_old_automatic_qa_before_opening_browser_preserving_error(
    sessions, queue, monkeypatch
):
    factory = Mock(side_effect=AssertionError("QA hotel must not open a browser"))
    monkeypatch.setattr(worker, "create_provider", factory)
    async with sessions() as db, db.begin():
        hotel = await seed(db, "qa", ROOT, "global")
        hotel.provider_hotel_id, hotel.hotel_name = "AQAMC", "EMPOWER Training Hotel"
        db.add(
            CrawlJob(
                id="qa-job",
                provider="marriott",
                hotel_id=hotel.id,
                kind=JobKind.FETCH_RATE,
                check_in=date.today() + timedelta(days=2),
                check_out=date.today() + timedelta(days=3),
                payload={"automatic_catalog_root": ROOT},
                dedupe_key="qa-job",
                status="QUEUED",
                error_type="TIMEOUT",
                error_message="Original failure",
                retry_count=2,
                scheduled_at=utcnow() - timedelta(seconds=1),
            )
        )
    await queue.put("qa-job", 50)
    assert await worker.process_one(sessions, queue, config())
    async with sessions() as db:
        job = await db.get(CrawlJob, "qa-job")
        assert job.status == "CANCELLED" and job.error_type == "TIMEOUT" and job.retry_count == 2
        assert job.error_message == "Original failure; " + policy.EXCLUDED_REASON
        assert (await db.get(Hotel, "qa")).active
    factory.assert_not_called()
