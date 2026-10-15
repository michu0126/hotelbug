"""Selected sources constrain future automatic jobs, not old data or explicit watches."""

from datetime import date, timedelta

import pytest
from sqlalchemy import select
from test_pipeline import FixtureProvider

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    PriceHistory,
    ProviderStatus,
    StayScan,
    Watchlist,
    utcnow,
)
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind, ProviderName
from app.services.catalog_price_source import SOURCE_MARKER, load_catalog_price_source
from app.services.failed_stays import expand_failed_stays
from app.services.jobs import enqueue
from app.services.rechecks import expand_successful_rechecks
from app.services.watchlists import expand_global, expand_watchlists


def config(provider="marriott", budget=1):
    return Settings(
        _env_file=None,
        global_jobs_per_tick=budget,
        max_pending_jobs=100,
        provider_overrides={p.value: ProviderPolicy(enabled=p.value == provider) for p in ProviderName},
    )


async def set_source(db, root):
    state = await db.get(AppSetting, "catalog:marriott")
    value = {"root": root, "pages": {}}
    if state:
        state.value = value
    else:
        db.add(AppSetting(key="catalog:marriott", value=value))


async def seed(db, identity, root, lane, *, provider="marriott", offset=20, age=4):
    origin = "https://www.marriott.com.cn" if root == CN_ROOT else "https://www.marriott.com"
    hotel = Hotel(
        id=identity,
        provider=provider,
        provider_hotel_id=identity,
        hotel_name="Offline source selection",
        official_url=origin + "/hotel/" + identity,
    )
    db.add(hotel)
    await db.flush()
    if lane == "global":
        db.add(AppSetting(key="global_date_cursor:" + identity, value={"offset": offset}))
    elif lane == "recheck":
        db.add(
            StayScan(
                id="scan-" + identity,
                hotel_id=identity,
                check_in=date.today() + timedelta(days=offset),
                check_out=date.today() + timedelta(days=offset + 1),
                adults=2,
                rooms=1,
                status="AVAILABLE",
                observed_at=utcnow() - timedelta(hours=age),
                job_id="offline-history",
            )
        )
    else:
        db.add(
            CrawlJob(
                id="failure-" + identity,
                provider=provider,
                hotel_id=identity,
                kind=JobKind.FETCH_RATE,
                check_in=date.today() + timedelta(days=offset),
                check_out=date.today() + timedelta(days=offset + 1),
                status="FAILED",
                error_type="TIMEOUT",
                created_at=utcnow() - timedelta(hours=age),
                scheduled_at=utcnow() - timedelta(seconds=1),
                dedupe_key="failure-" + identity,
            )
        )
    return hotel


async def expand(db, lane, provider="marriott"):
    settings = config(provider)
    if lane == "global":
        return await expand_global(db, settings)
    if lane == "recheck":
        return await expand_successful_rechecks(db, settings, [provider], 1)
    return await expand_failed_stays(db, settings, [provider], 1)


@pytest.mark.parametrize("lane", ["global", "recheck", "failed"])
@pytest.mark.parametrize("root", [CN_ROOT, EN_ROOT])
async def test_source_filter_precedes_bounded_keysets_and_preserves_excluded_progress(sessions, lane, root):
    old_root = EN_ROOT if root == CN_ROOT else CN_ROOT
    async with sessions() as db, db.begin():
        await set_source(db, root)
        for index in range(12):
            await seed(db, f"a-old-{index:02}", old_root, lane, age=6)
        await seed(db, "z-current", root, lane)
        if lane != "global":
            key = (
                "global_recheck_cursor:marriott"
                if lane == "recheck"
                else "global_failed_stay_cursor:marriott"
            )
            field = "observed_at" if lane == "recheck" else "created_at"
            db.add(
                AppSetting(key=key, value={field: (utcnow() - timedelta(days=1)).isoformat(), "legacy": True})
            )
        assert await expand(db, lane) == 1
        jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 1
        assert jobs[0].hotel_id == "z-current"
        assert jobs[0].payload[SOURCE_MARKER] == root
        assert jobs[0].check_in == date.today() + timedelta(days=20)
        assert (await db.get(Hotel, "a-old-00")).active
        if lane == "global":
            assert (await db.get(AppSetting, "global_date_cursor:a-old-00")).value == {"offset": 20}
            assert (await db.get(AppSetting, "global_hotel_cursor:marriott")).value == {
                "hotel_id": "z-current"
            }
        else:
            remembered = (await db.get(AppSetting, key)).value
            assert remembered["scan_id" if lane == "recheck" else "job_id"] == (
                "scan-z-current" if lane == "recheck" else "failure-z-current"
            )
            assert (
                await db.get(
                    StayScan if lane == "recheck" else CrawlJob,
                    "scan-a-old-00" if lane == "recheck" else "failure-a-old-00",
                )
                is not None
            )


