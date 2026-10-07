"""Wide-scope monitoring is incremental and database-only; no live hotel calls."""

from datetime import date, timedelta

import httpx
import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from app.api.main import app
from app.api.main import settings as api_settings
from app.core.config import Settings
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, Watchlist, utcnow
from app.schemas.domain import JobInput, JobKind
from app.schemas.watchlists import WatchInput
from app.services.jobs import enqueue
from app.services.watchlists import expand_global, expand_watchlists


def hotel(index=0, **overrides):
    data = dict(
        id=f"hotel-{index:03}",
        provider="marriott",
        provider_hotel_id=f"{index:05}",
        hotel_name=f"Offline hotel {index}",
        official_url="https://www.marriott.com/",
        city="Shanghai",
        country="CN",
        brand="Marriott",
    )
    return Hotel(**(data | overrides))


def wide(scope="country", **overrides):
    data = (
        {"country": "CN", "days_ahead": 365}
        if scope == "country"
        else {
            scope: {"city": "Shanghai", "brand": "Marriott", "provider": "marriott"}[scope],
            "days_ahead": 365,
        }
    )
    return Watchlist(id="watch-wide", name="Offline wide", scope=scope, filters=data | overrides)


@pytest.mark.parametrize(
    "scope,field,target",
    [
        ("city", "city", " Shanghai "),
        ("country", "country", " CN "),
        ("brand", "brand", " Marriott "),
        ("provider", "provider", "marriott"),
    ],
)
def test_scope_validation_and_official_spelling(scope, field, target):
    item = WatchInput.model_validate({"scope": scope, field: target, "days_ahead": 365})
    assert item.filters()[field] == target.strip()
    assert item.filters()["days_ahead"] == 365


@pytest.mark.parametrize(
    "body",
    [
        {"scope": "city"},
        {"scope": "country", "country": " "},
        {"scope": "brand", "brand": "X", "city": "Y"},
        {"scope": "provider", "provider": "unknown"},
        {"scope": "hotel", "hotel_id": "x", "provider": "marriott"},
        {"scope": "country", "country": "CN", "days_ahead": 366},
        {"scope": "city", "city": "Shanghai", "unexpected": True},
    ],
)
def test_invalid_or_mixed_scopes_rejected(body):
    with pytest.raises(ValidationError):
        WatchInput.model_validate(body)


def test_legacy_hotel_request_still_works_and_german_metadata_not_casefolded():
    assert WatchInput(hotel_id="id").filters() == {"hotel_id": "id", "days_ahead": 30}
    assert WatchInput(scope="city", city="Straße").filters()["city"] == "Straße"


@pytest.mark.parametrize("scope", ["city", "country", "brand", "provider"])
async def test_wide_scope_discovers_only_matching_active_enabled_hotels(sessions, monkeypatch, scope):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add_all(
            [
                hotel(0),
                hotel(1, **{scope: "Other"} if scope != "provider" else {"provider": "ihg"}),
                hotel(2, active=False),
                hotel(3, city=None, country=None, brand=None, provider="ihg"),
            ]
        )
        db.add(wide(scope))
        assert await expand_watchlists(db, Settings(), budget=10) == 1
        row = await db.scalar(select(CrawlJob))
        assert row.hotel_id == "hotel-000" and row.priority == 85
        assert row.check_in == date.today() and row.check_out == date.today() + timedelta(days=1)


