from contextlib import asynccontextmanager
from datetime import date, timedelta
from hmac import compare_digest
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import get_session
from app.models.tables import (
    AppSetting,
    CrawlJob,
    Hotel,
    NotificationLog,
    PriceAlert,
    PriceHistory,
    ProviderStatus,
    Rate,
    Watchlist,
    utcnow,
)
from app.providers.gha_catalog import candidate_progress as gha_candidate_progress
from app.providers.ihg_sitemap import STATE_KEY as IHG_SITEMAP_STATE_KEY
from app.providers.ihg_sitemap import property_code as ihg_sitemap_code
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, JobKind, ProviderName
from app.schemas.watchlists import TARGET_FIELDS, WatchInput, WatchToggle
from app.services.calendar import get_calendar, month_bounds, month_dates
from app.services.catalog_coverage import directory_coverage
from app.services.catalogs import read_sitemaps
from app.services.history import get_offer_trend, get_stay_offers
from app.services.ihg_catalogs import directory_policy
from app.services.jobs import enqueue
from app.services.offer_policy import classify_offer
from app.services.rates import upsert_hotel
from app.services.runtime_settings import (
    MonitoringInput,
    load_runtime_settings,
    public_monitoring_settings,
    save_runtime_settings,
)

settings = get_settings()
redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging(settings.log_level)
    yield
    await redis.aclose()


app = FastAPI(title="Hotel Bug Price Monitor", version="2.0.0-phase2-preview", lifespan=lifespan)
Db = Annotated[AsyncSession, Depends(get_session)]


async def telegram_http_client(db: Db):
    runtime = await load_runtime_settings(db, settings)
    async with httpx.AsyncClient(
        follow_redirects=False,
        proxy=runtime.telegram_proxy_url.get_secret_value()
        or runtime.browser_proxy_url.get_secret_value()
        or None,
    ) as client:
        yield client


TelegramClient = Annotated[httpx.AsyncClient, Depends(telegram_http_client)]


def admin(authorization: Annotated[str | None, Header()] = None) -> None:
    secret = settings.admin_token.get_secret_value()
    if not secret:
        raise HTTPException(503, "Set ADMIN_TOKEN to enable management writes")
    if not authorization or not compare_digest(authorization, "Bearer " + secret):
        raise HTTPException(401, "Invalid management token")


def encode(row) -> dict:
    return jsonable_encoder({column.name: getattr(row, column.name) for column in row.__table__.columns})


class TelegramSettingsInput(BaseModel):
    bot_token: str | None = Field(default=None, max_length=256)
    chat_id: str = Field(min_length=1, max_length=128)
    enabled: bool = True


@app.get("/api/settings/monitoring", dependencies=[Depends(admin)])
async def get_monitoring_settings(db: Db):
    return public_monitoring_settings(await load_runtime_settings(db, settings), set(FACTORIES))


@app.put("/api/settings/monitoring", dependencies=[Depends(admin)])
async def put_monitoring_settings(data: MonitoringInput, db: Db):
    if data.providers and any(
        policy.enabled and name not in FACTORIES for name, policy in data.providers.items()
    ):
        raise HTTPException(409, "Cannot enable a provider whose browser adapter is not implemented")
    await save_runtime_settings(db, data)
    await db.commit()
    return public_monitoring_settings(await load_runtime_settings(db, settings), set(FACTORIES))


@app.get("/api/settings/telegram", dependencies=[Depends(admin)])
async def get_telegram_settings(db: Db):
    row = await db.get(AppSetting, "telegram")
    return {
        "configured": bool(
            (row.value.get("bot_token") if row else None) or settings.telegram_bot_token.get_secret_value()
        )
        and bool((row.value.get("chat_id") if row else None) or settings.telegram_chat_id),
        "chat_id": row.value.get("chat_id", "") if row else settings.telegram_chat_id,
        "enabled": row.value.get("enabled", True) if row else True,
    }


@app.put("/api/settings/telegram", dependencies=[Depends(admin)])
async def put_telegram_settings(data: TelegramSettingsInput, db: Db):
    row = await db.get(AppSetting, "telegram")
    values = dict(row.value) if row else {}
    if data.bot_token is not None:
        values["bot_token"] = data.bot_token.strip()
    values.update(chat_id=data.chat_id.strip(), enabled=data.enabled)
    if not values["chat_id"]:
        raise HTTPException(422, "Chat ID is required")
    if row:
        row.value = values
        row.updated_at = utcnow()
    else:
        db.add(AppSetting(key="telegram", value=values))
    await db.commit()
    return {
        "configured": bool(values.get("bot_token") or settings.telegram_bot_token.get_secret_value()),
        "chat_id": values["chat_id"],
        "enabled": values["enabled"],
    }


