import json
import os
from datetime import timedelta
from unittest.mock import AsyncMock

import httpx
import pytest

from app.api.main import app
from app.api.main import settings as api_settings
from app.core.errors import ErrorCode, ProviderError
from app.database.session import get_session
from app.models.tables import CrawlJob, Hotel, utcnow
from app.providers import browser_session as pool
from app.services.official_connections import (
    DOMAINS,
    ORIGINS,
    connection_path,
    connection_summary,
    load_connection,
    parse_cookies,
    save_connection,
)
from tests.test_browser_session import fake_runtime


@pytest.mark.parametrize("provider", DOMAINS)
def test_header_is_scoped_to_selected_official_origin(provider):
    origin, cookies, expired = parse_cookies(
        provider, ORIGINS[provider], "Cookie: session=private; other=a=b"
    )
    assert cookies == [
        {"name": "session", "value": "private", "url": origin + "/"},
        {"name": "other", "value": "a=b", "url": origin + "/"},
    ]
    assert expired == 0


@pytest.mark.parametrize(
    "origin",
    [
        "http://www.marriott.com",
        "https://marriott.com.evil.org",
        "https://evil.org",
        "https://user:secret@www.marriott.com",
        "https://www.marriott.com/path",
    ],
)
def test_rejects_wrong_origins(origin):
    with pytest.raises(ValueError):
        parse_cookies("marriott", origin, "secret=value")


def test_json_and_expiry_and_same_site():
    raw = json.dumps(
        {
            "cookies": [
                {
                    "name": "session",
                    "value": "private",
                    "domain": ".marriott.com",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "no_restriction",
                    "expirationDate": 2000,
                },
                {"name": "old", "value": "private-old", "domain": ".marriott.com", "expires": 500},
            ]
        }
    )
    _, cookies, expired = parse_cookies("marriott", ORIGINS["marriott"], raw, now=1000)
    assert expired == 1 and len(cookies) == 1
    assert cookies[0]["expires"] == 2000 and cookies[0]["secure"] is True
    assert cookies[0]["sameSite"] == "None" and cookies[0]["httpOnly"] is True


def test_netscape_format_supports_httponly_and_session():
    raw = "# Netscape HTTP Cookie File\n#HttpOnly_.marriott.com\tTRUE\t/\tTRUE\t0\tsession\tprivate"
    _, cookies, _ = parse_cookies("marriott", ORIGINS["marriott"], raw)
    assert cookies == [
        dict(name="session", value="private", domain=".marriott.com", path="/", secure=True, httpOnly=True)
    ]


@pytest.mark.parametrize(
    "raw",
    [
        "bad",
        "a=1\r\nAuthorization: value",
        "a=1; a=2",
        "[]",
        "{",
        " ",
        '[{"name":"x","value":"secret","domain":".ihg.com"}]',
        '[{"name":"x","value":"secret","expires":"nan"}]',
    ],
)
def test_invalid_cookies_rejected_without_returning_values(raw):
    with pytest.raises(ValueError) as exc:
        parse_cookies("marriott", ORIGINS["marriott"], raw)
    assert "secret" not in str(exc.value)


def test_private_atomic_storage_and_disable_preserve_owned_scopes(tmp_path):
    saved = save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=private")
    assert load_connection(tmp_path, "marriott") == saved
    summary = connection_summary("marriott", saved)
    assert summary["configured"] and summary["status"] == "SAVED_NOT_VERIFIED"
    assert "private" not in str(summary) and "session" not in str(summary.get("cookie_names", ""))
    path = connection_path(tmp_path, "marriott")
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    disabled = save_connection(tmp_path, "marriott", ORIGINS["marriott"], enabled=False)
    assert disabled["cookies"] == [] and disabled["owned_keys"] == saved["owned_keys"]
    assert disabled["revision"] != saved["revision"]
    assert not connection_summary("marriott", disabled)["configured"]


def test_corrupt_file_can_be_replaced_by_valid_explicit_save(tmp_path):
    path = connection_path(tmp_path, "marriott")
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_connection(tmp_path, "marriott")
    saved = save_connection(tmp_path, "marriott", ORIGINS["marriott"], "new-session=offline")
    assert load_connection(tmp_path, "marriott")["revision"] == saved["revision"]


@pytest.mark.parametrize("domain", ["..marriott.com", "x..marriott.com", "bad_name.marriott.com"])
def test_malformed_cookie_domain_is_rejected(domain):
    with pytest.raises(ValueError):
        parse_cookies("marriott", ORIGINS["marriott"], json.dumps([dict(name="x", value="v", domain=domain)]))


async def test_import_once_then_apply_updated_connection_without_new_profile(monkeypatch, tmp_path):
    _, driver, _, context = fake_runtime(monkeypatch)
    context.clear_cookies, context.add_cookies = AsyncMock(), AsyncMock()
    save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=one")
    try:
        await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        context.add_cookies.assert_awaited_once()
        assert context.add_cookies.await_args.args[0][0]["value"] == "one"
        save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=two")
        await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        assert context.add_cookies.await_count == 2
        assert context.add_cookies.await_args.args[0][0]["value"] == "two"
        driver.chromium.launch_persistent_context.assert_awaited_once()
        save_connection(tmp_path, "marriott", ORIGINS["marriott"], enabled=False)
        await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        assert context.add_cookies.await_count == 2
        assert context.clear_cookies.await_count == 3
    finally:
        await pool.close_browser_sessions()


