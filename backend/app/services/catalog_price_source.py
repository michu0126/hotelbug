"""Explicit accepted directory sources constrain only newly automatic price work."""

from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import or_, true

from app.models.tables import AppSetting, CrawlJob, Hotel
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT as CN_ROOT
from app.schemas.domain import JobKind

ROOTS = (EN_ROOT, CN_ROOT)
SOURCE_MARKER = "automatic_catalog_root"


@dataclass(frozen=True)
class CatalogPriceSource:
    root: str | None

    def hotel_predicate(self):
        """Apply before bounded keysets, not after old-source rows consume them."""
        if self.root is None:
            return true()
        origin = "https://" + urlparse(self.root).hostname
        return or_(Hotel.provider != "marriott", Hotel.official_url.like(origin + "/%"))

    def payload(self, provider: str) -> dict:
        return {SOURCE_MARKER: self.root} if provider == "marriott" and self.root else {}


async def load_catalog_price_source(session) -> CatalogPriceSource:
    state = await session.get(AppSetting, "catalog:marriott")
    value = state.value if state and isinstance(state.value, dict) else {}
    root = value.get("root")
    # A default or requested-but-unverified root is not an accepted admin choice.
    # Legacy state keeps its existing behavior, without marking old/manual work.
    return CatalogPriceSource(root if root in ROOTS else None)


async def automatic_price_source_changed(session, job: CrawlJob, hotel: Hotel | None) -> bool:
    if job.provider != "marriott" or (
        job.kind not in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.FETCH_CALENDAR_HINTS)
        and not (job.kind == JobKind.VERIFY_ANOMALY and job.payload.get("calendar_signal") is True)
    ):
        return False
    marker = job.payload.get(SOURCE_MARKER)
    if marker not in ROOTS:
        return False  # Never reinterpret unmarked legacy/manual/watchlist jobs.
    source = await load_catalog_price_source(session)
    if source.root is None:
        return False
    if marker != source.root:
        return True
    return bool(hotel and urlparse(hotel.official_url).hostname != urlparse(source.root).hostname)