@pytest.mark.parametrize("lane", ["global", "recheck", "failed"])
@pytest.mark.parametrize(
    "value", [None, {"pages": {}}, {"requested_root": CN_ROOT}, {"root": "https://example.com/"}]
)
async def test_legacy_or_unaccepted_source_keeps_old_behavior_without_marker(sessions, lane, value):
    async with sessions() as db, db.begin():
        if value is not None:
            db.add(AppSetting(key="catalog:marriott", value=value))
        await seed(db, "legacy-en", EN_ROOT, lane)
        assert (await load_catalog_price_source(db)).root is None
        assert await expand(db, lane) == 1
        job = await db.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
        assert job.hotel_id == "legacy-en"
        assert SOURCE_MARKER not in job.payload


@pytest.mark.parametrize("lane", ["global", "recheck", "failed"])
async def test_other_provider_is_neither_filtered_nor_tagged(sessions, lane):
    async with sessions() as db, db.begin():
        await set_source(db, CN_ROOT)
        await seed(db, "ihg-only", EN_ROOT, lane, provider="ihg")
        assert await expand(db, lane, "ihg") == 1
        job = await db.scalar(select(CrawlJob).where(CrawlJob.status == "PENDING"))
        assert job.provider == "ihg"
        assert SOURCE_MARKER not in job.payload


async def test_source_switch_continues_each_source_existing_cursor_without_erasing_hotels(sessions):
    async with sessions() as db, db.begin():
        await set_source(db, CN_ROOT)
        await seed(db, "old-en", EN_ROOT, "global", offset=50)
        await seed(db, "current-cn", CN_ROOT, "global", offset=70)
        assert await expand(db, "global") == 1
    async with sessions() as db, db.begin():
        await set_source(db, EN_ROOT)
        assert await expand(db, "global") == 1
        jobs = (await db.scalars(select(CrawlJob).order_by(CrawlJob.check_in))).all()
        assert [(j.hotel_id, (j.check_in - date.today()).days) for j in jobs] == [
            ("old-en", 50),
            ("current-cn", 70),
        ]
        assert jobs[0].payload[SOURCE_MARKER] == EN_ROOT
        assert jobs[1].payload[SOURCE_MARKER] == CN_ROOT
        assert (await db.get(AppSetting, "global_date_cursor:current-cn")).value["offset"] == 71
        assert (await db.get(Hotel, "old-en")).official_url.startswith("https://www.marriott.com/")
        assert (await db.get(Hotel, "current-cn")).official_url.startswith("https://www.marriott.com.cn/")


async def test_explicit_watchlist_of_previous_source_remains_unfiltered_and_untagged(sessions):
    async with sessions() as db, db.begin():
        await set_source(db, CN_ROOT)
        await seed(db, "watched-en", EN_ROOT, "global")
        db.add(
            Watchlist(
                id="manual-watch",
                name="Explicit old source",
                scope="hotel",
                filters={"hotel_id": "watched-en", "days_ahead": 365},
            )
        )
        assert await expand_watchlists(db, config()) == 1
        job = await db.scalar(select(CrawlJob))
        assert job.hotel_id == "watched-en"
        assert SOURCE_MARKER not in job.payload