async def test_live_browser_job_blocks_cookie_replacement(monkeypatch, tmp_path):
    _, _, _, context = fake_runtime(monkeypatch)
    context.clear_cookies, context.add_cookies = AsyncMock(), AsyncMock()
    save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=one")
    try:
        await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        context.pages = [object()]
        save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=two")
        with pytest.raises(ProviderError) as exc:
            await pool.new_provider_page("marriott", session_dir=str(tmp_path))
        assert exc.value.code == ErrorCode.BROWSER_STARTUP_FAILED
        assert context.add_cookies.await_count == 1
    finally:
        await pool.close_browser_sessions()


async def test_authenticated_api_never_returns_or_enqueues_cookie_values(sessions, monkeypatch, tmp_path):
    from pydantic import SecretStr

    monkeypatch.setattr(api_settings, "admin_token", SecretStr("test-admin"))
    monkeypatch.setattr(api_settings, "browser_session_dir", str(tmp_path))
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    headers = {"authorization": "Bearer test-admin"}
    try:
        async with sessions() as db, db.begin():
            hotel = Hotel(
                provider="marriott",
                provider_hotel_id="NKGWI",
                hotel_name="Offline Test Hotel",
                official_url="https://www.marriott.com/hotels/travel/nkgwi-offline",
            )
            db.add(hotel)
            await db.flush()
            hotel_id = hotel.id
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            url = "/api/settings/connections/marriott"
            assert (await client.get("/api/settings/connections")).status_code == 401
            denied = await client.put(url, json={"cookie_text": "session=PRIVATE"})
            assert denied.status_code == 401
            invalid = await client.put(
                url, headers=headers, json={"origin": "https://evil.org", "cookie_text": "session=PRIVATE"}
            )
            assert invalid.status_code == 422 and "PRIVATE" not in invalid.text
            saved = await client.put(
                url, headers=headers, json={"origin": ORIGINS["marriott"], "cookie_text": "session=PRIVATE"}
            )
            assert saved.status_code == 200 and saved.json()["status"] == "SAVED_NOT_VERIFIED"
            assert "PRIVATE" not in saved.text and saved.headers["cache-control"] == "no-store"
            listed = await client.get("/api/settings/connections", headers=headers)
            assert listed.status_code == 200 and "PRIVATE" not in listed.text
            tested = await client.post(
                url + "/test",
                headers=headers,
                json={"hotel_id": hotel_id, "check_in": str((utcnow() + timedelta(days=3)).date())},
            )
            assert tested.status_code == 200 and tested.json()["last_test"]["status"] == "PENDING"
            async with sessions() as db:
                job = await db.get(CrawlJob, tested.json()["last_test"]["job_id"])
                assert "PRIVATE" not in json.dumps(job.payload)
                assert job.payload["official_session_revision"] == saved.json()["revision"]
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("changed", [False, True])
async def test_connection_quote_is_durable_without_notifications_and_stale_test_is_cancelled(
    sessions, queue, monkeypatch, tmp_path, changed
):
    from sqlalchemy import func, select
    from test_domain import fixture_rate

    from app.core.config import Settings
    from app.crawler import worker
    from app.models.tables import PriceHistory
    from app.schemas.domain import JobInput, JobKind
    from app.services.jobs import enqueue

    monkeypatch.setenv("MARRIOTT_ENABLED", "true")
    saved = save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=offline")
    day = (utcnow() + timedelta(days=3)).date()
    async with sessions() as db, db.begin():
        hotel = Hotel(
            provider="marriott",
            provider_hotel_id="NKGWI",
            hotel_name="Offline Test Hotel",
            official_url="https://www.marriott.com/hotels/travel/nkgwi-offline",
        )
        db.add(hotel)
        await db.flush()
        row = await enqueue(
            db,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=day,
                check_out=day + timedelta(days=1),
                payload={"connection_test": True, "official_session_revision": saved["revision"]},
            ),
        )
        identity = row.id
    rate = fixture_rate().model_copy(
        update={
            "provider_hotel_id": "NKGWI",
            "check_in": day,
            "check_out": day + timedelta(days=1),
            "adults": 2,
            "rooms": 1,
        }
    )
    execute = AsyncMock(return_value=[rate])
    detect, match = AsyncMock(), AsyncMock()
    monkeypatch.setattr(worker, "execute", execute)
    monkeypatch.setattr(worker, "detect_drops", detect)
    monkeypatch.setattr(worker, "match_calendar_candidates", match)
    if changed:
        save_connection(tmp_path, "marriott", ORIGINS["marriott"], "session=new-offline")
    await queue.put(identity, 95)
    assert await worker.process_one(sessions, queue, Settings(browser_session_dir=str(tmp_path)))
    detect.assert_not_awaited()
    match.assert_not_awaited()
    async with sessions() as db:
        row = await db.get(CrawlJob, identity)
        assert row.status == ("CANCELLED" if changed else "SUCCEEDED")
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == (0 if changed else 1)
    assert execute.await_count == (0 if changed else 1)
