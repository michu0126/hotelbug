from contextlib import asynccontextmanager
from datetime import date, timedelta
from hmac import compare_digest
from typing import Annotated

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
    CrawlJob,
    Hotel,
    PriceAlert,
    PriceHistory,
    ProviderStatus,
    Rate,
    Watchlist,
    utcnow,
)
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, JobKind, ProviderName
from app.services.calendar import get_calendar, month_bounds, month_dates
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel

settings = get_settings()
redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging(settings.log_level)
    yield
    await redis.aclose()


app = FastAPI(title="Hotel Bug Price Monitor", version="2.0.0-phase2-preview", lifespan=lifespan)
Db = Annotated[AsyncSession, Depends(get_session)]


def admin(authorization: Annotated[str | None, Header()] = None) -> None:
    secret = settings.admin_token.get_secret_value()
    if not secret:
        raise HTTPException(503, "Set ADMIN_TOKEN to enable management writes")
    if not authorization or not compare_digest(authorization, "Bearer " + secret):
        raise HTTPException(401, "Invalid management token")


def encode(row) -> dict:
    return jsonable_encoder({column.name: getattr(row, column.name) for column in row.__table__.columns})


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
    midnight = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return {
        "phase": "2-preview",
        "message": "万豪公开报价适配器处于实验阶段；独立 HTTP 访问尚未验证成功，默认关闭。",
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
    states = {s.provider: s for s in (await db.scalars(select(ProviderStatus))).all()}
    result = []
    for name in ProviderName:
        state = states.get(name.value)
        item = (
            encode(state)
            if state
            else {
                "provider": name,
                "status": "DEGRADED" if name == ProviderName.MARRIOTT else "BROKEN",
                "last_error": "HTTP_UNVERIFIED" if name == ProviderName.MARRIOTT else "NOT_IMPLEMENTED",
                "requests_today": 0,
                "success_count": 0,
                "failure_count": 0,
            }
        )
        item["implemented"] = name in FACTORIES
        item["enabled"] = settings.provider_policy(name).enabled and item["implemented"]
        item["verification"] = (
            "BROWSER_OBSERVED_HTTP_UNVERIFIED" if name == ProviderName.MARRIOTT else "NOT_IMPLEMENTED"
        )
        total = item["success_count"] + item["failure_count"]
        item["success_rate"] = item["success_count"] / total if total else None
        result.append(item)
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
    return {"hotel": encode(hotel), "latest_rates": [encode(r) for r in latest]}


@app.get("/api/hotels/{hotel_id}/history")
async def history(hotel_id: str, db: Db, days: int = Query(30, ge=1, le=90), offer_key: str | None = None):
    query = select(PriceHistory).where(
        PriceHistory.hotel_id == hotel_id, PriceHistory.captured_at >= utcnow() - timedelta(days=days)
    )
    if offer_key:
        query = query.where(PriceHistory.offer_key == offer_key)
    return [
        encode(r)
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


@app.post("/api/hotels/{hotel_id}/calendar/jobs", dependencies=[Depends(admin)])
async def enqueue_calendar(hotel_id: str, db: Db, month: str):
    hotel = await db.get(Hotel, hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    if hotel.provider != ProviderName.MARRIOTT or hotel.provider not in FACTORIES:
        raise HTTPException(409, "No verified calendar adapter for this hotel")
    if not settings.provider_policy(hotel.provider).enabled:
        raise HTTPException(409, "Provider disabled")
    try:
        dates = month_dates(month)
    except ValueError:
        raise HTTPException(422, "month must be YYYY-MM") from None
    today = date.today()
    eligible = [day for day in dates if today <= day <= today + timedelta(days=365)]
    if not eligible:
        raise HTTPException(422, "Month outside the next 365 days")
    rows = [
        await enqueue(
            db,
            JobInput(
                provider=ProviderName.MARRIOTT,
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


class HotelWatchInput(BaseModel):
    hotel_id: str
    days_ahead: int = Field(default=30, ge=1, le=365)


@app.get("/api/watchlists")
async def watchlists(db: Db):
    rows = (await db.scalars(select(Watchlist).order_by(Watchlist.created_at).limit(100))).all()
    return [encode(row) for row in rows]


@app.post("/api/watchlists", dependencies=[Depends(admin)])
async def add_watchlist(data: HotelWatchInput, db: Db):
    hotel = await db.get(Hotel, data.hotel_id)
    if not hotel:
        raise HTTPException(404, "Hotel not found")
    if hotel.provider != ProviderName.MARRIOTT:
        raise HTTPException(409, "Only Marriott hotel watchlists are in Phase 2 preview")
    existing = await db.scalar(
        select(Watchlist).where(
            Watchlist.scope == "hotel",
            Watchlist.enabled.is_(True),
            Watchlist.filters["hotel_id"].as_string() == hotel.id,
        )
    )
    if existing:
        existing.filters = {"hotel_id": hotel.id, "days_ahead": data.days_ahead}
        await db.commit()
        return encode(existing)
    row = Watchlist(
        name=hotel.hotel_name,
        scope="hotel",
        filters={"hotel_id": hotel.id, "days_ahead": data.days_ahead},
        enabled=True,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return encode(row)


@app.get("/api/jobs")
async def jobs(db: Db, limit: int = Query(50, ge=1, le=200)):
    rows = (await db.scalars(select(CrawlJob).order_by(CrawlJob.created_at.desc()).limit(limit))).all()
    # Payload may be provider-specific; expose only operational metadata.
    return [{k: v for k, v in encode(r).items() if k not in ("payload", "lease_owner")} for r in rows]


@app.post("/api/jobs", dependencies=[Depends(admin)])
async def add_job(data: JobInput, db: Db):
    if data.provider not in FACTORIES:
        raise HTTPException(409, "Provider not implemented; no requests will be sent")
    if not settings.provider_policy(data.provider).enabled:
        raise HTTPException(409, "Provider disabled")
    if data.kind == JobKind.VERIFY_ANOMALY:
        raise HTTPException(409, "Confirmation engine pending Phase 3")
    if data.hotel_id:
        hotel = await db.get(Hotel, data.hotel_id)
        if not hotel or hotel.provider != data.provider:
            raise HTTPException(400, "Hotel/provider mismatch")
    row = await enqueue(db, data)
    await db.commit()
    return {"id": row.id, "status": row.status}
