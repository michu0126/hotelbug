"""Official sitemap discovery; entries become verified detail-page jobs, not fake hotels."""

import re
from datetime import timedelta
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from xml.etree import ElementTree

import httpx
from sqlalchemy import func, select

from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.gha_catalog import hotel_candidate_url as gha_hotel_candidate
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import CATALOG_PRIORITY, LIVE, enqueue

ACCOR_SITEMAP = "https://all.accor.com/sitemap-fh.en.xml"
GHA_SITEMAP = "https://www.ghadiscovery.com/sitemap.xml"
HILTON_SITEMAP = "https://www.hilton.com/sitemap/en/sitemap-en.xml"


def hilton_links(content: bytes, index: bool = False) -> list[str]:
    root = ElementTree.fromstring(content)
    links = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc":
            continue
        url = urlparse((node.text or "").strip())
        if url.scheme != "https" or url.hostname != "www.hilton.com" or url.query or url.fragment:
            continue
        pattern = (
            r"/sitemap/en/sitemap-en-prop-[a-z0-9-]+\.xml"
            if index
            else r"/en/hotels/[a-z0-9]{7}-[a-z0-9-]+/?"
        )
        if re.fullmatch(pattern, url.path):
            links.add(url.geturl().rstrip("/"))
    return sorted(links)


def gha_links(content: bytes, index: bool = False) -> list[str]:
    root = ElementTree.fromstring(content)
    links = set()
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] != "loc":
            continue
        url = urlparse((node.text or "").strip())
        if url.scheme != "https" or url.netloc != "www.ghadiscovery.com" or url.query or url.fragment:
            continue
        if index:
            valid = re.fullmatch(r"/sitemap-\d+\.xml", url.path)
            if valid:
                links.add(url.geturl())
        else:
            candidate = gha_hotel_candidate(url.geturl())
            if candidate:
                links.add(candidate)
    return sorted(links)


async def discover_gha(sessions, queue, settings, client) -> int:
    return await _discover_index(sessions, queue, settings, client, "gha", GHA_SITEMAP, gha_links)


async def discover_hilton(sessions, queue, settings, client) -> int:
    return await _discover_index(sessions, queue, settings, client, "hilton", HILTON_SITEMAP, hilton_links)


def catalog_retry(exc, now) -> tuple[float, bool]:
    """Delay a failed source; explicit access/rate rejection pauses the catalog."""
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code in (401, 403):
            return 6 * 3600, True
        if exc.response.status_code == 429:
            value = exc.response.headers.get("Retry-After", "")
            try:
                delay = float(value)
                if not 0 <= delay < float("inf"):
                    raise ValueError("Invalid Retry-After")
            except ValueError:
                try:
                    until = parsedate_to_datetime(value)
                    if until.tzinfo is None:
                        raise ValueError("Retry-After must have a timezone")
                    delay = (until - now).total_seconds()
                except (ValueError, TypeError, OverflowError):
                    delay = 3600
            return max(30, delay), True
    return 3600, False


def read_sitemaps(data: dict) -> set[str]:
    """Legacy cursors only advanced on a successful or explicitly missing map."""
    maps = data.get("maps", [])
    missing = set(data.get("missing_maps", []))
    failed = set(data.get("map_failures", {}))
    if "read_maps" in data:
        return set(data["read_maps"]) & set(maps) - missing - failed
    return set(maps[: data.get("map_cursor", 0)]) - missing - failed


def source_pause(data, exc, now, delay):
    """Record only an explicit HTTP rejection, never a generic transport error."""
    data["source_pause_until"] = (now + timedelta(seconds=delay)).timestamp()
    data["source_error_code"] = "RATE_LIMITED" if exc.response.status_code == 429 else "BLOCKED_BY_ANTIBOT"


async def persist_source_pause(session, provider, data, now):
    """Share source rejection with Worker/year scheduling; preserve later pauses."""
    stamp = data.get("source_pause_until", 0)
    if stamp <= now.timestamp():
        return False
    state = await session.scalar(
        select(ProviderStatus).where(ProviderStatus.provider == provider).with_for_update()
    )
    deadline = now + timedelta(seconds=stamp - now.timestamp())
    if state is None:
        state = ProviderStatus(provider=provider)
        session.add(state)
    if not state.blocked_until or state.blocked_until.replace(tzinfo=now.tzinfo) < deadline:
        state.blocked_until = deadline
        state.last_error = data.get("source_error_code")
    state.status = "BLOCKED"
    # This is not a browser task or a successful scan; don't invent its metrics.
    return True