@app.post("/api/settings/telegram/test", dependencies=[Depends(admin)])
async def test_telegram_settings(db: Db, client: TelegramClient):
    row = await db.get(AppSetting, "telegram")
    token = (row.value.get("bot_token") if row else None) or settings.telegram_bot_token.get_secret_value()
    chat_id = (row.value.get("chat_id") if row else None) or settings.telegram_chat_id
    if not token or not chat_id:
        raise HTTPException(422, "Save a Telegram Bot Token and Chat ID first")
    try:
        response = await client.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": "Hotel Bug Price Monitor：这是一条手动测试消息。",
                "link_preview_options": {"is_disabled": True},
            },
            timeout=15,
        )
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "Telegram test request failed") from None
    if not response.is_success or not isinstance(result, dict) or result.get("ok") is not True:
        code = (
            result.get("error_code", response.status_code)
            if isinstance(result, dict)
            else response.status_code
        )
        raise HTTPException(502, f"Telegram rejected test message (code {code})")
    return {"delivered": True}


@app.get("/api/health/live")
async def live():
    return {"status": "ok", "phase": "2-preview"}


@app.get("/api/health/ready")
async def ready(db: Db):
    try:
        await db.execute(text("SELECT 1 FROM alembic_version LIMIT 1"))
        await redis.ping()
    except Exception:
        raise HTTPException(503, "Database, migrations or Redis unavailable") from None
    return {"status": "ready"}


@app.get("/api/dashboard")
async def dashboard(db: Db):
    runtime = await load_runtime_settings(db, settings)
    midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "phase": "2-preview",
        "global_monitoring_enabled": runtime.global_monitoring_enabled,
        "alert_drop_fraction": str(runtime.alert_drop_fraction),
        "alert_min_history_days": runtime.alert_min_history_days,
        "message": "IHG、雅高与 GHA 官网页面采集已通过开发机单酒店 Worker 入库实测；万豪、希尔顿、凯悦、群晖实采与全球覆盖仍在验证。降价复查与 Telegram 发送链路已实现。",
        "hotels": await db.scalar(select(func.count()).select_from(Hotel)),
        "rates_today": await db.scalar(
            select(func.count()).select_from(PriceHistory).where(PriceHistory.captured_at >= midnight)
        ),
        "confirmed_alerts": await db.scalar(
            select(func.count()).select_from(PriceAlert).where(PriceAlert.confirmed.is_(True))
        ),
        "queued_jobs": await db.scalar(
            select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(("PENDING", "QUEUED")))
        ),
        "worker_heartbeat": await redis.get("hotelbug:heartbeat:worker"),
        "scheduler_heartbeat": await redis.get("hotelbug:heartbeat:scheduler"),
    }


@app.get("/api/providers")
async def providers(db: Db):
    runtime = await load_runtime_settings(db, settings)
    states = {s.provider: s for s in (await db.scalars(select(ProviderStatus))).all()}
    result = []
    for name in ProviderName:
        state = states.get(name.value)
        item = (
            encode(state)
            if state
            else {
                "provider": name,
                "status": "DEGRADED" if name in FACTORIES else "BROKEN",
                "last_error": "NOT_YET_RUN" if name in FACTORIES else "NOT_IMPLEMENTED",
                "requests_today": 0,
                "success_count": 0,
                "failure_count": 0,
            }
        )
        item["implemented"] = name in FACTORIES
        item["enabled"] = runtime.provider_policy(name).enabled and item["implemented"]
        item["verification"] = {
            "marriott": "BROWSER_OBSERVED_HTTP_UNVERIFIED",
            "accor": "PUBLIC_PAGE_SAMPLE_VERIFIED",
            "gha": "PUBLIC_PAGE_SAMPLE_VERIFIED",
            "hilton": "PUBLIC_PAGE_OBSERVED_WORKER_UNVERIFIED",
            "ihg": "PUBLIC_PAGE_SAMPLE_VERIFIED",
            "hyatt": "PUBLIC_PAGE_OBSERVED_WORKER_UNVERIFIED",
        }.get(name, "NOT_IMPLEMENTED")
        total = item["success_count"] + item["failure_count"]
        item["success_rate"] = item["success_count"] / total if total else None
        result.append(item)
    return result