async def test_optional_provider_narrowing_pause_and_case_trim_matching(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    monkeypatch.setenv("IHG_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add_all([hotel(0, country=" cn "), hotel(1, provider="ihg"), hotel(2, provider="gha")])
        db.add(wide(provider="ihg"))
        db.add(ProviderStatus(provider="ihg", blocked_until=utcnow() + timedelta(hours=1)))
        assert await expand_watchlists(db, Settings()) == 0
        state = await db.get(ProviderStatus, "ihg")
        state.blocked_until = None
        assert await expand_watchlists(db, Settings()) == 1
        assert (await db.scalar(select(CrawlJob))).hotel_id == "hotel-001"


async def test_german_metadata_matches_without_ss_substitution(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel(city="Straße"))
        db.add(wide("city", city="Straße"))
        assert await expand_watchlists(db, Settings()) == 1


async def test_more_than_hundred_hotels_rotates_across_sessions_and_keeps_per_hotel_dates(
    sessions, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add_all([hotel(i) for i in range(105)])
        db.add(wide())
    for _ in range(106):
        async with sessions() as db, db.begin():
            assert await expand_watchlists(db, Settings()) == 1
    async with sessions() as db:
        assert await db.scalar(select(func.count(func.distinct(CrawlJob.hotel_id)))) == 105
        dates = (
            await db.scalars(
                select(CrawlJob.check_in).where(CrawlJob.hotel_id == "hotel-000").order_by(CrawlJob.check_in)
            )
        ).all()
        assert dates == [date.today(), date.today() + timedelta(days=1)]
        assert (await db.get(AppSetting, "watch_date_cursor:watch-wide:hotel-001")).value["offset"] == 1


async def test_later_catalog_hotel_is_automatically_included(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel(0))
        db.add(wide())
        assert await expand_watchlists(db, Settings()) == 1
    async with sessions() as db, db.begin():
        db.add(hotel(1))
        assert await expand_watchlists(db, Settings()) == 1
        assert await db.scalar(select(CrawlJob.id).where(CrawlJob.hotel_id == "hotel-001"))


@pytest.mark.parametrize("days", [1, 30, 90, 365])
async def test_exact_date_count_and_last_checkout_stays_within_year(sessions, monkeypatch, days):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel())
        db.add(wide(days_ahead=days))
        db.add(AppSetting(key="watch_date_cursor:watch-wide:hotel-000", value={"offset": days - 1}))
        assert await expand_watchlists(db, Settings()) == 1
        row = await db.scalar(select(CrawlJob))
        assert row.check_in == date.today() + timedelta(days=days - 1)
        assert row.check_out == date.today() + timedelta(days=days)
        assert (await db.get(AppSetting, "watch_date_cursor:watch-wide:hotel-000")).value["offset"] == 0


@pytest.mark.parametrize("lane", ["watch", "global"])
@pytest.mark.parametrize("variant", ["same", "adult1", "room2", "multinight", "old-live", "verify"])
async def test_recent_suppression_matches_full_stay_occupancy_and_live_status(
    sessions, monkeypatch, lane, variant
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel())
        db.add(wide())
        await db.flush()
        payload = {"adults": 1} if variant == "adult1" else {"rooms": 2} if variant == "room2" else {}
        row = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.VERIFY_ANOMALY if variant == "verify" else JobKind.FETCH_RATE,
                hotel_id="hotel-000",
                check_in=date.today(),
                check_out=date.today() + timedelta(days=2 if variant == "multinight" else 1),
                payload=payload,
            ),
        )
        if variant == "old-live":
            row.created_at = utcnow() - timedelta(days=10)
        else:
            row.status = "SUCCEEDED"
        expected = 1 if variant in {"adult1", "room2", "multinight"} else 0
        expand = expand_watchlists if lane == "watch" else expand_global
        assert await expand(db, Settings()) == expected


async def test_wide_and_single_scope_do_not_monopolize_budget_or_duplicate(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add_all([hotel(0), hotel(1)])
        db.add_all(
            [
                wide(),
                Watchlist(
                    id="watch-single",
                    name="Single",
                    scope="hotel",
                    filters={"hotel_id": "hotel-001", "days_ahead": 365},
                ),
            ]
        )
        assert await expand_watchlists(db, Settings(), budget=4) == 2
        assert await expand_global(db, Settings()) == 0
        assert await db.scalar(select(func.count()).select_from(CrawlJob)) == 2


@pytest.fixture
async def client(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("offline-admin"))

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as request:
            yield request
    finally:
        app.dependency_overrides.clear()


AUTH = {"authorization": "Bearer offline-admin"}


async def test_api_scopes_case_insensitive_upsert_pause_resume_preserve_progress(client, sessions):
    payload = {"scope": "city", "city": " Shanghai ", "days_ahead": 365, "name": "My city"}
    assert (await client.post("/api/watchlists", json=payload)).status_code == 401
    first = (await client.post("/api/watchlists", headers=AUTH, json=payload)).json()
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="watch_hotel_cursor:" + first["id"], value={"hotel_id": "existing-progress"}))
    assert (await client.patch("/api/watchlists/" + first["id"], json={"enabled": False})).status_code == 401
    disabled = (
        await client.patch("/api/watchlists/" + first["id"], headers=AUTH, json={"enabled": False})
    ).json()
    assert not disabled["enabled"]
    second = (
        await client.post(
            "/api/watchlists", headers=AUTH, json={"scope": "city", "city": "shanghai", "days_ahead": 90}
        )
    ).json()
    assert second["id"] == first["id"] and second["name"] == "My city" and second["enabled"]
    assert second["filters"]["days_ahead"] == 90
    async with sessions() as db:
        assert (await db.get(AppSetting, "watch_hotel_cursor:" + first["id"])).value == {
            "hotel_id": "existing-progress"
        }


