import asyncio
import logging
import time
from datetime import timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, ProviderError
from app.core.logging import configure_logging, redact
from app.database.session import Session
from app.models.tables import CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.gha_catalog import hotel_candidate_url as gha_hotel_candidate
from app.providers.registry import create_provider
from app.schemas.domain import JobKind, RateRequest
from app.services.alerts import confirm_job_drops, detect_drops
from app.services.ihg_catalogs import directory_policy, persist_catalog, record_catalog_failure
from app.services.jobs import backoff, cancel_outside_window, within_monitoring_window
from app.services.provider_health import recent_healthy_scan
from app.services.queue import Queue
from app.services.rates import finalize_stay_scan, persist_rates, upsert_hotel
from app.services.runtime_settings import load_runtime_settings

log = logging.getLogger(__name__)


async def execute(job: CrawlJob, hotel: Hotel | None, settings: Settings | None = None):
    provider = create_provider(
        job.provider,
        hotel.hotel_name if hotel else None,
        hotel.official_url if hotel else None,
        settings=settings,
    )
    try:
        if job.kind == JobKind.PROVIDER_HEALTHCHECK:
            return await provider.health_check()
        if job.kind == JobKind.DISCOVER_HOTELS:
            return await provider.search_hotels(job.payload)
        if job.kind == JobKind.DISCOVER_CATALOG:
            discover = getattr(provider, "discover_catalog", None)
            if not callable(discover):
                raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Provider directory browser not implemented")
            return await discover(job.payload)
        if not hotel:
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hotel not found")
        request = RateRequest(
            provider_hotel_id=hotel.provider_hotel_id,
            check_in=job.check_in,
            check_out=job.check_out,
            adults=job.payload.get("adults", 2),
            rooms=job.payload.get("rooms", 1),
        )
        if job.kind == JobKind.FETCH_CALENDAR:
            return await provider.get_calendar_rates(request)
        if job.kind not in (JobKind.FETCH_RATE, JobKind.VERIFY_ANOMALY):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Unsupported job type")
        return await provider.search_rates(request)
    finally:
        await provider.close()


