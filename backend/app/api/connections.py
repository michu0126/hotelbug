import asyncio
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select

from app.core.config import get_settings
from app.database.session import get_session
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, ProviderStatus, utcnow
from app.schemas.domain import JobInput, JobKind, ProviderName
from app.services.jobs import enqueue, within_monitoring_window
from app.services.official_connections import (
    DOMAINS,
    LIMIT,
    connection_summary,
    load_connection,
    save_connection,
)
from app.services.runtime_settings import load_runtime_settings


def connection_router(admin_dependency):
    router = APIRouter(prefix="/api/settings/connections", dependencies=[Depends(admin_dependency)])

    async def view(db, provider, value):
        result = connection_summary(provider, value)
        record = await db.get(AppSetting, "official_connection_test:" + provider)
        job = await db.get(CrawlJob, record.value.get("job_id")) if record else None
        if job and record.value.get("revision") == result["revision"]:
            result["last_test"] = dict(
                job_id=job.id,
                status=job.status,
                error_type=job.error_type,
                check_in=str(job.check_in),
                hotel_id=job.hotel_id,
                meaning="QUOTE_JOB_NOT_LOGIN_VERIFICATION",
                quote_count=await db.scalar(
                    select(func.count()).select_from(PriceHistory).where(PriceHistory.job_id == job.id)
                ),
            )
        else:
            result["last_test"] = None
        state = await db.get(ProviderStatus, provider)
        result["blocked_until"] = state.blocked_until.isoformat() if state and state.blocked_until else None
        return result

    @router.get("")
    async def list_connections(response: Response, db=Depends(get_session)):
        response.headers["Cache-Control"] = "no-store"
        runtime = await load_runtime_settings(db, get_settings())
        result = []
        for provider in DOMAINS:
            try:
                value = await asyncio.to_thread(load_connection, runtime.browser_session_dir, provider)
                result.append(await view(db, provider, value))
            except ValueError:
                result.append(dict(provider=provider, configured=False, status="CONFIG_ERROR"))
        return result

    @router.put("/{provider}")
    async def put_connection(
        provider: ProviderName, request: Request, response: Response, db=Depends(get_session)
    ):
        response.headers["Cache-Control"] = "no-store"
        body = await request.body()
        if len(body) > LIMIT + 4096:
            raise HTTPException(413, "Cookie input is too large")
        try:
            data = await request.json()
            if not isinstance(data, dict) or not isinstance(data.get("enabled", True), bool):
                raise ValueError("Invalid official connection settings")
            runtime = await load_runtime_settings(db, get_settings())
            saved = await asyncio.to_thread(
                save_connection,
                runtime.browser_session_dir,
                provider.value,
                data.get("origin"),
                data.get("cookie_text"),
                data.get("enabled", True),
            )
        except (ValueError, TypeError, RecursionError):
            # Validation errors must not echo Cookie values back through FastAPI.
            raise HTTPException(
                422, "Cookie 格式或官网域名不正确；支持 Cookie 请求头、JSON、Netscape 格式，请检查过期时间。"
            ) from None
        except OSError:
            raise HTTPException(503, "无法写入浏览器数据卷，请检查容器目录权限") from None
        return await view(db, provider.value, saved)

    @router.post("/{provider}/test")
    async def test_connection(
        provider: ProviderName, request: Request, response: Response, db=Depends(get_session)
    ):
        response.headers["Cache-Control"] = "no-store"
        runtime = await load_runtime_settings(db, get_settings())
        try:
            saved = await asyncio.to_thread(load_connection, runtime.browser_session_dir, provider.value)
        except ValueError:
            raise HTTPException(409, "请重新保存官网会话连接") from None
        if not connection_summary(provider.value, saved)["configured"]:
            raise HTTPException(409, "请先保存尚未过期的 Cookie")
        if not runtime.provider_policy(provider.value).enabled:
            raise HTTPException(409, "请先启用该酒店集团")
        try:
            data = await request.json()
            if not isinstance(data, dict):
                raise ValueError
            hotel_id = data.get("hotel_id")
            if not isinstance(hotel_id, str) or len(hotel_id) > 36:
                raise ValueError
            check_in = date.fromisoformat(data["check_in"])
            check_out = check_in + timedelta(days=1)
            if not within_monitoring_window(check_in, check_out):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            raise HTTPException(422, "请选择该集团已收录的酒店和未来一年内的入住日期") from None
        hotel = await db.scalar(
            select(Hotel).where(
                Hotel.id == hotel_id, Hotel.provider == provider.value, Hotel.active.is_(True)
            )
        )
        if not hotel:
            raise HTTPException(422, "测试酒店不存在、已关闭或不属于所选集团")
        job = await enqueue(
            db,
            JobInput(
                provider=provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=check_in,
                check_out=check_out,
                priority=95,
                payload={
                    "adults": 2,
                    "rooms": 1,
                    "connection_test": True,
                    "official_session_revision": saved["revision"],
                },
            ),
        )
        key = "official_connection_test:" + provider.value
        record = await db.get(AppSetting, key)
        value = {"job_id": job.id, "revision": saved["revision"]}
        if record:
            record.value, record.updated_at = value, utcnow()
        else:
            db.add(AppSetting(key=key, value=value))
        await db.commit()
        return await view(db, provider.value, saved)

    return router
