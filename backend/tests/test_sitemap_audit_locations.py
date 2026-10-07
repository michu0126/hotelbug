import json
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import httpx
from audit_sitemap_catalog import (
    ensure_audit_database,
    install_location_diagnostics,
    public_location_projection,
    run_batch,
)

from app.crawler import worker
from app.models.tables import AppSetting, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData
from tests.test_sitemap_audit_resume import settings


async def test_missing_child_map_does_not_abort_cached_new_hotel_details(sessions, queue, monkeypatch):
    await ensure_audit_database(sessions, "gha")
    source = "https://www.ghadiscovery.com/anantara/explicit-offline-new-hotel"
    missing = "https://www.ghadiscovery.com/sitemap-4.xml"
    async with sessions() as db, db.begin():
        db.add(
            AppSetting(
                key="catalog:gha",
                value={
                    "maps": [missing],
                    "map_cursor": 0,
                    "refresh_at": (utcnow() + timedelta(days=14)).timestamp(),
                    "urls": [source],
                    "cursor": 0,
                },
            )
        )
    calls = []

    def response(request):
        calls.append(str(request.url))
        return httpx.Response(404)

    execute = AsyncMock(
        return_value=[
            HotelData(
                provider="gha",
                provider_hotel_id="12345",
                hotel_name="Explicit offline new hotel",
                official_url=source,
            )
        ]
    )
    monkeypatch.setattr(worker, "execute", execute)
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        result = await run_batch(sessions, queue, settings("gha"), client, "gha", 1, emit=lambda _: None)
    assert result["outcome"] == "BATCH_COMPLETE"
    assert result["real_hotels_persisted"] == result["sitemaps_missing"] == 1
    assert result["sitemaps_read"] == result["sitemaps_failed"] == 0
    assert result["source_error"] == "SITEMAP_MISSING" and calls == [missing]
    assert execute.await_count == 1


def test_location_diagnostics_only_read_public_schema_and_dom_not_script_secrets():
    html = """<html><body><section><div><h1>Explicit offline hotel</h1></div>
    <input value="PRIVATE-FORM-VALUE"></section><address>Public Road</address>
    <script>window.cookie = "PRIVATE-SCRIPT";</script>
    <script type="application/ld+json">{"@graph":[{"@type":"Hotel","name":"Explicit offline hotel","token":"PRIVATE-LD-TOKEN","address":{"addressCountry":"ExplicitCountry","cookie":"PRIVATE-LD-COOKIE"},"geo":{"latitude":1,"longitude":2}},{"@type":"Thing","secret":"PRIVATE-THING"}]}</script>
    </body></html>"""
    result = public_location_projection(html)
    assert result["public_hotel_schema"] == [
        {
            "type": ["Hotel"],
            "name": "Explicit offline hotel",
            "address": {"addressCountry": "ExplicitCountry"},
            "geo": {"latitude": 1, "longitude": 2},
        }
    ]
    assert result["rendered_address_elements"] == ["Public Road"]
    assert "PRIVATE" not in json.dumps(result)


def test_location_projection_ignores_malformed_or_nonhotel_structured_content():
    result = public_location_projection(
        '<script type="application/ld+json">bad</script><script type="application/ld+json">{"@type":{"Hotel":"not a type"}}</script>'
    )
    assert result["public_hotel_schema"] == []


async def test_accor_diagnostic_reads_only_already_loaded_public_details(monkeypatch, capsys):
    page = MagicMock()
    page.url = "https://all.accor.com/hotel/0505/index.en.shtml?private=SECRET"
    page.is_closed.return_value = False
    page.content = AsyncMock(return_value='<address>Explicit public address</address><input value="SECRET">')
    instance = MagicMock()
    instance._page = None
    result = HotelData(
        provider="accor",
        provider_hotel_id="0505",
        hotel_name="Explicit offline hotel",
        official_url="https://all.accor.com/hotel/0505/index.en.shtml",
    )

    async def original(code):
        assert code == "0505"
        instance._page = page
        return result

    instance.get_hotel_details = original
    monkeypatch.setitem(FACTORIES, "accor", lambda: instance)
    install_location_diagnostics("accor")
    assert await FACTORIES["accor"]().get_hotel_details("0505") is result
    projection = json.loads(capsys.readouterr().out)
    assert projection["rendered_address_elements"] == ["Explicit public address"]
    assert projection["public_location_source"] == str(result.official_url)
    assert "SECRET" not in json.dumps(projection)
    page.goto.assert_not_called()