async def process_one(sessions: async_sessionmaker, queue: Queue, settings: Settings) -> bool:
    job_id = await queue.pop()
    if not job_id:
        return False
    owner = str(uuid4())
    async with sessions() as session, session.begin():
        settings = await load_runtime_settings(session, settings)
        job = await session.scalar(select(CrawlJob).where(CrawlJob.id == job_id).with_for_update())
        now = utcnow()
        if (
            not job
            or job.status not in ("PENDING", "QUEUED")
            or job.scheduled_at.replace(tzinfo=now.tzinfo) > now
        ):
            return True
        policy = settings.provider_policy(job.provider)
        if (
            job.provider == "gha"
            and job.kind == JobKind.DISCOVER_HOTELS
            and job.payload.get("official_url")
            and gha_hotel_candidate(job.payload["official_url"]) is None
        ):
            # Old cached/queued promotion links should not retry an absent
            # hotel control or consume a provider request slot. Preserve any
            # previous error/retry evidence and the original due timestamp.
            job.status = "CANCELLED"
            job.error_message = (
                (job.error_message + "; ") if job.error_message else ""
            ) + "GHA public URL is not a hotel candidate"
            job.lease_owner, job.lease_until = None, None
            return True
        if job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
            if not within_monitoring_window(job.check_in, job.check_out):
                cancel_outside_window(job)
                return True
        if not policy.enabled:
            job.status = "CANCELLED"
            job.error_message = "Provider disabled by configuration"
            return True
        state = await session.get(ProviderStatus, job.provider)
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            job.status = "PENDING"
            job.scheduled_at = state.blocked_until
            return True
        if (
            job.kind == JobKind.PROVIDER_HEALTHCHECK
            and job.payload.get("scheduled_probe") is True
            and await recent_healthy_scan(session, job.provider, now)
        ):
            # Only our automatic probe is obsolete. Explicit/manual checks
            # still run, and regular price polling is unaffected.
            job.status = "CANCELLED"
            job.error_type = None
            job.error_message = "Recent successful rate scan; redundant scheduled health probe"
            return True
        hotel = await session.get(Hotel, job.hotel_id) if job.hotel_id else None
        if (
            hotel
            and not hotel.active
            and job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)
        ):
            job.status = "CANCELLED"
            job.error_type, job.error_message = None, "Hotel disabled"
            job.lease_owner, job.lease_until = None, None
            return True
        job.status, job.lease_owner = "RUNNING", owner
        job.lease_until = now + timedelta(seconds=settings.lease_seconds)
    slot = await queue.provider_slot(job.provider, policy.concurrency, settings.lease_seconds)
    wait = await queue.rate_limit(job.provider, policy.interval_seconds) if slot else 2
    if not slot or wait:
        async with sessions() as session, session.begin():
            current = await session.get(CrawlJob, job_id)
            if current.lease_owner == owner:
                current.status = "PENDING"
                current.scheduled_at = utcnow() + timedelta(seconds=max(wait, 1))
                current.lease_owner, current.lease_until = None, None
        if slot:
            await queue.unlock(*slot)
        return True
    started = time.monotonic()
    error = None
    result = None
    try:
        result = await asyncio.wait_for(execute(job, hotel, settings), settings.job_timeout_seconds)
        if job.kind == JobKind.DISCOVER_CATALOG:
            if result.provider != job.provider:
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Catalog provider mismatch")
            _, directory_url = directory_policy(job.provider)

            if directory_url(str(result.source_url)) != directory_url(job.payload.get("official_url", "")):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Catalog URL does not match job")
        if job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY) and any(
            r.check_in != job.check_in or r.check_out != job.check_out for r in result
        ):
            raise ProviderError(ErrorCode.INVALID_RESPONSE, "Rate dates do not match job")
        if job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
            if any(
                r.provider != job.provider or r.provider_hotel_id != hotel.provider_hotel_id for r in result
            ):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Hotel identity does not match job")
            if any(
                r.adults != job.payload.get("adults", 2) or r.rooms != job.payload.get("rooms", 1)
                for r in result
            ):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Room occupancy does not match job")
        if job.kind == JobKind.PROVIDER_HEALTHCHECK and result.status != "ONLINE":
            raise ProviderError(
                ErrorCode.BLOCKED_BY_ANTIBOT if result.status == "BLOCKED" else ErrorCode.PROVIDER_CHANGED,
                "Provider health check failed",
            )
    except ProviderError as exc:
        error = exc
    except TimeoutError:
        error = ProviderError(ErrorCode.TIMEOUT, "Provider task timed out")
    except Exception:
        # Never persist raw exception URLs, credentials, headers or response content.
        error = ProviderError(ErrorCode.PARSER_ERROR, "Provider execution failed; inspect sanitized fixture")
    try:
        async with sessions() as session, session.begin():
            current = await session.scalar(select(CrawlJob).where(CrawlJob.id == job_id).with_for_update())
            if current.lease_owner != owner:
                return True
            state = await session.scalar(
                select(ProviderStatus).where(ProviderStatus.provider == job.provider).with_for_update()
            )
            if not state:
                state = ProviderStatus(
                    provider=job.provider,
                    requests_today=0,
                    success_count=0,
                    failure_count=0,
                    average_latency=0,
                )
                session.add(state)
            # The task started before this pause; Scheduler or another Worker
            # may have recorded a rejection while its browser was in flight.
            concurrent_pause = state.blocked_until
            concurrent_error = state.last_error
            if state.metrics_date != utcnow().date():
                (
                    state.metrics_date,
                    state.requests_today,
                    state.success_count,
                    state.failure_count,
                    state.average_latency,
                ) = utcnow().date(), 0, 0, 0, 0
            elapsed = time.monotonic() - started
            state.average_latency = (float(state.average_latency) * state.requests_today + elapsed) / (
                state.requests_today + 1
            )
            state.requests_today += 1
            if (
                error
                and error.code == ErrorCode.BOOKING_WINDOW_CLOSED
                and hotel is not None
                and job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)
            ):
                # A verified official "not yet bookable" response is neither a
                # transport failure nor unavailable inventory. Keep monitoring
                # this stay on the normal schedule, without poisoning the group.
                await finalize_stay_scan(
                    session,
                    hotel,
                    job.check_in,
                    job.check_out,
                    job.payload.get("adults", 2),
                    job.payload.get("rooms", 1),
                    [],
                    job.id,
                    empty_status="NOT_OPEN",
                )
                if job.kind == JobKind.VERIFY_ANOMALY:
                    await confirm_job_drops(session, job, [])
                current.status = "SUCCEEDED"
                current.error_type, current.error_message = error.code, redact(str(error))
                state.status, state.last_error, state.blocked_until = "ONLINE", None, None
                state.success_count += 1
                state.last_success = utcnow()
            elif error:
                if job.kind == JobKind.DISCOVER_CATALOG and job.provider in {"ihg", "marriott", "hyatt"}:
                    await record_catalog_failure(session, job, error, settings)
                current.retry_count += 1
                current.error_type, current.error_message = error.code, redact(str(error))
                transient = error.code in (
                    ErrorCode.TIMEOUT,
                    ErrorCode.NETWORK_ERROR,
                    ErrorCode.RATE_LIMITED,
                    ErrorCode.BROWSER_STARTUP_FAILED,
                )
                current.status = (
                    "PENDING" if transient and current.retry_count <= settings.max_retries else "FAILED"
                )
                current.scheduled_at = utcnow() + timedelta(
                    seconds=backoff(current.retry_count, error.retry_after)
                )
                state.failure_count += 1
                state.last_error = error.code.value
                state.status = "DEGRADED"
                if error.code in (ErrorCode.BLOCKED_BY_ANTIBOT, ErrorCode.RATE_LIMITED):
                    state.status = "BLOCKED"
                    state.blocked_until = utcnow() + timedelta(
                        seconds=max(
                            error.retry_after or 0,
                            21600 if error.code == ErrorCode.BLOCKED_BY_ANTIBOT else 3600,
                        )
                    )
                    current.scheduled_at = state.blocked_until
                elif error.code in (ErrorCode.NOT_IMPLEMENTED, ErrorCode.BROWSER_UNAVAILABLE):
                    state.status = "BROKEN"
            else:
                if job.kind == JobKind.DISCOVER_CATALOG:
                    await persist_catalog(session, job, result, settings)
                elif job.kind == JobKind.DISCOVER_HOTELS:
                    for data in result:
                        if data.provider != job.provider:
                            raise ValueError("discovery provider mismatch")
                        await upsert_hotel(session, data)
                elif job.kind in (JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY):
                    await persist_rates(session, hotel, result, job.id)
                    await finalize_stay_scan(
                        session,
                        hotel,
                        job.check_in,
                        job.check_out,
                        job.payload.get("adults", 2),
                        job.payload.get("rooms", 1),
                        result,
                        job.id,
                    )
                    if job.kind == JobKind.VERIFY_ANOMALY:
                        await confirm_job_drops(session, job, result)
                    else:
                        await detect_drops(session, hotel, result, settings)
                state.status = result.status if job.kind == JobKind.PROVIDER_HEALTHCHECK else "ONLINE"
                state.success_count += 1
                state.last_success, state.last_error = utcnow(), None
                if state.status == "BLOCKED":
                    state.blocked_until = utcnow() + timedelta(hours=6)
                else:
                    state.blocked_until = None
                current.status = "SUCCEEDED"
                current.error_type, current.error_message = None, None
            now = utcnow()
            if concurrent_pause and concurrent_pause.replace(tzinfo=now.tzinfo) > now:
                if not state.blocked_until or state.blocked_until.replace(
                    tzinfo=now.tzinfo
                ) < concurrent_pause.replace(tzinfo=now.tzinfo):
                    state.blocked_until = concurrent_pause
                state.status = "BLOCKED"
                if not error or error.code not in (ErrorCode.BLOCKED_BY_ANTIBOT, ErrorCode.RATE_LIMITED):
                    state.last_error = concurrent_error
                if current.status == "PENDING":
                    current.scheduled_at = max(
                        current.scheduled_at.replace(tzinfo=now.tzinfo),
                        state.blocked_until.replace(tzinfo=now.tzinfo),
                    )
            current.lease_owner, current.lease_until = None, None
            log.info(
                "job finished",
                extra={
                    "provider": job.provider,
                    "hotel_id": job.hotel_id,
                    "task": job.kind,
                    "status": current.status,
                    "latency": round(elapsed, 3),
                    "error_type": error.code if error else None,
                },
            )
    finally:
        await queue.unlock(*slot)
    return True


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    queue = Queue(redis)

    async def consume() -> None:
        while True:
            try:
                if not await process_one(Session, queue, settings):
                    await asyncio.sleep(1)
            except Exception as exc:
                log.error("worker cycle failed: %s", type(exc).__name__)
                await asyncio.sleep(3)

    async def heartbeat() -> None:
        while True:
            await redis.set("hotelbug:heartbeat:worker", utcnow().isoformat(), ex=60)
            await asyncio.sleep(15)

    try:
        await asyncio.gather(heartbeat(), *(consume() for _ in range(settings.worker_concurrency)))
    finally:
        from app.providers.browser_session import close_browser_sessions

        try:
            await close_browser_sessions()
        finally:
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
