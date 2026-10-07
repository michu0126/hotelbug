"""Execute one existing automatically generated global price job; no TG delivery.

Requires the persistent directory audit marker. Never selects a hotel manually,
resets a retry deadline, or re-runs a succeeded job. By default, also skips new
periodic monitoring jobs for previously observed stays. Explicit --allow-due-recheck
permits those existing due jobs without changing the production scheduler.
Explicit --year-boundary creates one separately tagged day-364 condition from
an existing global frontier job, preserving all normal production cursors.
"""

import argparse
import asyncio
import json
import re
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fakeredis.aioredis import FakeRedis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import AppSetting, CrawlJob, Hotel, PriceHistory, StayScan, utcnow
from app.providers.browser_session import close_browser_sessions
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.jobs import enqueue, within_monitoring_window
from app.services.queue import Queue


def install_stay_diagnostics(provider):
    """Read only known public stay controls in the same project-owned page."""
    assert provider in {"ihg", "accor", "hilton"}, "Unsupported public stay diagnostic"
    factory = FACTORIES[provider]

    def diagnostic_factory():
        instance = factory()
        original = instance.search_rates

        async def read_rates(request):
            try:
                return await original(request)
            finally:
                page = instance._page
                if page and not page.is_closed():
                    parsed = urlparse(page.url)
                    if provider == "ihg" and parsed.hostname == "www.ihg.com":
                        await emit_public_stay_diagnostics(page, request.provider_hotel_id)
                    elif (
                        provider == "accor"
                        and parsed.hostname == "all.accor.com"
                        and parsed.path.startswith("/booking/en/accor/hotel/")
                    ):
                        await emit_accor_offer_diagnostics(page)
                if provider == "hilton":
                    pages = [*instance._journey_pages, page]
                    seen = set()
                    for journey in pages:
                        if journey and id(journey) not in seen and not journey.is_closed():
                            seen.add(id(journey))
                            if urlparse(journey.url).hostname == "www.hilton.com":
                                await emit_hilton_stay_diagnostics(journey, request, instance.hotel_name)
                            else:
                                print(
                                    json.dumps(
                                        {
                                            "public_hilton_page_state": "UNCOMMITTED_BLANK"
                                            if journey.url == "about:blank"
                                            else "OTHER_ORIGIN",
                                            "booking_stage": instance._booking_stage
                                            if isinstance(instance._booking_stage, str)
                                            else None,
                                        }
                                    ),
                                    flush=True,
                                )

        instance.search_rates = read_rates
        return instance

    FACTORIES[provider] = diagnostic_factory


