"""Fair IHG directory/map lanes with independent persistent source state."""

from app.models.tables import AppSetting, ProviderStatus, utcnow
from app.providers.ihg_sitemap import ROOT, STATE_KEY, property_code, sitemap_links
from app.services.catalogs import _discover_index
from app.services.ihg_catalogs import discover_ihg as discover_directory

LANE_KEY = "catalog:ihg-source-lane"


async def discover_ihg_sitemap(sessions, queue, settings, client):
    return await _discover_index(
        sessions,
        queue,
        settings,
        client,
        "ihg",
        ROOT,
        sitemap_links,
        state_key=STATE_KEY,
        candidate_code=property_code,
    )


async def discover_ihg_global(sessions, queue, settings, client):
    if not settings.global_monitoring_enabled or not settings.provider_policy("ihg").enabled:
        return 0
    token = await queue.lock("hotelbug:catalog-lane:ihg", 60)
    if token is None:
        return 0
    try:
        async with sessions() as db:
            now = utcnow()
            status = await db.get(ProviderStatus, "ihg")
            if status and status.blocked_until and status.blocked_until.replace(tzinfo=now.tzinfo) > now:
                return 0
            lane = await db.get(AppSetting, LANE_KEY)
            sitemap_next = lane.value.get("sitemap_next", True) if lane else True
        # Each lane gets the same per-provider job budget, not two budgets
        # in a single tick. Source failures don't monopolize the other lane.
        discover = discover_ihg_sitemap if sitemap_next else discover_directory
        count = await discover(sessions, queue, settings, client)
        async with sessions() as db, db.begin():
            lane = await db.get(AppSetting, LANE_KEY)
            value = {"sitemap_next": not sitemap_next}
            if lane:
                lane.value = value
            else:
                db.add(AppSetting(key=LANE_KEY, value=value))
        return count
    finally:
        await queue.unlock("hotelbug:catalog-lane:ihg", token)
