"""Late directory results must not replace the selected source's hotel identity."""

from types import SimpleNamespace
from urllib.parse import urlencode

from sqlalchemy import select

from app.core.config import Settings
from app.models.tables import AppSetting, Hotel
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.domain import CatalogPageData, HotelData, JobKind
from app.services.ihg_catalogs import (
    persist_catalog,
    request_directory_root,
    selected_directory_pages,
    selected_directory_root,
)
from app.services.rates import upsert_hotel

CODE = "SINNV"
EN_LEAF = "https://www.marriott.com/en/destinations/singapore.mi"
CN_LEAF = "https://www.marriott.com.cn/search/findHotels.mi?" + urlencode(
    {
        "destinationAddress.destination": "新加坡",
        "destinationAddress.country": "SG",
        "searchType": "InCity",
        "deviceType": "desktop-web",
    }
)
CN_HOTEL = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=SINNV&isSearch=true&currency="
EN_HOTEL = "https://www.marriott.com/en-us/hotels/travel/sinnv-aloft-singapore-novena"


def task(source, identity):
    return SimpleNamespace(
        id=identity,
        provider="marriott",
        kind=JobKind.DISCOVER_CATALOG,
        payload={"official_url": source},
    )


def hotel(name, url):
    return HotelData(
        provider="marriott",
        provider_hotel_id=CODE,
        hotel_name=name,
        official_url=url,
        country="SG",
    )


def leaf_result(source, item):
    return CatalogPageData(
        provider="marriott",
        source_url=source,
        hotels=[item],
        reported_total=1,
        complete=True,
        page_complete=True,
    )


async def test_late_english_leaf_keeps_selected_chinese_hotel_and_old_page_evidence(sessions):
    async with sessions() as db, db.begin():
        choice = task(CN_ROOT, "selected-cn-root")
        await request_directory_root(db, choice)
        await persist_catalog(
            db,
            choice,
            CatalogPageData(
                provider="marriott",
                source_url=CN_ROOT,
                directory_urls=[CN_LEAF],
                reported_total=1,
                complete=False,
                page_complete=True,
            ),
            Settings(),
        )
        await persist_catalog(
            db,
            task(CN_LEAF, "current-cn-leaf"),
            leaf_result(CN_LEAF, hotel("当前中文酒店名称", CN_HOTEL)),
            Settings(),
        )

    async with sessions() as db, db.begin():
        await persist_catalog(
            db,
            task(EN_LEAF, "older-en-leaf"),
            leaf_result(EN_LEAF, hotel("Older English Hotel Name", EN_HOTEL)),
            Settings(),
        )

    async with sessions() as db:
        row = await db.scalar(select(Hotel).where(Hotel.provider == "marriott"))
        assert row.provider_hotel_id == CODE
        assert row.official_url == CN_HOTEL
        assert row.hotel_name == "当前中文酒店名称"
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert selected_directory_root(state, "marriott") == CN_ROOT
        assert set(selected_directory_pages(state, "marriott")) == {CN_ROOT, CN_LEAF}
        assert EN_LEAF not in state["pages"][CN_ROOT]["directory_urls"]
        # Keep the late result as historical evidence without using it to
        # overwrite the active property's identity or its selected graph.
        archived = state["pages"][EN_LEAF]
        assert archived["status"] == "SUCCEEDED"
        assert archived["hotel_ids"] == [CODE]
        assert archived["parsed_hotels"] == archived["reported_total"] == 1
        assert archived["job_id"] == "older-en-leaf"


async def test_default_english_source_still_refreshes_hotel_identity(sessions):
    async with sessions() as db, db.begin():
        await upsert_hotel(
            db,
            hotel("Previous English Hotel Name", EN_HOTEL + "/"),
        )
    async with sessions() as db, db.begin():
        await persist_catalog(
            db,
            task(EN_LEAF, "current-en-leaf"),
            leaf_result(EN_LEAF, hotel("Current English Hotel Name", EN_HOTEL)),
            Settings(),
        )

    async with sessions() as db:
        row = await db.scalar(select(Hotel).where(Hotel.provider == "marriott"))
        assert row.provider_hotel_id == CODE
        assert row.official_url == EN_HOTEL
        assert row.hotel_name == "Current English Hotel Name"
        state = (await db.get(AppSetting, "catalog:marriott")).value
        assert selected_directory_root(state, "marriott") == EN_ROOT
        assert "requested_root" not in state
        assert state["pages"][EN_LEAF]["hotel_ids"] == [CODE]