async def fetch_index_source(data, now, settings, client, index_url, parse_links):
    """One network read per tick; a broken map cannot trap later known maps."""
    stamp = now.timestamp()
    data["next_fetch_at"] = (now + timedelta(seconds=30)).timestamp()
    if stamp >= data.get("refresh_at", 0) and stamp >= data.get("index_retry_at", 0):
        try:
            response = await client.get(index_url, timeout=20)
            response.raise_for_status()
            maps = parse_links(response.content, index=True)
            if not maps:
                raise ValueError("No official sitemap children")
        except Exception as exc:
            delay, pause = catalog_retry(exc, now)
            data.update(
                index_error=type(exc).__name__, index_retry_at=stamp + delay, last_error=type(exc).__name__
            )
            if pause:
                data["next_fetch_at"] = stamp + delay
                source_pause(data, exc, now, delay)
        else:
            data.update(
                maps=maps,
                map_cursor=0,
                read_maps=[],
                map_failures={},
                urls=[],
                cursor=0,
                refresh_at=(now + timedelta(days=settings.discovery_interval_days)).timestamp(),
                index_retry_at=0,
                index_error=None,
                last_error=None,
                missing_maps=[],
                retry_last=False,
                source_pause_until=0,
                source_error_code=None,
            )
        return
    maps = data.get("maps", [])
    position = min(len(maps), max(0, data.get("map_cursor", 0)))
    failures = dict(data.get("map_failures", {}))
    due = sorted(
        (url for url, item in failures.items() if url in maps and item["next_retry_at"] <= stamp),
        key=lambda url: (failures[url]["next_retry_at"], url),
    )
    # Alternate overdue repair and unseen maps when both exist. Persist the
    # lane so restarting the Scheduler cannot starve one side.
    retry = bool(due) and (position == len(maps) or not data.get("retry_last", False))
    if not retry and position == len(maps):
        return
    url = due[0] if retry else maps[position]
    data["retry_last"] = retry
    read = read_sitemaps(data)
    missing = set(data.get("missing_maps", []))
    try:
        response = await client.get(url, timeout=20)
        if response.status_code in (404, 410):
            missing.add(url)
            read.discard(url)
            failures.pop(url, None)
            data["last_error"] = "SITEMAP_MISSING"
        else:
            response.raise_for_status()
            found = parse_links(response.content)
            urls = list(data.get("urls", []))
            seen = set(urls)
            urls.extend(item for item in found if item not in seen)
            data["urls"] = urls
            missing.discard(url)
            failures.pop(url, None)
            read.add(url)
            data["last_error"] = data.get("index_error")
            data.update(source_pause_until=0, source_error_code=None)
    except Exception as exc:
        delay, pause = catalog_retry(exc, now)
        failures[url] = {"next_retry_at": stamp + delay, "error": type(exc).__name__}
        read.discard(url)
        data["last_error"] = type(exc).__name__
        if pause:
            data["next_fetch_at"] = stamp + delay
            source_pause(data, exc, now, delay)
    if not retry:
        data["map_cursor"] = position + 1
    data.update(read_maps=sorted(read), missing_maps=sorted(missing), map_failures=failures)