async def emit_hilton_stay_diagnostics(page, request, hotel_name):
    """Known public search/stay controls on existing project pages only."""
    try:
        code = request.provider_hotel_id.upper()
        assert re.fullmatch(r"[A-Z0-9]{7}", code), "Invalid diagnostic hotel code"
        headings = await page.locator("h1,h2,[role='alert'],[role='option']").evaluate_all(
            """es => es.filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .slice(0, 30).map(e => ({tag: e.tagName, role: e.getAttribute('role'), text: (e.innerText || '').slice(0, 500)}))"""
        )
        location = await page.locator("#location-input").evaluate_all(
            """(es, expected) => es.map(e => ({value: e.value === expected ? expected : 'other',
                visible: !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'}))""",
            hotel_name,
        )
        controls = await page.locator(
            f'[data-testid="search-edit-button"],[data-testid="hotelName"],[data-testid="hotel-card-{code}"],'
            '[data-testid="search-submit-button"]'
        ).evaluate_all(
            """es => es.map(e => ({testid: e.getAttribute('data-testid'),
                text: (e.innerText || '').slice(0, 1500), aria_label: e.getAttribute('aria-label'),
                visible: !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'}))"""
        )
        print(
            json.dumps(
                {
                    "public_hilton_stay_controls": {
                        "page_path": urlparse(page.url).path,
                        "headings": headings,
                        "location": location,
                        "controls": controls,
                    }
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
    except Exception:
        print(json.dumps({"public_hilton_stay_controls_unavailable": True}), flush=True)


async def emit_accor_offer_diagnostics(page):
    """Project rendered public offer text; do not navigate or inspect private state."""
    try:
        offers = await page.locator("label.hotel-accommodation-offers-content__label").evaluate_all(
            """es => es.filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .filter(e => [...e.querySelectorAll('.offer-price')].some(p => /^Public rate/.test(p.innerText.trim())))
                .map(e => ({
                    plan: e.querySelector('.ads-radio__label')?.innerText || null,
                    stay_details: e.querySelector('.stay-details')?.innerText || null,
                    public_prices: [...e.querySelectorAll('.offer-price')]
                        .filter(p => /^Public rate/.test(p.innerText.trim())).map(p => p.innerText),
                    displayed_offer_text: e.innerText.slice(0, 2000)
                }))"""
        )
        print(json.dumps({"public_accor_offer_controls": offers}, ensure_ascii=True), flush=True)
    except Exception:
        print(json.dumps({"public_accor_offer_controls_unavailable": True}), flush=True)


async def emit_public_stay_diagnostics(page, hotel_code=None):
    try:
        # These exact controls display public dates/occupancy;
        # don't collect arbitrary inputs, cookies or private state.
        dates = await page.locator("input.calendar-input").evaluate_all(
            """es => es.map(e => ({value: /^\\d{1,2}\\/\\d{1,2}\\/\\d{4}$/.test(e.value) ? e.value : 'other', visible: !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'}))"""
        )
        guests = await page.locator("#room-and-guest").evaluate_all(
            """es => es.map(e => ({value: /^\\d+ Room[s]?, \\d+ Guest[s]?$/.test(e.value) ? e.value : 'other', visible: !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden'}))"""
        )
        rooms = await page.locator('app-room-rate-item[id^="ROOM_CODE"]').evaluate_all(
            """es => es.map(e => ({
                            id: /^ROOM_CODE[A-Z0-9]+$/.test(e.id) ? e.id : 'other',
                            room_type: e.querySelector('[data-testid="roomNameTestId"]')?.innerText || null,
                            public_fields: [...e.querySelectorAll('[data-testid],[data-slnm-ihg],input[role="switch"]')].map(n => ({tag: n.tagName, testid: n.getAttribute('data-testid'), identity: n.getAttribute('data-slnm-ihg'), role: n.getAttribute('role'), checked: n.getAttribute('aria-checked'), class: n.className, text: (n.innerText || '').slice(0,300)})),
                            buttons: [...e.querySelectorAll('button,[role="button"]')].map(b => ({text: b.innerText, aria_label: b.getAttribute('aria-label'), title: b.getAttribute('title'), visible: !!b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden', disabled: !!b.disabled || b.getAttribute('aria-disabled') === 'true'})),
                            plan_buttons: [...e.querySelectorAll('button')].filter(b => /^View prices for /.test(b.innerText)).map(b => ({text: b.innerText, visible: !!b.getClientRects().length && getComputedStyle(b).visibility !== 'hidden', disabled: b.disabled || b.getAttribute('aria-disabled') === 'true'}))
                        }))"""
        )
        messages = await page.locator("h1,h2,h3,[role='alert'],[role='status']").evaluate_all(
            """es => es.filter(e => !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .slice(0, 30).map(e => ({tag: e.tagName, role: e.getAttribute('role'),
                    text: (e.innerText || '').slice(0, 600)}))"""
        )
        result_messages = await page.locator("body").evaluate_all(
            """es => es.flatMap(e => (e.innerText || '').split('\\n'))
                .map(t => t.trim()).filter(t => /no rooms|not available|no availability|sorry|unable|error|sold out|session|try again/i.test(t))
                .slice(0, 20).map(t => t.slice(0, 600))"""
        )
        hotel_cards = await page.locator('app-hotel-card-list-view[data-testid="hotel-card"]').evaluate_all(
            """(es, code) => es.filter(e => (!code || e.id === code) && !!e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden')
                .slice(0, 5).map(e => ({id: e.id, heading: e.querySelector('[data-testid="brandHotelNameSID"]')?.innerText,
                            text: (e.innerText || '').slice(0, 2000),
                            links: [...e.querySelectorAll('a[href]')].slice(0, 15).map(a => {
                                const u = new URL(a.href);
                                return {text: a.innerText.slice(0, 100), host: u.hostname, path: u.pathname,
                                    has_query: !!u.search, has_fragment: !!u.hash};
                            }), public_nodes: [...e.querySelectorAll('[data-testid],[data-slnm-ihg]')]
                                .slice(0, 30).map(c => ({tag: c.tagName, testid: c.getAttribute('data-testid'),
                                    identity: c.getAttribute('data-slnm-ihg'), text: (c.innerText || '').slice(0, 600)}))}))""",
            hotel_code.upper() if hotel_code and re.fullmatch(r"[A-Za-z0-9]{5}", hotel_code) else None,
        )
        parsed = urlparse(page.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        public_patterns = {
            "qSlH": r"[A-Za-z0-9]{5}",
            "qRms": r"[0-9]{1,2}",
            "qAdlt": r"[0-9]{1,2}",
            "qChld": r"[0-9]{1,2}",
            "qCiD": r"[0-9]{1,2}",
            "qCiMy": r"[0-9]{6}",
            "qCoD": r"[0-9]{1,2}",
            "qCoMy": r"[0-9]{6}",
            "qRtP": r"[A-Za-z0-9]{6}",
        }
        public_query = {
            key: values[0] if len(values) == 1 and re.fullmatch(pattern, values[0]) else "other"
            for key, pattern in public_patterns.items()
            if (values := query.get(key)) is not None
        }
        print(
            json.dumps(
                {
                    "public_ihg_stay_controls": {
                        "dates": dates,
                        "guests": guests,
                        "rooms": rooms,
                        "messages": messages,
                        "result_messages": result_messages,
                        "hotel_cards": hotel_cards,
                        "page_path": parsed.path,
                        "public_stay_query": public_query,
                    }
                },
                ensure_ascii=True,
            ),
            flush=True,
        )

    except Exception:
        # Optional audit output must never replace the real result/error, e.g.
        # when the same browser disconnects during diagnostic inspection.
        print(json.dumps({"public_ihg_stay_controls_unavailable": True}), flush=True)


async def run_existing_price(
    sessions, queue, settings, emit=print, provider="ihg", target_job_id=None, allow_due_recheck=False
):
    assert provider in {"ihg", "accor", "gha", "hilton"}, "Unsupported audit provider"
    marker_key = "audit:directory-only" if provider == "ihg" else "audit:sitemap-catalog"
    async with sessions() as db:
        marker = await db.get(AppSetting, marker_key)
        assert marker and marker.value == {"provider": provider}, (
            f"Not the isolated {provider.upper()} audit database"
        )
        observed = (
            select(StayScan.id)
            .where(
                StayScan.hotel_id == CrawlJob.hotel_id,
                StayScan.check_in == CrawlJob.check_in,
                StayScan.check_out == CrawlJob.check_out,
                StayScan.adults == func.coalesce(CrawlJob.payload["adults"].as_integer(), 2),
                StayScan.rooms == func.coalesce(CrawlJob.payload["rooms"].as_integer(), 1),
            )
            .correlate(CrawlJob)
            .exists()
        )
        job = await db.scalar(
            select(CrawlJob)
            .where(
                CrawlJob.provider == provider,
                CrawlJob.kind == JobKind.FETCH_RATE,
                CrawlJob.status.in_(("PENDING", "QUEUED")),
                CrawlJob.scheduled_at <= utcnow(),
                *([CrawlJob.id == target_job_id] if target_job_id else []),
                *(
                    []
                    if target_job_id
                    else [
                        CrawlJob.check_in >= date.today(),
                        CrawlJob.check_out > CrawlJob.check_in,
                        CrawlJob.check_out <= date.today() + timedelta(days=365),
                    ]
                ),
                *([] if allow_due_recheck else [~observed]),
            )
            .order_by(CrawlJob.created_at, CrawlJob.id)
            .limit(1)
        )
        if job is None:
            return {"outcome": "NO_PENDING_PRICE_DUE"}
        job_id, priority = job.id, job.priority
        hotel = await db.get(Hotel, job.hotel_id)
        emit(
            json.dumps(
                {
                    "starting_price_job": job_id,
                    "provider": provider,
                    "hotel": hotel.hotel_name,
                    "hotel_code": hotel.provider_hotel_id,
                    "country": hotel.country,
                    "check_in": job.check_in.isoformat(),
                    "check_out": job.check_out.isoformat(),
                },
                ensure_ascii=True,
            )
        )
    await queue.put(job_id, priority)
    await process_one(sessions, queue, settings)
    async with sessions() as db:
        current = await db.get(CrawlJob, job_id)
        history_count = await db.scalar(
            select(func.count()).select_from(PriceHistory).where(PriceHistory.job_id == job_id)
        )
        scan = await db.scalar(select(StayScan).where(StayScan.job_id == job_id))
        offers = (await db.scalars(select(PriceHistory).where(PriceHistory.job_id == job_id).limit(5))).all()
        report = {
            "provider": provider,
            "outcome": current.status,
            "job_id": job_id,
            "error": current.error_type,
            "message": current.error_message,
            "history_rows_persisted": history_count,
            "stay_scan_status": scan.status if scan else None,
            "offers": [
                {
                    "room_type": offer.room_type,
                    "rate_name": offer.rate_name,
                    "currency": offer.currency,
                    "cash_price": str(offer.cash_price) if offer.cash_price is not None else None,
                    "total_price": str(offer.total_price) if offer.total_price is not None else None,
                }
                for offer in offers[:5]
            ],
            "telegram_delivery_called": False,
        }
        # Hotel/room names can contain U+2060 or other text outside Windows GBK.
        # ASCII JSON preserves those characters without failing after the DB commit.
        emit(json.dumps(report, ensure_ascii=True))
        return report


async def requeue_fixed_identity_job(sessions, job_id):
    """Explicit code-fix retest only; keep the original failure as evidence."""
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, "audit:directory-only")
        assert marker and marker.value == {"provider": "ihg"}, "Not the isolated IHG audit database"
        original = await db.get(CrawlJob, job_id)
        assert original and original.provider == "ihg" and original.kind == JobKind.FETCH_RATE
        assert original.status == "FAILED" and original.error_type == "INVALID_RESPONSE"
        assert (original.error_message or "").startswith("IHG hotel, dates or occupancy mismatch")
        succeeded = await db.scalar(
            select(CrawlJob.id)
            .where(
                CrawlJob.dedupe_key == original.dedupe_key,
                CrawlJob.status == "SUCCEEDED",
                CrawlJob.created_at >= original.created_at,
            )
            .limit(1)
        )
        assert succeeded is None, "This failed stay has already succeeded after the original failure"
        new = await enqueue(
            db,
            JobInput(
                provider=original.provider,
                kind=original.kind,
                hotel_id=original.hotel_id,
                check_in=original.check_in,
                check_out=original.check_out,
                priority=original.priority,
                payload=original.payload,
            ),
        )
        return new.id


async def requeue_fixed_room_job(sessions, provider, job_id, *, no_room=False, link_identity=False):
    """One explicit recorded room-flow fix retest; never shorten a deadline."""
    expected = {
        "ihg": ("TIMEOUT", "IHG booking page failed at expand room plans"),
        "gha": ("PROVIDER_CHANGED", "GHA room identity or inclusive summary price missing"),
    }
    assert not (no_room and link_identity), "Choose one recorded flow fix"
    if link_identity:
        assert provider == "ihg", "No-room link fix is IHG-only"
        expected = {"ihg": ("INVALID_RESPONSE", "IHG unavailable hotel official link mismatch")}
    elif no_room:
        assert provider == "ihg", "No-room redirect fix is IHG-only"
        expected = {"ihg": ("TIMEOUT", "IHG booking page failed at room results")}
    assert provider in expected, "Unsupported recorded room-flow fix"
    marker_key = "audit:directory-only" if provider == "ihg" else "audit:sitemap-catalog"
    async with sessions() as db, db.begin():
        marker = await db.get(AppSetting, marker_key)
        assert marker and marker.value == {"provider": provider}, "Different isolated audit provider"
        original = await db.get(CrawlJob, job_id)
        assert original and original.provider == provider and original.kind == JobKind.FETCH_RATE
        assert (
            original.status == "FAILED"
            and (original.error_type, original.error_message) == expected[provider]
        )
        assert within_monitoring_window(original.check_in, original.check_out), (
            "Recorded stay is outside current 365-day window"
        )
        prefix = (
            "audit:no-room-link-fix-retest:"
            if link_identity
            else "audit:no-room-fix-retest:"
            if no_room
            else "audit:room-fix-retest:"
        )
        attempt_key = prefix + job_id
        previous = await db.get(AppSetting, attempt_key)
        if previous:
            existing = await db.get(CrawlJob, previous.value["job_id"])
            assert existing.status in ("PENDING", "QUEUED", "RUNNING"), "Recorded fix retest already finished"
            return existing.id
        succeeded = await db.scalar(
            select(CrawlJob.id)
            .where(
                CrawlJob.dedupe_key == original.dedupe_key,
                CrawlJob.status == "SUCCEEDED",
                CrawlJob.created_at >= original.created_at,
            )
            .limit(1)
        )
        assert succeeded is None, "This failed stay has already succeeded"
        active = await db.scalar(
            select(CrawlJob)
            .where(
                CrawlJob.dedupe_key == original.dedupe_key,
                CrawlJob.status.in_(("PENDING", "QUEUED", "RUNNING")),
            )
            .limit(1)
        )
        new = active or await enqueue(
            db,
            JobInput(
                provider=provider,
                kind=original.kind,
                hotel_id=original.hotel_id,
                check_in=original.check_in,
                check_out=original.check_out,
                priority=original.priority,
                payload=original.payload,
            ),
        )
        now = utcnow()
        if active is None:
            new.scheduled_at = max(now, original.scheduled_at.replace(tzinfo=now.tzinfo))
        db.add(AppSetting(key=attempt_key, value={"job_id": new.id}))
        return new.id


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("ihg", "accor", "gha", "hilton"), default="ihg")
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--proxy")
    parser.add_argument(
        "--year-boundary",
        action="store_true",
        help="IHG/GHA: one new explicit day-364 audit from an existing global job; no cursor reset",
    )
    parser.add_argument(
        "--existing-job-id", help="Select only this existing due job; never reset or create a task"
    )
    parser.add_argument(
        "--allow-due-recheck",
        action="store_true",
        help="Explicitly permit an existing due periodic monitoring job; default tests only unobserved stays",
    )
    parser.add_argument(
        "--stay-diagnostics",
        action="store_true",
        help="IHG/Hilton public stay controls or Accor public offers on existing pages, no extra request",
    )
    parser.add_argument(
        "--navigation-diagnostics",
        action="store_true",
        help="Hilton only: existing project main-document status/transport events, no XHR/headers/body",
    )
    parser.add_argument(
        "--retry-fixed-identity-job",
        help="Explicit failed job ID for a code-fix retest, never an access rejection",
    )
    parser.add_argument(
        "--retry-fixed-room-job",
        help="Recorded IHG inline/GHA member-summary code fix only; keep original deadline/failure",
    )
    parser.add_argument(
        "--retry-fixed-no-room-job", help="One recorded IHG room-results timeout code-fix retest only"
    )
    parser.add_argument(
        "--retry-fixed-no-room-link-job", help="One recorded IHG unavailable-card link identity fix retest"
    )
    args = parser.parse_args()
    if args.navigation_diagnostics:
        assert args.provider == "hilton", "Navigation diagnostic is Hilton-only"
        from hilton_navigation_diagnostics import install_navigation_diagnostics

        install_navigation_diagnostics()
    if args.stay_diagnostics:
        install_stay_diagnostics(args.provider)
    database = args.state_dir.resolve() / "catalog-audit.sqlite"
    assert database.is_file(), "Missing persistent directory audit database"
    settings = Settings(
        _env_file=None,
        browser_channel="chrome",
        browser_proxy_url=args.proxy or "",
        provider_overrides={name: ProviderPolicy(enabled=name == args.provider) for name in FACTORIES},
        telegram_bot_token="",
        telegram_chat_id="",
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + str(database))
    redis = FakeRedis(decode_responses=True)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        fix_jobs = [
            args.retry_fixed_identity_job,
            args.retry_fixed_room_job,
            args.retry_fixed_no_room_job,
            args.retry_fixed_no_room_link_job,
        ]
        assert sum(bool(job_id) for job_id in fix_jobs) <= 1, "Choose one fix retest"
        assert not (args.existing_job_id and any(fix_jobs)), (
            "Existing due job selection cannot be combined with a code-fix retest"
        )
        target_id = args.existing_job_id
        if args.year_boundary:
            assert not (target_id or any(fix_jobs) or args.allow_due_recheck), (
                "Boundary audit cannot be combined with explicit selection/retest/recheck"
            )
            from year_boundary_audit import prepare_year_boundary

            prepared = await prepare_year_boundary(sessions, settings, args.provider)
            print(json.dumps(prepared, ensure_ascii=True), flush=True)
            target_id = prepared.get("job_id")
            if not target_id:
                raise SystemExit(2)
        if args.retry_fixed_identity_job:
            assert args.provider == "ihg", "Fixed identity retest is IHG-only"
            target_id = await requeue_fixed_identity_job(sessions, args.retry_fixed_identity_job)
        if args.retry_fixed_room_job:
            target_id = await requeue_fixed_room_job(sessions, args.provider, args.retry_fixed_room_job)
        if args.retry_fixed_no_room_job:
            target_id = await requeue_fixed_room_job(
                sessions, args.provider, args.retry_fixed_no_room_job, no_room=True
            )
        if args.retry_fixed_no_room_link_job:
            target_id = await requeue_fixed_room_job(
                sessions, args.provider, args.retry_fixed_no_room_link_job, link_identity=True
            )
        report = await run_existing_price(
            sessions,
            Queue(redis),
            settings,
            provider=args.provider,
            target_job_id=target_id,
            allow_due_recheck=args.allow_due_recheck,
        )
        if report["outcome"] == "NO_PENDING_PRICE_DUE":
            print(json.dumps(report, ensure_ascii=True), flush=True)
        if report["outcome"] != "SUCCEEDED":
            raise SystemExit(2)
    finally:
        try:
            await close_browser_sessions()
        finally:
            await redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
