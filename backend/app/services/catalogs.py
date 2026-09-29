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