@pytest.mark.parametrize("tagged", [True, False])
async def test_worker_cancels_only_tagged_source_changed_work(sessions, queue, monkeypatch, tagged):
    calls = []

    class EmptyPrice(FixtureProvider):
        async def search_rates(self, request):
            calls.append(request.provider_hotel_id)
            return []

    monkeypatch.setitem(FACTORIES, "marriott", EmptyPrice)
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await set_source(db, EN_ROOT)
        await seed(db, "en-hotel", EN_ROOT, "global")
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id="en-hotel",
                check_in=day,
                check_out=day + timedelta(days=1),
                payload={SOURCE_MARKER: EN_ROOT} if tagged else {},
            ),
        )
        job.error_type, job.error_message, job.retry_count = "TIMEOUT", "Previous timeout", 2
        job_id = job.id
        await set_source(db, CN_ROOT)
    await queue.put(job_id, 40)
    await process_one(sessions, queue, config())
    async with sessions() as db:
        job = await db.get(CrawlJob, job_id)
        assert calls == ([] if tagged else ["en-hotel"])
        assert job.status == ("CANCELLED" if tagged else "SUCCEEDED")
        if tagged:
            assert job.error_type == "TIMEOUT" and job.retry_count == 2
            assert job.error_message == "Previous timeout; Automatic price task directory source changed"
            assert await db.get(ProviderStatus, "marriott") is None
            assert (await db.scalars(select(StayScan))).all() == []
            assert (await db.scalars(select(PriceHistory))).all() == []
        else:
            assert (await db.scalars(select(StayScan))).one().hotel_id == "en-hotel"


@pytest.mark.parametrize(
    "condition", ["unchanged", "removed_selection", "invalid_marker", "hotel_host_changed", "paused"]
)
async def test_worker_source_marker_respects_current_selection_and_existing_pause(
    sessions, queue, monkeypatch, condition
):
    calls = []

    class EmptyPrice(FixtureProvider):
        async def search_rates(self, request):
            calls.append(request.provider_hotel_id)
            return []

    monkeypatch.setitem(FACTORIES, "marriott", EmptyPrice)
    day, deadline = date.today() + timedelta(days=20), utcnow() + timedelta(hours=4)
    async with sessions() as db, db.begin():
        await set_source(db, EN_ROOT)
        item = await seed(db, "en-hotel", EN_ROOT, "global")
        marker = "unsupported" if condition == "invalid_marker" else EN_ROOT
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=item.id,
                check_in=day,
                check_out=day + timedelta(days=1),
                payload={SOURCE_MARKER: marker},
            ),
        )
        job_id = job.id
        if condition == "removed_selection":
            (await db.get(AppSetting, "catalog:marriott")).value = {"pages": {}}
        elif condition == "invalid_marker":
            await set_source(db, CN_ROOT)
        elif condition == "hotel_host_changed":
            item.official_url = "https://www.marriott.com.cn/hotel/en-hotel"
        elif condition == "paused":
            await set_source(db, CN_ROOT)
            db.add(
                ProviderStatus(
                    provider="marriott", status="BLOCKED", blocked_until=deadline, last_error="RATE_LIMITED"
                )
            )
    await queue.put(job_id, 40)
    await process_one(sessions, queue, config())
    cancelled = condition in {"hotel_host_changed", "paused"}
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == ("CANCELLED" if cancelled else "SUCCEEDED")
        assert calls == ([] if cancelled else ["en-hotel"])
        if condition == "paused":
            state = await db.get(ProviderStatus, "marriott")
            assert state.blocked_until.replace(tzinfo=deadline.tzinfo) == deadline
            assert state.status == "BLOCKED" and state.last_error == "RATE_LIMITED"


async def test_manual_verification_is_not_reinterpreted_from_source_marker(sessions, queue, monkeypatch):
    calls = []

    class EmptyPrice(FixtureProvider):
        async def search_rates(self, request):
            calls.append(request.provider_hotel_id)
            return []

    monkeypatch.setitem(FACTORIES, "marriott", EmptyPrice)
    day = date.today() + timedelta(days=20)
    async with sessions() as db, db.begin():
        await set_source(db, CN_ROOT)
        await seed(db, "en-hotel", EN_ROOT, "global")
        job = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.VERIFY_ANOMALY,
                hotel_id="en-hotel",
                check_in=day,
                check_out=day + timedelta(days=1),
                payload={SOURCE_MARKER: EN_ROOT},
            ),
        )
        job_id = job.id
    await queue.put(job_id, 100)
    await process_one(sessions, queue, config())
    async with sessions() as db:
        assert (await db.get(CrawlJob, job_id)).status == "SUCCEEDED"
        assert calls == ["en-hotel"]
