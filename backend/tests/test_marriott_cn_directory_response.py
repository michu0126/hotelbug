"""Two-country projection of the observed 151-bucket public response.

Totals are reduced to 24 for this synthetic parser fixture, not live coverage.
"""

from urllib.parse import parse_qs, urlparse

import pytest

from app.core.errors import ProviderError
from app.providers.marriott_cn_directory_response import OPERATION, parse_worldwide_response


def observed():
    request = {
        "operationName": OPERATION,
        "variables": {
            "search": {
                "options": {"countsOnly": True},
                "facets": {"terms": [{"type": "COUNTRIES", "dimensions": []}]},
            }
        },
    }
    data = {
        "total": 24,
        "status": [{"code": "SUCCESS", "httpStatus": 200, "errors": []}],
        "facets": [
            {
                "type": {"code": "countries", "count": 2},
                "buckets": [
                    {"code": "BT", "count": 2, "label": "不丹"},
                    {"code": "SG", "count": 22, "label": "新加坡"},
                ],
            }
        ],
    }
    response = {"data": {"search": {"properties": {"searchByLocation": data}}}}
    return request, response, data


def test_public_counts_expand_country_tasks_without_inventing_hotels_or_rates():
    request, response, _ = observed()
    result = parse_worldwide_response(request, response)
    assert result.page_complete and not result.complete
    assert result.reported_total == 24 and not result.hotels
    assert len(result.directory_urls) == 2
    assert {
        parse_qs(urlparse(str(u)).query)["destinationAddress.country"][0] for u in result.directory_urls
    } == {"SG", "BT"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r, s, d: r.update(operationName="unrelated"),
        lambda r, s, d: r["variables"]["search"].update(location={"country": "SG"}),
        lambda r, s, d: r["variables"]["search"]["options"].update(startDate="2026-10-30"),
        lambda r, s, d: r["variables"]["search"]["facets"]["terms"][0].update(dimensions="SG"),
        lambda r, s, d: s.update(errors=[{"message": "partial failure"}]),
        lambda r, s, d: d.update(total=25),
        lambda r, s, d: d["status"][0].update(httpStatus=403),
        lambda r, s, d: d["facets"][0]["type"].update(count=3),
        lambda r, s, d: d["facets"][0]["buckets"][0].update(code="SG"),
        lambda r, s, d: d["facets"][0]["buckets"][0].update(label=""),
    ],
)
def test_filtered_partial_or_inconsistent_data_cannot_seed_a_worldwide_graph(mutation):
    request, response, data = observed()
    mutation(request, response, data)
    with pytest.raises(ProviderError):
        parse_worldwide_response(request, response)


async def test_response_root_persists_and_scheduler_queues_countries(sessions, queue):
    from sqlalchemy import select

    from app.core.config import ProviderPolicy, Settings
    from app.models.tables import CrawlJob
    from app.providers.marriott_cn_catalog import ROOT
    from app.schemas.domain import JobInput, JobKind
    from app.services.ihg_catalogs import discover_marriott, persist_catalog, request_directory_root
    from app.services.jobs import enqueue

    request, response, _ = observed()
    settings = Settings(
        provider_overrides={"marriott": ProviderPolicy(enabled=True)}, catalog_jobs_per_tick=2
    )
    async with sessions() as db, db.begin():
        task = await enqueue(
            db, JobInput(provider="marriott", kind=JobKind.DISCOVER_CATALOG, payload={"official_url": ROOT})
        )
        await request_directory_root(db, task)
        await persist_catalog(db, task, parse_worldwide_response(request, response), settings)
        task.status = "SUCCEEDED"
    assert await discover_marriott(sessions, queue, settings) == 2
    async with sessions() as db:
        jobs = (await db.scalars(select(CrawlJob).where(CrawlJob.status == "PENDING"))).all()
        assert len(jobs) == 2
        assert all("destinationAddress.country=" in j.payload["official_url"] for j in jobs)
