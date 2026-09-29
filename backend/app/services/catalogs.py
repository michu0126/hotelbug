"""Official sitemap discovery; entries become verified detail-page jobs, not fake hotels."""

import re
from datetime import timedelta
from urllib.parse import urlparse
from xml.etree import ElementTree

from sqlalchemy import func, select

from app.models.tables import AppSetting, CrawlJob, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import LIVE, enqueue

ACCOR_SITEMAP = "https://all.accor.com/sitemap-fh.en.xml"
GHA_SITEMAP = "https://www.ghadiscovery.com/sitemap.xml"


def gha_links(content: bytes, index: bool = False) -> list[str]:
    root = ElementTree.fromstring(content)
    links = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc":
            continue
        url = urlparse((node.text or "").strip())
        if url.scheme != "https" or url.hostname != "www.ghadiscovery.com" or url.query:
            continue
        if index:
            valid = re.fullmatch(r"/sitemap-\d+\.xml", url.path)
        else:
            parts = url.path.strip("/").split("/")
            if len(parts) > 2 and parts[2] in {"stay-offers", "local-offers", "experiences", "dining"}:
                # Offer pages also advertise their parent hotel, verified by the Worker.
                url = url._replace(path="/" + "/".join(parts[:2]))
            valid = re.fullmatch(r"/[a-z0-9-]+/[a-z0-9-]+/?", url.path)
            if url.path.split("/")[1] in {
                "promotions",
                "our-partners",
                "member",
                "booking",
                "blog",
                "destinations",
            }:
                valid = False
        if valid:
            links.add(url.geturl().rstrip("/"))
    return sorted(links)


async def discover_gha(sessions, queue, settings, client) -> int:
    """Walk one official sitemap per cycle, resume both page and hotel cursors."""
    if not settings.global_monitoring_enabled or not settings.provider_policy("gha").enabled:
        return 0
    token = await queue.lock("hotelbug:catalog:gha", 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            state = await session.get(AppSetting, "catalog:gha")
            data = dict(state.value) if state else {}
            now = utcnow()
            if state is None:
                state = AppSetting(key="catalog:gha", value={})
                session.add(state)
            if now.timestamp() >= data.get("next_fetch_at", 0):
                try:
                    if now.timestamp() >= data.get("refresh_at", 0):
                        response = await client.get(GHA_SITEMAP, timeout=20)
                        response.raise_for_status()
                        maps = gha_links(response.content, index=True)
                        if not maps:
                            raise ValueError("No official GHA sitemap children")
                        data.update(
                            maps=maps,
                            map_cursor=0,
                            urls=[],
                            cursor=0,
                            refresh_at=(now + timedelta(days=settings.discovery_interval_days)).timestamp(),
                            last_error=None,
                            missing_maps=[],
                        )
                    else:
                        maps = data.get("maps", [])
                        position = data.get("map_cursor", 0)
                        if position < len(maps):
                            response = await client.get(maps[position], timeout=20)
                            if response.status_code in (404, 410):
                                data["missing_maps"] = [*data.get("missing_maps", []), maps[position]]
                                data.update(map_cursor=position + 1, last_error="SITEMAP_MISSING")
                            else:
                                response.raise_for_status()
                                # Append rather than re-sort: already dispatched offsets stay valid.
                                urls = list(data.get("urls", []))
                                seen = set(urls)
                                urls.extend(url for url in gha_links(response.content) if url not in seen)
                                data.update(urls=urls, map_cursor=position + 1, last_error=None)
                    data["next_fetch_at"] = (now + timedelta(seconds=30)).timestamp()
                except Exception as exc:
                    data.update(
                        last_error=type(exc).__name__, next_fetch_at=(now + timedelta(hours=1)).timestamp()
                    )
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(10, max(0, settings.max_pending_jobs - pending))
            cursor = data.get("cursor", 0)
            selected = data.get("urls", [])[cursor : cursor + budget]
            for url in selected:
                await enqueue(
                    session,
                    JobInput(provider="gha", kind=JobKind.DISCOVER_HOTELS, payload={"official_url": url}),
                )
            state.value = {**data, "cursor": cursor + len(selected)}
            return len(selected)
    finally:
        await queue.unlock("hotelbug:catalog:gha", token)


def accor_codes(content: bytes) -> list[str]:
    root = ElementTree.fromstring(content)
    codes = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc":
            continue
        url = urlparse((node.text or "").strip())
        match = re.fullmatch(r"/hotel/([A-Za-z0-9]{4})/index.en.shtml", url.path)
        if url.scheme == "https" and url.hostname == "all.accor.com" and match:
            codes.add(match[1].upper())
    if not codes:
        raise ValueError("Official Accor sitemap contained no recognized hotel entries")
    return sorted(codes)


async def discover_accor(sessions, queue, settings, client) -> int:
    if not settings.global_monitoring_enabled or not settings.provider_policy("accor").enabled:
        return 0
    token = await queue.lock("hotelbug:catalog:accor", 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            state = await session.get(AppSetting, "catalog:accor")
            now = utcnow()
            data = dict(state.value) if state else {}
            # Retry failed sitemap requests at most hourly, including after restart.
            if now.timestamp() >= data.get("refresh_at", 0):
                data["refresh_at"] = (now + timedelta(hours=1)).timestamp()
                if state is None:
                    state = AppSetting(key="catalog:accor", value=data)
                    session.add(state)
                try:
                    response = await client.get(ACCOR_SITEMAP, timeout=20)
                    response.raise_for_status()
                    codes = accor_codes(response.content)
                except Exception as exc:
                    data["last_error"] = type(exc).__name__
                else:
                    data.update(
                        codes=codes,
                        cursor=0,
                        last_error=None,
                        refresh_at=(now + timedelta(days=settings.discovery_interval_days)).timestamp(),
                    )
                state.value = dict(data)
            codes = data.get("codes", [])
            cursor = data.get("cursor", 0)
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(10, max(0, settings.max_pending_jobs - pending))
            selected = codes[cursor : cursor + budget]
            for code in selected:
                await enqueue(
                    session,
                    JobInput(
                        provider="accor", kind=JobKind.DISCOVER_HOTELS, payload={"provider_hotel_id": code}
                    ),
                )
            if state is not None:
                state.value = {**data, "cursor": cursor + len(selected)}
            return len(selected)
    finally:
        await queue.unlock("hotelbug:catalog:accor", token)
