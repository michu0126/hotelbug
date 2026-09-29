import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.models.tables import AppSetting, CrawlJob
from app.services.catalogs import accor_codes, discover_accor, discover_gha, gha_links


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


def gha_map(urls):
    return ("<urlset>" + "".join(f"<url><loc>{url}</loc></url>" for url in urls) + "</urlset>").encode()


def test_gha_catalog_filters():
    root = "https://www.ghadiscovery.com"
    assert gha_links(
        gha_map(
            [
                root + "/avani-hotels-resorts/avani-sukhumvit-bangkok-hotel",
                root + "/promotions/example",
                root + "/avani-hotels-resorts/hotel/stay-offers/a",
                "https://example.com/brand/hotel",
            ]
        )
    ) == [root + "/avani-hotels-resorts/avani-sukhumvit-bangkok-hotel", root + "/avani-hotels-resorts/hotel"]


@pytest.mark.asyncio
async def test_gha_map_resume_and_dedupe(sessions, queue, monkeypatch):
    monkeypatch.setenv("GHA_ENABLED", "true")
    root = "https://www.ghadiscovery.com"
    calls = []

    def respond(request):
        calls.append(request.url.path)
        urls = {
            "/sitemap.xml": [root + "/sitemap-1.xml", root + "/sitemap-2.xml"],
            "/sitemap-1.xml": [root + "/brand/hotel-one"],
            "/sitemap-2.xml": [root + "/brand/hotel-one", root + "/brand/hotel-two"],
        }[request.url.path]
        return httpx.Response(200, content=gha_map(urls))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        for expected in (0, 1, 1, 0):
            assert await discover_gha(sessions, queue, Settings(), client) == expected
            async with sessions() as session, session.begin():
                state = await session.get(AppSetting, "catalog:gha")
                state.value = {**state.value, "next_fetch_at": 0}
    assert calls == ["/sitemap.xml", "/sitemap-1.xml", "/sitemap-2.xml"]
    async with sessions() as session:
        jobs = (await session.scalars(select(CrawlJob))).all()
        assert len(jobs) == 2
        assert all(job.provider == "gha" and job.payload.get("official_url") for job in jobs)


@pytest.mark.asyncio
async def test_gha_missing_map_does_not_block_later_maps(sessions, queue, monkeypatch):
    monkeypatch.setenv("GHA_ENABLED", "true")
    root = "https://www.ghadiscovery.com"
    async with sessions() as session, session.begin():
        session.add(
            AppSetting(
                key="catalog:gha",
                value={"maps": [root + "/sitemap-4.xml", root + "/sitemap-5.xml"], "refresh_at": 9999999999},
            )
        )

    def respond(request):
        return (
            httpx.Response(404)
            if request.url.path == "/sitemap-4.xml"
            else httpx.Response(200, content=gha_map([root + "/brand/hotel"]))
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await discover_gha(sessions, queue, Settings(), client) == 0
        async with sessions() as session, session.begin():
            state = await session.get(AppSetting, "catalog:gha")
            assert state.value["missing_maps"] == [root + "/sitemap-4.xml"]
            state.value = {**state.value, "next_fetch_at": 0}
        assert await discover_gha(sessions, queue, Settings(), client) == 1
