"""One explicit upper-window audit of a globally selected hotel, never a cursor reset."""

from datetime import date, timedelta

from sqlalchemy import String, cast, func, select

from app.models.tables import AppSetting, CrawlJob, Hotel, ProviderStatus, StayScan, utcnow
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import LIVE, enqueue


async def prepare_year_boundary(sessions, settings, provider):
    assert provider in {"ihg", "gha"}, "Upper-window audit currently supports IHG/GHA only"
    async with sessions() as db, db.begin():
        marker_key = "audit:directory-only" if provider == "ihg" else "audit:sitemap-catalog"
        marker = await db.get(AppSetting, marker_key)
        assert marker and marker.value == {"provider": provider}, "Different isolated audit database"
        if not settings.provider_policy(provider).enabled:
            return {"outcome": "PROVIDER_DISABLED"}
        now, today = utcnow(), date.today()
        state = await db.get(ProviderStatus, provider)
        if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
            return {"outcome": "PROVIDER_PAUSED"}
        check_in, check_out = today + timedelta(days=364), today + timedelta(days=365)
        key = f"audit:year-boundary:{provider}:{today.isoformat()}"
        attempt = await db.get(AppSetting, key)
        if attempt:
            existing = await db.get(CrawlJob, attempt.value["job_id"])
            assert existing and existing.provider == provider and existing.kind == JobKind.FETCH_RATE
            assert (existing.check_in, existing.check_out) == (check_in, check_out)
            if existing.status in ("PENDING", "QUEUED"):
                return {"outcome": "EXISTING_BOUNDARY_JOB", "job_id": existing.id}
            return {"outcome": "BOUNDARY_ALREADY_ATTEMPTED", "job_status": existing.status}
        pending = await db.scalar(select(func.count()).select_from(CrawlJob).where(CrawlJob.status.in_(LIVE)))
        if pending >= settings.max_pending_jobs:
            return {"outcome": "QUEUE_AT_CAPACITY"}
        # Source only an existing due global frontier job. A normal global date
        # cursor must already exist; never accept a manually supplied hotel ID.
        source = await db.scalar(
            select(CrawlJob)
            .join(Hotel, Hotel.id == CrawlJob.hotel_id)
            .join(AppSetting, AppSetting.key == "global_date_cursor:" + Hotel.id)
            .where(
                CrawlJob.provider == provider,
                CrawlJob.kind == JobKind.FETCH_RATE,
                CrawlJob.status.in_(("PENDING", "QUEUED")),
                CrawlJob.scheduled_at <= now,
                CrawlJob.check_in >= today,
                CrawlJob.check_out > CrawlJob.check_in,
                CrawlJob.check_out <= check_out,
                CrawlJob.priority.in_((40, 60, 80)),
                cast(CrawlJob.payload, String) == "{}",
                Hotel.provider == provider,
                Hotel.active.is_(True),
            )
            .order_by(CrawlJob.created_at, CrawlJob.id)
            .limit(1)
        )
        if source is None:
            return {"outcome": "NO_DUE_GLOBAL_SOURCE"}
        conditions = [
            CrawlJob.hotel_id == source.hotel_id,
            CrawlJob.check_in == check_in,
            CrawlJob.check_out == check_out,
            CrawlJob.kind.in_((JobKind.FETCH_RATE, JobKind.FETCH_CALENDAR, JobKind.VERIFY_ANOMALY)),
            func.coalesce(CrawlJob.payload["adults"].as_integer(), 2) == 2,
            func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1) == 1,
        ]
        previous = await db.scalar(select(CrawlJob.id).where(*conditions).limit(1))
        scan = await db.scalar(
            select(StayScan.id)
            .where(
                StayScan.hotel_id == source.hotel_id,
                StayScan.check_in == check_in,
                StayScan.check_out == check_out,
                StayScan.adults == 2,
                StayScan.rooms == 1,
            )
            .limit(1)
        )
        if previous or scan:
            return {"outcome": "BOUNDARY_CONDITION_ALREADY_RECORDED"}
        job = await enqueue(
            db,
            JobInput(
                provider=provider,
                kind=JobKind.FETCH_RATE,
                hotel_id=source.hotel_id,
                check_in=check_in,
                check_out=check_out,
                priority=40,
                payload={"adults": 2, "rooms": 1, "audit_year_boundary": True},
            ),
        )
        db.add(AppSetting(key=key, value={"job_id": job.id, "source_job_id": source.id}))
        return {"outcome": "CREATED_BOUNDARY_JOB", "job_id": job.id}