async def _discover_index(
    sessions,
    queue,
    settings,
    client,
    provider,
    index_url,
    parse_links,
    *,
    state_key=None,
    candidate_code=None,
) -> int:
    """Walk one official sitemap per cycle, resume both page and hotel cursors."""
    if not settings.global_monitoring_enabled or not settings.provider_policy(provider).enabled:
        return 0
    lock_key = "hotelbug:catalog:" + provider
    state_key = state_key or "catalog:" + provider
    token = await queue.lock(lock_key, 60)
    if token is None:
        return 0
    try:
        async with sessions() as session, session.begin():
            state = await session.get(AppSetting, state_key)
            data = dict(state.value) if state else {}
            now = utcnow()
            provider_state = await session.get(ProviderStatus, provider)
            if (
                provider_state
                and provider_state.blocked_until
                and provider_state.blocked_until.replace(tzinfo=now.tzinfo) > now
            ):
                return 0
            if state is None:
                state = AppSetting(key=state_key, value={})
                session.add(state)
            if await persist_source_pause(session, provider, data, now):
                return 0
            if now.timestamp() >= data.get("next_fetch_at", 0):
                await fetch_index_source(data, now, settings, client, index_url, parse_links)
            if await persist_source_pause(session, provider, data, now):
                state.value = data
                return 0
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(settings.catalog_jobs_per_tick, max(0, settings.max_pending_jobs - pending))
            cursor = data.get("cursor", 0)
            urls = data.get("urls", [])
            selected = []
            skipped = set(data.get("skipped_non_hotel_urls", []))
            known_codes = set()
            if candidate_code is not None:
                cutoff = now - timedelta(days=settings.discovery_interval_days)
                known_codes.update(
                    await session.scalars(
                        select(Hotel.provider_hotel_id).where(
                            Hotel.provider == provider,
                            # An intentionally disabled hotel is not reactivated
                            # by a sitemap refresh. Stale active details may refresh.
                            (Hotel.updated_at >= cutoff) | Hotel.active.is_(False),
                        )
                    )
                )
                live_details = (
                    await session.scalars(
                        select(CrawlJob).where(
                            CrawlJob.provider == provider,
                            CrawlJob.kind == JobKind.DISCOVER_HOTELS,
                            CrawlJob.status.in_(LIVE),
                        )
                    )
                ).all()
                known_codes.update(candidate_code(job.payload.get("official_url")) for job in live_details)
            # Keep the old source list and ordinal cursor. Removing entries
            # would shift positions and can silently skip an unvisited hotel.
            while cursor < len(urls) and len(selected) < budget:
                url = urls[cursor]
                cursor += 1
                if provider == "gha" and gha_hotel_candidate(url) is None:
                    skipped.add(url)
                    continue
                if candidate_code is not None:
                    code = candidate_code(url)
                    if code is None or code in known_codes:
                        continue
                    known_codes.add(code)
                selected.append(url)
            for url in selected:
                payload = {"official_url": url}
                if state_key != "catalog:" + provider:
                    payload["catalog_source"] = state_key
                await enqueue(
                    session,
                    JobInput(
                        provider=provider,
                        kind=JobKind.DISCOVER_HOTELS,
                        priority=CATALOG_PRIORITY,
                        payload=payload,
                    ),
                )
            state.value = {**data, "cursor": cursor, "skipped_non_hotel_urls": sorted(skipped)}
            return len(selected)
    finally:
        await queue.unlock(lock_key, token)


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
            provider_state = await session.get(ProviderStatus, "accor")
            if (
                provider_state
                and provider_state.blocked_until
                and provider_state.blocked_until.replace(tzinfo=now.tzinfo) > now
            ):
                return 0
            data = dict(state.value) if state else {}
            if await persist_source_pause(session, "accor", data, now):
                return 0
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
                    delay, pause = catalog_retry(exc, now)
                    if pause:
                        source_pause(data, exc, now, delay)
                        data["refresh_at"] = data["source_pause_until"]
                else:
                    data.update(
                        codes=codes,
                        cursor=0,
                        last_error=None,
                        refresh_at=(now + timedelta(days=settings.discovery_interval_days)).timestamp(),
                        source_pause_until=0,
                        source_error_code=None,
                    )
                state.value = dict(data)
            if await persist_source_pause(session, "accor", data, now):
                return 0
            codes = data.get("codes", [])
            cursor = data.get("cursor", 0)
            pending = await session.scalar(
                select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE))
            )
            budget = min(settings.catalog_jobs_per_tick, max(0, settings.max_pending_jobs - pending))
            selected = codes[cursor : cursor + budget]
            for code in selected:
                await enqueue(
                    session,
                    JobInput(
                        provider="accor",
                        kind=JobKind.DISCOVER_HOTELS,
                        priority=CATALOG_PRIORITY,
                        payload={"provider_hotel_id": code},
                    ),
                )
            if state is not None:
                state.value = {**data, "cursor": cursor + len(selected)}
            return len(selected)
    finally:
        await queue.unlock("hotelbug:catalog:accor", token)
