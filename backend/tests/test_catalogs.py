import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob
from app.services.catalogs import accor_codes, discover_accor


def sitemap(codes):
    return (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + "".join(
            f"<url><loc>https://all.accor.com/hotel/{code}/index.en.shtml</loc></url>" for code in codes
        )
        + "</urlset>"
    ).encode()


def test_catalog_identity():
    assert accor_codes(sitemap(["0338", "B123", "0338"])) == ["0338", "B123"]
    with pytest.raises(ValueError):
        accor_codes(sitemap(["0338"]).replace(b"all.accor.com", b"example.com"))


@pytest.mark.asyncio
async def test_catalog_resume_and_no_placeholder_hotels(sessions, queue, monkeypatch):
    monkeypatch.setenv("ACCOR_ENABLED", "true")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=sitemap([f"{i:04}" for i in range(23)]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for expected in (10, 10, 3, 0):
            assert await discover_accor(sessions, queue, Settings(), client) == expected
    assert len(calls) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(CrawlJob)) == 23
        assert (await session.get(AppSetting, "catalog:accor")).value["cursor"] == 23


@pytest.mark.asyncio
async def test_catalog_failure_backoff(sessions, queue, monkeypatch):
    monkeypatch.setenv("ACCOR_ENABLED", "true")
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(403)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await discover_accor(sessions, queue, Settings(), client) == 0
        assert await discover_accor(sessions, queue, Settings(), client) == 0
    assert len(calls) == 1
