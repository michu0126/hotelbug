import asyncio
import logging
import signal
import time
from datetime import date, timedelta
from uuid import uuid4

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, ProviderError
from app.core.logging import configure_logging, redact
from app.database.session import Session
from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.gha_catalog import hotel_candidate_url as gha_hotel_candidate
from app.providers.marriott_cn_catalog import ROOT as MARRIOTT_CN_ROOT
from app.providers.registry import create_provider
from app.schemas.calendar_hints import CalendarHintBatch, CalendarHintRequest, month_window
from app.schemas.domain import JobKind, RateRequest
from app.services.alerts import confirm_job_drops, detect_drops
from app.services.calendar_hints import hint_month_current, match_calendar_candidates, persist_calendar_hints
from app.services.catalog_price_source import (
    SOURCE_MARKER,
    automatic_price_source_changed,
    load_catalog_price_source,
)
from app.services.hotel_eligibility import EXCLUDED_REASON, automatic_job_excluded, is_test_hotel
from app.services.ihg_catalogs import (
    directory_policy,
    obsolete_directory_job,
    persist_catalog,
    record_catalog_failure,
)
from app.services.jobs import backoff, cancel_outside_window, within_monitoring_window
from app.services.official_connections import load_connection
from app.services.provider_health import scheduled_probe_cancellation
from app.services.queue import Queue
from app.services.rates import finalize_stay_scan, persist_rates, upsert_hotel
from app.services.runtime_settings import load_runtime_settings

log = logging.getLogger(__name__)


async def execute(
    job: CrawlJob,
    hotel: Hotel | None,
    settings: Settings | None = None,
    *,
    request_window: tuple[date, date] | None = None,
):
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
        if job.kind == JobKind.FETCH_CALENDAR_HINTS:
            window = request_window if request_window is not None else month_window(job.payload.get("month"))
            method = getattr(provider, "get_calendar_hints", None)
            if window is None or not callable(method):
                raise ProviderError(ErrorCode.NOT_IMPLEMENTED, "Public month hints unavailable for this task")
            return await method(
                CalendarHintRequest(
                    provider_hotel_id=hotel.provider_hotel_id,
                    start_date=window[0],
                    end_date=window[1],
                )
            )
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


