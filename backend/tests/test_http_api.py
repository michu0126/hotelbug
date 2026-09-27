import httpx
import pytest

from app.api.main import app
from app.core.errors import ErrorCode, ProviderError
from app.core.logging import redact
from app.crawler.http import request
from app.database.session import get_session


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
            assert len(states) == 6 and not any(s["implemented"] for s in states)
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