@app.get("/api/catalogs")
async def catalogs(db: Db):
    runtime = await load_runtime_settings(db, settings)
    result = []
    for provider in ProviderName:
        row = await db.get(AppSetting, "catalog:" + provider.value)
        data = row.value if row else {}
        pages = list(data.get("pages", {}).values())
        maps = data.get("maps", [])
        candidates = (
            data.get("codes", [])
            if provider == ProviderName.ACCOR
            else data.get("urls", [])
            if provider in {ProviderName.GHA, ProviderName.HILTON}
            else []
        )
        missing_maps = len(data.get("missing_maps", []))
        map_count = len(maps) if provider != ProviderName.ACCOR else int(bool(candidates))
        maps_read = len(read_sitemaps(data)) if provider != ProviderName.ACCOR else int(bool(candidates))
        candidate_counts = (
            gha_candidate_progress(candidates, data.get("cursor", 0))
            if provider == ProviderName.GHA
            else {
                "hotel_candidates": len(candidates),
                "candidate_tasks_dispatched": min(len(candidates), data.get("cursor", 0)),
                "excluded_non_hotel_entries": 0,
            }
        )
        auxiliary_sitemap = None
        if provider == ProviderName.IHG:
            supplemental = await db.get(AppSetting, IHG_SITEMAP_STATE_KEY)
            if supplemental:
                source = supplemental.value
                source_maps = source.get("maps", [])
                codes = {ihg_sitemap_code(url) for url in source.get("urls", [])}
                codes.discard(None)
                auxiliary_sitemap = {
                    "sitemaps_total": len(source_maps),
                    "sitemaps_read": len(read_sitemaps(source)),
                    "sitemaps_missing": len(source.get("missing_maps", [])),
                    "sitemaps_failed": len(set(source.get("map_failures", {})) & set(source_maps)),
                    "hotel_candidates": len(codes),
                    # Some cached positions were already-known hotels or live
                    # tasks. This cursor is not a dispatched/successful count.
                    "candidate_positions_processed": min(
                        len(source.get("urls", [])), source.get("cursor", 0)
                    ),
                    "last_error": source.get("last_error"),
                }
        coverage = (
            directory_coverage(
                data, directory_policy(provider.value)[0], utcnow(), runtime.discovery_interval_days
            )
            if provider in {ProviderName.IHG, ProviderName.MARRIOTT, ProviderName.HYATT}
            else {}
        )
        result.append(
            {
                **coverage,
                "provider": provider.value,
                "enabled": runtime.global_monitoring_enabled and runtime.provider_policy(provider).enabled,
                "source": "OFFICIAL_BROWSER_DIRECTORY"
                if provider in {ProviderName.IHG, ProviderName.MARRIOTT, ProviderName.HYATT}
                else "OFFICIAL_SITEMAP"
                if provider.value in {"accor", "gha", "hilton"}
                else "NOT_IMPLEMENTED",
                "hotels": await db.scalar(
                    select(func.count()).select_from(Hotel).where(Hotel.provider == provider.value)
                ),
                "hotels_with_quotes": await db.scalar(
                    select(func.count(func.distinct(PriceHistory.hotel_id))).where(
                        PriceHistory.provider == provider.value
                    )
                ),
                "directory_pages": len(pages),
                "parsed_directory_pages": sum(p.get("status") in {"SUCCEEDED", "PARTIAL"} for p in pages),
                "partial_directory_pages": sum(p.get("status") == "PARTIAL" for p in pages),
                "failed_directory_pages": sum(p.get("status") == "FAILED" for p in pages),
                "unparsed_hotel_links": sum(p.get("unparsed_hotel_links", 0) for p in pages),
                "sitemaps_total": map_count,
                "sitemaps_read": maps_read,
                "sitemaps_missing": missing_maps,
                "sitemaps_failed": len(set(data.get("map_failures", {})) & set(maps)),
                "sitemap_index_error": data.get("index_error"),
                **candidate_counts,
                "auxiliary_sitemap": auxiliary_sitemap,
                "last_error": data.get("last_error"),
            }
        )
    return result


