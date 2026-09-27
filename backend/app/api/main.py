from contextlib import asynccontextmanager
from datetime import timedelta
from hmac import compare_digest
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from redis.asyncio import Redis
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.database.session import get_session
from app.models.tables import CrawlJob, Hotel, PriceAlert, PriceHistory, ProviderStatus, Rate, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import HotelData, JobInput, JobKind, ProviderName
from app.services.jobs import enqueue
from app.services.rates import upsert_hotel

settings = get_settings()
redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)


@asynccontextmanager
async def lifespan(application: FastAPI):
    configure_logging(settings.log_level)
    yield
    await redis.aclose()


app = FastAPI(title="Hotel Bug Price Monitor", version="2.0.0-phase1", lifespan=lifespan)
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
    return {"status": "ok", "phase": 1}


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
        "phase": 1,
        "message": "基础框架已运行；尚未接入生产 Provider，不生成模拟报价。",
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
                "status": "BROKEN",
                "last_error": "NOT_IMPLEMENTED",
                "requests_today": 0,
                "success_count": 0,
                "failure_count": 0,
            }
        )
        item["implemented"] = name in FACTORIES
        item["enabled"] = settings.provider_policy(name).enabled and item["implemented"]
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