async def current_calendar_hint_hotel(session, job, result):
    """Revalidate the claimed target before treating its response as success."""
    # Keep source and hotel records stable through persistence. An in-flight
    # admin change must not manufacture a fresh successful monthly observation.
    await session.scalar(
        select(AppSetting)
        .where(AppSetting.key == "catalog:marriott")
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    source = await load_catalog_price_source(session)
    hotel = await session.scalar(
        select(Hotel)
        .where(Hotel.id == job.hotel_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        source.root != MARRIOTT_CN_ROOT
        or hotel is None
        or not hotel.active
        or is_test_hotel(hotel)
        or hotel.provider != result.provider
        or hotel.provider_hotel_id != result.provider_hotel_id
        or not hotel.official_url.startswith("https://www.marriott.com.cn/")
        or job.payload.get(SOURCE_MARKER) not in (None, MARRIOTT_CN_ROOT)
        or await automatic_price_source_changed(session, job, hotel)
    ):
        return None
    return hotel


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
        if job.kind == JobKind.FETCH_CALENDAR_HINTS and not hint_month_current(job):
            cancel_outside_window(job)
            return True
        if not policy.enabled:
            job.status = "CANCELLED"
            job.error_message = "Provider disabled by configuration"
            return True
        if await obsolete_directory_job(session, job):
            job.status = "CANCELLED"
            job.error_message = (
                (job.error_message + "; ") if job.error_message else ""
            ) + "Official directory source changed"
            job.lease_owner, job.lease_until = None, None
            return True
        if job.payload.get("connection_test") is True:
            try:
                connection = await asyncio.to_thread(
                    load_connection, settings.browser_session_dir, job.provider
                )
            except ValueError:
                connection = None
            if (
                not connection
                or not connection.get("enabled")
                or connection.get("revision") != job.payload.get("official_session_revision")
            ):
                job.status = "CANCELLED"
                job.error_message = "Official connection changed; submit a new quote test"
                job.lease_owner, job.lease_until = None, None
                return True
        hotel = await session.get(Hotel, job.hotel_id) if job.hotel_id else None
        if automatic_job_excluded(job, hotel):
            job.status = "CANCELLED"
            job.error_message = ((job.error_message + "; ") if job.error_message else "") + EXCLUDED_REASON
            job.lease_owner, job.lease_until = None, None
            return True
        if await automatic_price_source_changed(session, job, hotel):
            # Retire only newly marked automatic work. Existing unmarked jobs,
            # explicit watches and manual checks retain their original meaning.
            job.status = "CANCELLED"
            job.error_message = (
                (job.error_message + "; ") if job.error_message else ""
            ) + "Automatic price task directory source changed"
            job.lease_owner, job.lease_until = None, None
            return True
        state = await session.get(ProviderStatus, job.provider)
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            job.status = "PENDING"
            job.scheduled_at = state.blocked_until
            return True
        if job.kind == JobKind.PROVIDER_HEALTHCHECK and job.payload.get("scheduled_probe") is True:
            # Only our automatic probe is obsolete. Explicit/manual checks
            # still run, and regular price polling is unaffected.
            reason = await scheduled_probe_cancellation(session, job, now)
            if reason:
                job.status = "CANCELLED"
                job.error_type = None
                job.error_message = reason
                return True
        if (
            hotel
            and not hotel.active
            and job.kind
            in (
                JobKind.FETCH_RATE,
                JobKind.FETCH_CALENDAR,
                JobKind.VERIFY_ANOMALY,
                JobKind.FETCH_CALENDAR_HINTS,
            )
        ):
            job.status = "CANCELLED"
            job.error_type, job.error_message = None, "Hotel disabled"
            job.lease_owner, job.lease_until = None, None
            return True
        job.status, job.lease_owner = "RUNNING", owner
        job.lease_until = now + timedelta(seconds=settings.lease_seconds)
    return await run_claimed_job(sessions, queue, settings, job, hotel, owner, policy)


async def run_claimed_job(sessions, queue, settings, job, hotel, owner, policy):
    """Return interrupted work to SQL without treating shutdown as a source failure."""
    slot = None
    try:
        slot = await queue.provider_slot(job.provider, policy.concurrency, settings.lease_seconds)
        return await finish_claimed_job(sessions, queue, settings, job, hotel, owner, policy, slot)
    except asyncio.CancelledError:
        async with sessions() as session, session.begin():
            current = await session.scalar(select(CrawlJob).where(CrawlJob.id == job.id).with_for_update())
            # A completed job or a lease reclaimed by another Worker is not ours.
            if current and current.status == "RUNNING" and current.lease_owner == owner:
                current.status = "PENDING"
                current.lease_owner, current.lease_until = None, None
        raise
    finally:
        if slot:
            await queue.unlock(*slot)


async def finish_claimed_job(sessions, queue, settings, job, hotel, owner, policy, slot):
    job_id = job.id
    wait = await queue.rate_limit(job.provider, policy.interval_seconds) if slot else 2
    if not slot or wait:
        async with sessions() as session, session.begin():
            current = await session.get(CrawlJob, job_id)
            if current.lease_owner == owner:
                current.status = "PENDING"
                current.scheduled_at = utcnow() + timedelta(seconds=max(wait, 1))
                current.lease_owner, current.lease_until = None, None
        return True
    started = time.monotonic()
    error = None
    result = None
    try:
        if job.kind == JobKind.FETCH_CALENDAR_HINTS:
            # Bind validation to the exact requested dates, not a fresh window
            # after the browser returns. Both boundaries can move at midnight;
            # persistence separately clips any dates that have become past.
            window = month_window(job.payload.get("month"))
            if window is None:
                raise ProviderError(
                    ErrorCode.INVALID_RESPONSE, "Calendar hint month outside monitoring window"
                )
            execution = execute(job, hotel, settings, request_window=window)
        else:
            execution = execute(job, hotel, settings)
        result = await asyncio.wait_for(execution, settings.job_timeout_seconds)
        if job.kind == JobKind.FETCH_CALENDAR_HINTS:
            if (
                not isinstance(result, CalendarHintBatch)
                or hotel is None
                or result.provider != job.provider
                or result.provider_hotel_id != hotel.provider_hotel_id
                or result.start_date.strftime("%Y-%m") != job.payload["month"]
                or result.end_date != window[1]
                or result.start_date != window[0]
            ):
                raise ProviderError(ErrorCode.INVALID_RESPONSE, "Calendar hint scope does not match job")
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
    async with sessions() as session, session.begin():
        current = await session.scalar(select(CrawlJob).where(CrawlJob.id == job_id).with_for_update())
        if current.lease_owner != owner:
            return True
        if await obsolete_directory_job(session, current):
            # A source switch while this request was in flight must not let an
            # obsolete origin's response overwrite active-source group health.
            current.status = "CANCELLED"
            if error:
                await record_catalog_failure(session, current, error, settings)
                current.error_type = error.code.value
            current.error_message = "Official directory source changed during request"
            current.lease_owner, current.lease_until = None, None
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
        cancelled_hint = False
        if not error and job.kind == JobKind.FETCH_CALENDAR_HINTS:
            hotel = await current_calendar_hint_hotel(session, current, result)
            if hotel is None:
                current.status = "CANCELLED"
                current.error_message = (
                    (current.error_message + "; ") if current.error_message else ""
                ) + "Calendar hint target changed during request"
                cancelled_hint = True
        if cancelled_hint:
            pass  # Preserve prior error/retry and group health, including any concurrent pause.
        elif (
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
            if job.kind == JobKind.FETCH_CALENDAR_HINTS:
                await persist_calendar_hints(session, hotel, job, result, settings)
            elif job.kind == JobKind.DISCOVER_CATALOG:
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
                elif job.payload.get("connection_test") is not True:
                    await detect_drops(session, hotel, result, settings)
                if job.payload.get("connection_test") is not True:
                    await match_calendar_candidates(session, hotel, job, result)
            state.status = (
                (state.status if state.status == "ONLINE" else "DEGRADED")
                if job.kind == JobKind.FETCH_CALENDAR_HINTS
                else (result.status if job.kind == JobKind.PROVIDER_HEALTHCHECK else "ONLINE")
            )
            state.success_count += 1
            if job.kind != JobKind.FETCH_CALENDAR_HINTS:
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
    return True


async def run_worker(settings, redis, sessions=Session) -> None:
    queue = Queue(redis)

    async def consume() -> None:
        while True:
            try:
                if not await process_one(sessions, queue, settings):
                    await asyncio.sleep(1)
            except Exception as exc:
                log.error("worker cycle failed: %s", type(exc).__name__)
                await asyncio.sleep(3)

    async def heartbeat() -> None:
        while True:
            await redis.set("hotelbug:heartbeat:worker", utcnow().isoformat(), ex=60)
            await asyncio.sleep(15)

    async def browser_maintenance() -> None:
        from app.providers.browser_session import close_idle_browser_sessions

        while True:
            await asyncio.sleep(15)
            try:
                await close_idle_browser_sessions()
            except Exception as exc:
                log.warning("idle browser cleanup failed: %s", type(exc).__name__)

    tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(browser_maintenance())]
    tasks.extend(asyncio.create_task(consume()) for _ in range(settings.worker_concurrency))
    try:
        await asyncio.gather(*tasks)
    finally:
        from app.providers.browser_session import close_browser_sessions

        # Stop every producer/user before closing their shared browsers and Redis.
        # gather alone leaves siblings running when one task fails.
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await close_browser_sessions()
        finally:
            await redis.aclose()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(run_worker(settings, redis))
    stopping = False

    def request_stop(*_):
        nonlocal stopping
        if not stopping:
            stopping = True
            # Repeated SIGTERM must not cancel browser/profile cleanup halfway.
            loop.call_soon_threadsafe(task.cancel)

    previous = {sig: signal.signal(sig, request_stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        await task
    except asyncio.CancelledError:
        if not stopping:
            raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    asyncio.run(main())