@app.get("/api/hotels")
async def hotels(
    db: Db,
    q: str = "",
    provider: ProviderName | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    query = select(Hotel)
    if provider:
        query = query.where(Hotel.provider == provider)
    if q:
        pattern = "%" + q[:200] + "%"
        query = query.where(
            or_(*(c.ilike(pattern) for c in (Hotel.hotel_name, Hotel.city, Hotel.country, Hotel.brand)))
        )
    return [
        encode(h)
        for h in (await db.scalars(query.order_by(Hotel.hotel_name).offset(offset).limit(limit))).all()
    ]


@app.post("/api/hotels", dependencies=[Depends(admin)])
async def add_hotel(data: HotelData, db: Db):
    row = await upsert_hotel(db, data)
    await db.commit()
    return encode(row)


@app.get("/api/hotels/{hotel_id}")
async def hotel_detail(hotel_id: str, db: Db):
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    latest = (
        await db.scalars(
            select(Rate).where(Rate.hotel_id == hotel_id).order_by(Rate.captured_at.desc()).limit(100)
        )
    ).all()
    return {
        "hotel": encode(hotel),
        "latest_rates": [{**encode(r), "offer_classification": classify_offer(r).as_dict()} for r in latest],
    }


@app.get("/api/hotels/{hotel_id}/history")
async def history(hotel_id: str, db: Db, days: int = Query(30, ge=1, le=90), offer_key: str | None = None):
    query = select(PriceHistory).where(
        PriceHistory.hotel_id == hotel_id, PriceHistory.captured_at >= utcnow() - timedelta(days=days)
    )
    if offer_key:
        query = query.where(PriceHistory.offer_key == offer_key)
    return [
        {**encode(r), "offer_classification": classify_offer(r).as_dict()}
        for r in (await db.scalars(query.order_by(PriceHistory.captured_at.desc()).limit(1000))).all()
    ]


@app.get("/api/hotels/{hotel_id}/calendar")
async def calendar(hotel_id: str, db: Db, month: str):
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    try:
        month_bounds(month)
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM") from None
    return await get_calendar(db, hotel, month)


@app.get("/api/hotels/{hotel_id}/offers")
async def stay_offers(
    hotel_id: str,
    db: Db,
    check_in: date,
    check_out: date | None = None,
    cursor: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    limit: int = Query(100, ge=1, le=200),
):
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    if check_out is not None and check_out <= check_in:
        raise HTTPException(422, "check_out must be after check_in")
    return await get_stay_offers(db, hotel, check_in, check_out=check_out, after=cursor, limit=limit)


@app.get("/api/hotels/{hotel_id}/trend")
async def offer_trend(
    hotel_id: str,
    db: Db,
    offer_key: str = Query(pattern=r"^[a-f0-9]{64}$"),
    days: int = Query(30, ge=1, le=90),
):
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    result = await get_offer_trend(db, hotel, offer_key, days)
    if result is None:
        raise HTTPException(404, "No recorded offer for this hotel")
    return result


@app.post("/api/hotels/{hotel_id}/calendar/jobs", dependencies=[Depends(admin)])
async def enqueue_calendar(hotel_id: str, db: Db, month: str):
    runtime = await load_runtime_settings(db, settings)
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    if hotel.provider not in FACTORIES:
        raise HTTPException(409, "No verified calendar adapter for this hotel")
    if not runtime.provider_policy(hotel.provider).enabled:
        raise HTTPException(409, "Provider disabled")
    try:
        dates = month_dates(month)
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM") from None
    today = date.today()
    eligible = [day for day in dates if today <= day < today + timedelta(days=365)]
    if not eligible:
        raise HTTPException(422, "Month outside the next 365 days")
    rows = [
        await enqueue(
            db,
            JobInput(
                provider=hotel.provider,
                kind=JobKind.FETCH_CALENDAR,
                hotel_id=hotel.id,
                check_in=day,
                check_out=day + timedelta(days=1),
                priority=60,
            ),
        )
        for day in eligible
    ]
    await db.commit()
    return {"month": month, "queued_dates": len(eligible), "job_ids": [r.id for r in rows]}


@app.get("/api/watchlists")
async def watchlists(db: Db, limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0)):
    rows = (
        await db.scalars(
            select(Watchlist).order_by(Watchlist.created_at, Watchlist.id).offset(offset).limit(limit)
        )
    ).all()
    return [encode(row) for row in rows]