async def test_provider_narrowing_creates_separate_scope_and_no_current_match_is_legal(client):
    one = (
        await client.post("/api/watchlists", headers=AUTH, json={"scope": "country", "country": "CN"})
    ).json()
    two = (
        await client.post(
            "/api/watchlists", headers=AUTH, json={"scope": "country", "country": "CN", "provider": "ihg"}
        )
    ).json()
    assert one["id"] != two["id"]
    assert (
        await client.post("/api/watchlists", headers=AUTH, json={"scope": "provider", "provider": "hyatt"})
    ).status_code == 200
    assert (
        await client.patch("/api/watchlists/missing", headers=AUTH, json={"enabled": False})
    ).status_code == 404


async def test_watchlist_read_pagination_does_not_hide_over_hundred(client, sessions):
    async with sessions() as db, db.begin():
        for i in range(105):
            db.add(
                Watchlist(
                    id=f"watch-{i:03}",
                    name=f"Offline {i}",
                    scope="provider",
                    filters={"provider": "marriott"},
                )
            )
    first = (await client.get("/api/watchlists?limit=100")).json()
    second = (await client.get("/api/watchlists?limit=100&offset=100")).json()
    assert len(first) == 100 and len(second) == 5
    assert len({row["id"] for row in first + second}) == 105


async def test_backlog_cap_and_disabled_watch_do_not_advance_cursor(sessions, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel())
        row = wide()
        row.enabled = False
        db.add(row)
        assert await expand_watchlists(db, Settings()) == 0
        row.enabled = True
        settings = Settings()
        settings.max_pending_jobs = 1
        assert await expand_watchlists(db, settings) == 1
        cursor = await db.get(AppSetting, "watch_date_cursor:watch-wide:hotel-000")
        saved = dict(cursor.value)
        assert await expand_watchlists(db, settings) == 0
        assert cursor.value == saved


@pytest.mark.parametrize(
    "scope,expected",
    [("city", ["Shanghai", "Straße"]), ("country", ["CN", "DE"]), ("brand", ["Marriott", "Park Hyatt"])],
)
async def test_metadata_targets_are_actual_distinct_nonempty_active_catalog_values(
    client, sessions, scope, expected
):
    async with sessions() as db, db.begin():
        db.add_all(
            [
                hotel(0),
                hotel(1),
                hotel(2, provider="hyatt", city="Straße", country="DE", brand="Park Hyatt"),
                hotel(3, city=None, country=None, brand=None),
                hotel(4, city=" ", country=" ", brand=" "),
                hotel(5, active=False, city="Inactive", country="ZZ", brand="Hidden"),
            ]
        )
    response = await client.get("/api/watchlists/targets", params={"scope": scope})
    assert response.status_code == 200 and response.json() == expected
    assert (
        await client.get("/api/watchlists/targets", params={"scope": scope, "provider": "hyatt"})
    ).json() == [expected[1]]


async def test_metadata_search_treats_percent_underscore_as_literal_and_is_bounded(client, sessions):
    async with sessions() as db, db.begin():
        db.add_all(
            [
                hotel(0, city="100% City"),
                hotel(1, city="1000 City"),
                hotel(2, city="A_B"),
                hotel(3, city="AXB"),
            ]
        )
    assert (await client.get("/api/watchlists/targets?scope=city&q=100%25")).json() == ["100% City"]
    assert (await client.get("/api/watchlists/targets?scope=city&q=_")).json() == ["A_B"]
    assert len((await client.get("/api/watchlists/targets?scope=city&limit=2")).json()) == 2
    assert (await client.get("/api/watchlists/targets?scope=hotel")).status_code == 422


async def test_pausing_scope_keeps_queued_jobs_and_global_monitoring_independent(
    client, sessions, monkeypatch
):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    async with sessions() as db, db.begin():
        db.add(hotel())
    saved = (
        await client.post(
            "/api/watchlists",
            headers=AUTH,
            json={"scope": "provider", "provider": "marriott", "days_ahead": 365},
        )
    ).json()
    async with sessions() as db, db.begin():
        assert await expand_watchlists(db, Settings()) == 1
        old_job = await db.scalar(select(CrawlJob))
        old_id = old_job.id
    assert (
        await client.patch("/api/watchlists/" + saved["id"], headers=AUTH, json={"enabled": False})
    ).status_code == 200
    async with sessions() as db, db.begin():
        assert (await db.get(CrawlJob, old_id)).status == "PENDING"
        assert await expand_watchlists(db, Settings()) == 0
        assert (
            await expand_global(db, Settings()) == 0
        )  # Existing same-condition task, not disabled global lane.
        assert await expand_global(db, Settings()) == 1  # Next unvisited date still runs globally.
