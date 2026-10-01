import httpx
import pytest
from pydantic import SecretStr

from app.api.main import app, settings
from app.core.errors import ErrorCode, ProviderError
from app.core.logging import redact
from app.crawler.http import request
from app.database.session import get_session
from app.schemas.domain import HotelData
from app.services.rates import upsert_hotel


@pytest.mark.parametrize(
    "status,code",
    [
        (403, ErrorCode.BLOCKED_BY_ANTIBOT),
        (429, ErrorCode.RATE_LIMITED),
        (401, ErrorCode.TOKEN_EXPIRED),
        (500, ErrorCode.HTTP_ERROR),
    ],
)
async def test_http_error_classification(status, code):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(status, headers={"retry-after": "120"}))
    ) as client:
        with pytest.raises(ProviderError) as error:
            await request(client, "GET", "https://www.marriott.com/")
        assert error.value.code == code
        if status == 429:
            assert error.value.retry_after == 120


def test_logging_redacts_secrets():
    result = redact("token=secret Cookie=abc https://example.com/path?session=abc")
    assert "secret" not in result and "abc" not in result


async def test_api_live_providers_and_write_guard(sessions):
    async def dependency():
        async with sessions() as s:
            yield s

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/health/live")).status_code == 200
            states = (await client.get("/api/providers")).json()
            assert len(states) == 6
            assert [s["provider"] for s in states if s["implemented"]] == [
                "marriott",
                "ihg",
                "hilton",
                "accor",
                "gha",
            ]
            assert (
                next(s for s in states if s["provider"] == "hilton")["verification"]
                == "PUBLIC_PAGE_OBSERVED_WORKER_UNVERIFIED"
            )
            assert (
                next(s for s in states if s["provider"] == "ihg")["verification"]
                == "PUBLIC_PAGE_SAMPLE_VERIFIED"
            )
            assert not any(s["enabled"] for s in states)
            result = await client.post(
                "/api/hotels",
                json={
                    "provider": "gha",
                    "provider_hotel_id": "x",
                    "hotel_name": "test",
                    "official_url": "https://www.ghadiscovery.com/",
                },
            )
            assert result.status_code == 503
    finally:
        app.dependency_overrides.clear()


async def test_catalog_api_distinguishes_discovery_from_actual_quote_coverage(sessions):
    from app.models.tables import AppSetting

    async with sessions() as db, db.begin():
        await upsert_hotel(
            db,
            HotelData(
                provider="ihg",
                provider_hotel_id="HSVPP",
                hotel_name="Observed Hotel",
                official_url="https://www.ihg.com/holidayinnexpress/hotels/us/en/huntsville/hsvpp/hoteldetail",
            ),
        )
        db.add(
            AppSetting(
                key="catalog:ihg",
                value={
                    "pages": {"root": {"status": "SUCCEEDED"}, "leaf": {"status": "PARTIAL"}, "todo": {}},
                    "last_error": "DIRECTORY_PARTIAL",
                },
            )
        )

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/api/catalogs")
            assert response.status_code == 200
            ihg = next(x for x in response.json() if x["provider"] == "ihg")
            assert ihg["hotels"] == 1 and ihg["hotels_with_quotes"] == 0
            assert ihg["parsed_directory_pages"] == 2 and ihg["directory_pages"] == 3
            assert ihg["partial_directory_pages"] == 1 and ihg["last_error"] == "DIRECTORY_PARTIAL"
    finally:
        app.dependency_overrides.clear()


async def test_watchlist_create_and_update_via_api(sessions, monkeypatch):
    monkeypatch.setattr(settings, "admin_token", SecretStr("test-only"))
    async with sessions() as db, db.begin():
        hotel = await upsert_hotel(
            db,
            HotelData(
                provider="marriott",
                provider_hotel_id="NYCMQ",
                hotel_name="New York Marriott Marquis",
                official_url="https://www.marriott.com/hotels/travel/nycmq-new-york-marriott-marquis",
            ),
        )
        hotel_id = hotel.id

    async def dependency():
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            headers = {"authorization": "Bearer test-only"}
            first = await client.post(
                "/api/watchlists", headers=headers, json={"hotel_id": hotel_id, "days_ahead": 30}
            )
            assert first.status_code == 200
            second = await client.post(
                "/api/watchlists", headers=headers, json={"hotel_id": hotel_id, "days_ahead": 365}
            )
            assert second.status_code == 200
            assert second.json()["id"] == first.json()["id"]
            assert second.json()["filters"]["days_ahead"] == 365
    finally:
        app.dependency_overrides.clear()