@app.get("/api/watchlists/targets")
async def watch_targets(
    db: Db,
    scope: Literal["city", "country", "brand"],
    provider: ProviderName | None = None,
    q: str = Query("", max_length=100),
    limit: int = Query(100, ge=1, le=200),
):
    field = getattr(Hotel, scope)
    value = func.trim(field)
    query = select(value).where(Hotel.active.is_(True), field.is_not(None), value != "")
    if provider is not None:
        query = query.where(Hotel.provider == provider.value)
    if q.strip():
        query = query.where(field.icontains(q.strip(), autoescape=True))
    return list(await db.scalars(query.distinct().order_by(value).limit(limit)))


@app.post("/api/watchlists", dependencies=[Depends(admin)])
async def add_watchlist(data: WatchInput, db: Db):
    filters = data.filters()
    field = TARGET_FIELDS[data.scope]
    default_name = f"{data.scope}: {filters[field]}"
    if data.scope == "hotel":
        hotel = await db.get(Hotel, data.hotel_id)
        if not hotel:
            raise HTTPException(404, "Hotel not found")
        if hotel.provider not in FACTORIES:
            raise HTTPException(409, "Provider does not yet have a rate adapter")
        default_name = hotel.hotel_name
    target_column = Watchlist.filters[field].as_string()
    matches = (
        target_column == filters[field]
        if data.scope == "hotel"
        else func.lower(func.trim(target_column)) == func.lower(filters[field])
    )
    conditions = [Watchlist.scope == data.scope, matches]
    if data.scope != "hotel":
        provider = filters.get("provider")
        conditions.append(
            Watchlist.filters["provider"].as_string() == provider
            if provider
            else Watchlist.filters["provider"].as_string().is_(None)
        )
    existing = await db.scalar(
        select(Watchlist).where(*conditions).order_by(Watchlist.created_at, Watchlist.id).limit(1)
    )
    if existing:
        existing.filters = filters
        existing.enabled = data.enabled
        if data.name is not None:
            existing.name = data.name
        await db.commit()
        return encode(existing)
    row = Watchlist(
        name=data.name or default_name,
        scope=data.scope,
        filters=filters,
        enabled=data.enabled,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return encode(row)


@app.patch("/api/watchlists/{watch_id}", dependencies=[Depends(admin)])
async def toggle_watchlist(watch_id: str, data: WatchToggle, db: Db):
    row = await db.get(Watchlist, watch_id)
    if row is None:
        raise HTTPException(404, "Watchlist not found")
    row.enabled = data.enabled
    await db.commit()
    return encode(row)


@app.get("/api/jobs")
async def jobs(db: Db, limit: int = Query(50, ge=1, le=200)):
    rows = (await db.scalars(select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(limit))).all()
    # Payload may be provider-specific; expose only operational metadata.
    return [{k: v for k, v in encode(r).items() if k not in ("payload", "lease_owner")} for r in rows]


@app.post("/api/jobs", dependencies=[Depends(admin)])
async def add_job(data: JobInput, db: Db):
    runtime = await load_runtime_settings(db, settings)
    if data.provider not in FACTORIES:
        raise HTTPException(409, "Provider not implemented; no requests will be sent")
    if not runtime.provider_policy(data.provider).enabled:
        raise HTTPException(409, "Provider disabled")
    if data.kind == JobKind.VERIFY_ANOMALY:
        raise HTTPException(409, "Verification jobs are created automatically from detected price drops")
    if data.hotel_id:
        hotel = await db.get(Hotel, data.hotel_id)
        if not hotel or hotel.provider != data.provider:
            raise HTTPException(400, "Hotel/provider mismatch")
    try:
        row = await enqueue(db, data)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    await db.commit()
    return {"id": row.id, "status": row.status}


@app.get("/api/alerts")
async def alerts(db: Db, limit: int = Query(50, ge=1, le=200)):
    rows = (await db.scalars(select(PriceAlert).order_by(PriceAlert.created_at.desc()).limit(limit))).all()
    return [encode(row) for row in rows]


@app.get("/api/notifications", dependencies=[Depends(admin)])
async def notifications(db: Db, limit: int = Query(50, ge=1, le=200)):
    rows = (await db.scalars(select(NotificationLog).order_by(NotificationLog.id.desc()).limit(limit))).all()
    return [encode(row) for row in rows]
