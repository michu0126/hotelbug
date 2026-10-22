"""Local six-group worldwide monitoring using the production catalog and price pipeline.

SQLite stores both observations and a bounded durable queue. This is not a second
implementation of hotel selectors, rate interpretation, or anomaly detection.
"""

import argparse
import asyncio
import contextlib
import json
import math
import random
import sqlite3
import tomllib
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.crawler.worker import process_one
from app.models.tables import AppSetting, Base, CrawlJob, Hotel, ProviderStatus, utcnow
from app.providers.marriott_cn_catalog import ROOT as MARRIOTT_ROOT
from app.providers.registry import FACTORIES, provider_settings
from app.schemas.domain import HotelData
from app.services.alerts import reconcile_terminal_verifications
from app.services.catalogs import discover_accor, discover_gha, discover_hilton
from app.services.ihg_catalogs import discover_hyatt, discover_marriott
from app.services.ihg_sitemaps import discover_ihg_global
from app.services.jobs import due_dispatch_jobs, expire_pending_stays, reserve_provider_turn
from app.services.rates import upsert_hotel
from app.services.runtime_settings import RuntimeHttpClients
from app.services.watchlists import expand_global

BRANDS = ("marriott", "ihg", "hilton", "hyatt", "gha", "accor")
DISCOVER = dict(
    zip(
        BRANDS,
        (
            discover_marriott,
            discover_ihg_global,
            discover_hilton,
            discover_hyatt,
            discover_gha,
            discover_accor,
        ),
    )
)
DOMAINS = {
    "marriott": ("marriott.com", "marriott.com.cn"),
    "ihg": ("ihg.com", "ihg.com.cn"),
    "hilton": ("hilton.com", "hilton.com.cn"),
    "hyatt": ("hyatt.com",),
    "gha": ("ghadiscovery.com",),
    "accor": ("accor.com",),
}
CONTROL = "local_global:control"
PAUSES = "local_global:pauses"
CATALOG_KEYS = {"catalog:" + brand for brand in BRANDS} | {"catalog:ihg-sitemap"}
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "local_global.toml"


@dataclass(frozen=True)
class GlobalConfig:
    database: Path
    queue: Path
    brands: tuple[str, ...] = BRANDS
    chrome_port: int = 9222
    min_delay_seconds: float = 15
    max_delay_seconds: float = 30
    max_pending_jobs: int = 120
    catalog_interval_seconds: float = 30
    seed_sources: tuple[Path, ...] = ()
    profile: Path | None = None
    alert_drop_fraction: float = 0.35
    alert_min_history_days: int = 5
    alert_confirmation_seconds: int = 180
    dashboard_port: int = 8098

    def __post_init__(self):
        if (
            not self.brands
            or len(set(self.brands)) != len(self.brands)
            or set(self.brands) - set(BRANDS)
            or type(self.chrome_port) is not int
            or not 1024 <= self.chrome_port <= 65535
            or type(self.dashboard_port) is not int
            or not 1024 <= self.dashboard_port <= 65535
            or self.dashboard_port == self.chrome_port
            or type(self.max_pending_jobs) is not int
            or not 20 <= self.max_pending_jobs <= 2000
            or any(
                type(x) not in (int, float) or not math.isfinite(x)
                for x in (
                    self.min_delay_seconds,
                    self.max_delay_seconds,
                    self.catalog_interval_seconds,
                )
            )
            or not 1 <= self.min_delay_seconds <= self.max_delay_seconds <= 3600
            or not 15 <= self.catalog_interval_seconds <= 3600
            or self.database == self.queue
            or self.database in self.seed_sources
            or self.queue in self.seed_sources
            or type(self.alert_drop_fraction) not in (float, int)
            or not 0 < self.alert_drop_fraction < 1
            or type(self.alert_min_history_days) is not int
            or not 2 <= self.alert_min_history_days <= 90
            or type(self.alert_confirmation_seconds) is not int
            or not 5 <= self.alert_confirmation_seconds <= 3600
        ):
            raise ValueError("Invalid global monitoring configuration")

    def settings(self):
        # Do not inherit unrelated shell/NAS secrets or paid API configuration.
        return Settings(
            _env_file=None,
            database_url="sqlite+aiosqlite:///" + self.database.as_posix(),
            redis_url="redis://localhost:6379/0",
            admin_token="",
            browser_proxy_url="",
            telegram_bot_token="",
            telegram_chat_id="",
            telegram_proxy_url="",
            bark_url="",
            webhook_url="",
            browser_channel="chrome",
            browser_session_dir=str(self.database.parent / "unused-external-sessions"),
            worker_concurrency=1,
            provider_concurrency=1,
            job_timeout_seconds=120,
            global_monitoring_enabled=True,
            global_jobs_per_tick=12,
            catalog_jobs_per_tick=1,
            max_pending_jobs=self.max_pending_jobs,
            alert_drop_fraction=str(self.alert_drop_fraction),
            alert_min_history_days=self.alert_min_history_days,
            alert_confirmation_seconds=self.alert_confirmation_seconds,
            alert_historical_lows_enabled=False,
            provider_overrides={
                b: ProviderPolicy(
                    enabled=b in self.brands,
                    concurrency=1,
                    interval_seconds=15,
                )
                for b in BRANDS
            },
        )


def load_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    data = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    for key in ("database", "queue"):
        data[key] = (path.parent / data[key]).resolve()
    data["seed_sources"] = tuple((path.parent / p).resolve() for p in data.get("seed_sources", []))
    data["brands"] = tuple(data.get("brands", BRANDS))
    if data.get("profile"):
        data["profile"] = (path.parent / data["profile"]).resolve()
    return GlobalConfig(**data)


def official_hotel(data):
    hotel = HotelData.model_validate(data)
    parsed = urlsplit(str(hotel.official_url))
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not any(host == d or host.endswith("." + d) for d in DOMAINS[hotel.provider])
    ):
        raise ValueError("Catalog imports must contain official hotel URLs")
    return hotel


def read_catalog(path):
    """Read only public hotel fields, not cookies, account settings or old prices."""
    fields = tuple(HotelData.model_fields)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        hotels = []
        for row in db.execute("SELECT " + ",".join(fields) + ",updated_at FROM hotels"):
            values = dict(row)
            updated_at = datetime.fromisoformat(values.pop("updated_at"))
            hotels.append((official_hotel(values), updated_at))
        catalogs = [
            (key, json.loads(value))
            for key, value in db.execute(
                "SELECT key,value FROM settings WHERE key IN (" + ",".join("?" * len(CATALOG_KEYS)) + ")",
                tuple(CATALOG_KEYS),
            )
        ]
    return hotels, catalogs


async def initialize(sessions, config):
    imported = {}
    async with sessions() as db, db.begin():
        for path in config.seed_sources:
            marker = "local_global:import:" + str(path)
            if not path.is_file() or await db.get(AppSetting, marker):
                continue
            hotels, catalogs = read_catalog(path)
            count = 0
            for hotel, updated_at in hotels:
                if hotel.provider not in config.brands:
                    continue
                existing = await db.scalar(
                    select(Hotel.id).where(
                        Hotel.provider == hotel.provider,
                        Hotel.provider_hotel_id == hotel.provider_hotel_id,
                    )
                )
                if existing is None:
                    added = await upsert_hotel(db, hotel)
                    added.updated_at = updated_at
                    count += 1
            # Preserve verified traversal, but not old prices or historical success counts.
            for key, value in catalogs:
                if await db.get(AppSetting, key) is None:
                    db.add(AppSetting(key=key, value=value))
            db.add(AppSetting(key=marker, value={"hotels_imported": count, "at": utcnow().isoformat()}))
            imported[path.name + ":" + path.parent.name] = count
        # New local mode explicitly selects CN discovery; only a successful root
        # can later claim traversal complete. An empty root is never coverage.
        if await db.get(AppSetting, "catalog:marriott") is None:
            db.add(
                AppSetting(
                    key="catalog:marriott",
                    value={
                        "root": MARRIOTT_ROOT,
                        "pages": {MARRIOTT_ROOT: {"next_scan_at": 0}},
                        "urls": [],
                    },
                )
            )
        if await db.get(AppSetting, CONTROL) is None:
            db.add(AppSetting(key=CONTROL, value={"stop": False, "running": False}))
    return imported


async def pin_denials(sessions):
    """Local user policy: 403/challenge/429 requires explicit resume, not auto retry."""
    async with sessions() as db, db.begin():
        saved = await db.get(AppSetting, PAUSES)
        value = dict(saved.value) if saved else {}
        states = (await db.scalars(select(ProviderStatus))).all()
        for state in states:
            if state.status == "BLOCKED" and state.last_error in {"BLOCKED_BY_ANTIBOT", "RATE_LIMITED"}:
                value.setdefault(state.provider, {"reason": state.last_error, "at": utcnow().isoformat()})
            if state.provider in value:
                state.blocked_until = datetime(2099, 1, 1, tzinfo=timezone.utc)
                state.status = "BLOCKED"
        if saved:
            saved.value = value
        else:
            db.add(AppSetting(key=PAUSES, value=value))


async def set_control(sessions, **values):
    async with sessions() as db, db.begin():
        changes = {**values, "updated_at": utcnow().isoformat()}
        # Phase updates must preserve a stop requested by another process.
        statement = insert(AppSetting).values(key=CONTROL, value=changes)
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={
                    "value": func.json_patch(AppSetting.value, json.dumps(changes)),
                    "updated_at": utcnow(),
                },
            )
        )


async def stopping(sessions):
    async with sessions() as db:
        row = await db.get(AppSetting, CONTROL)
        return bool(row and row.value.get("stop"))


async def dispatch(sessions, queue, settings):
    """Production fair/bounded cursors, no single-hotel 365-row expansion."""
    async with sessions() as db, db.begin():
        now = utcnow()
        await db.execute(
            update(CrawlJob)
            .where(
                CrawlJob.status == "RUNNING",
                CrawlJob.lease_until < now,
                CrawlJob.retry_count >= settings.max_retries,
            )
            .values(
                status="FAILED",
                error_type="TIMEOUT",
                error_message="Worker lease expired repeatedly",
                lease_owner=None,
                lease_until=None,
            )
        )
        await db.execute(
            update(CrawlJob)
            .where(
                CrawlJob.status == "RUNNING",
                CrawlJob.lease_until < now,
            )
            .values(
                status="PENDING",
                retry_count=CrawlJob.retry_count + 1,
                scheduled_at=now,
                lease_owner=None,
                lease_until=None,
            )
        )
        await expire_pending_stays(db)
        await reconcile_terminal_verifications(db)
        await expand_global(db, settings)
        now = utcnow()
        rows = await due_dispatch_jobs(db, now, settings.provider_overrides)
        eligible = []
        for job, priority in rows:
            state = await db.get(ProviderStatus, job.provider)
            if not settings.provider_policy(job.provider).enabled:
                continue
            if state and state.blocked_until and state.blocked_until.replace(tzinfo=now.tzinfo) > now:
                continue
            eligible.append((job, priority))
        head = await reserve_provider_turn(db, eligible)
        promoted = set()
        for job, priority in eligible:
            if priority < 100 and job.provider not in promoted:
                priority = max(priority, 99)
                promoted.add(job.provider)
            if job.id == head:
                priority = max(priority, 99.5)
            await queue.put(job.id, priority)
            job.status = "QUEUED"


async def interruptible(sessions, operation):
    task = asyncio.ensure_future(operation)
    try:
        while not task.done():
            if await stopping(sessions):
                task.cancel()
                return None
            await asyncio.wait({task}, timeout=1)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def report(config):
    """Read-only, deliberately never initialize a missing database."""
    if not config.database.is_file():
        return {"initialized": False, "scope": "WORLDWIDE", "brands": list(config.brands)}
    with sqlite3.connect(config.database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        settings = {
            r[0]: json.loads(r[1])
            for r in db.execute(
                "SELECT key,value FROM settings WHERE key IN (?,?)",
                (CONTROL, PAUSES),
            )
        }
        brands = {}
        for brand in config.brands:
            hotel = db.execute(
                "SELECT count(*),count(distinct country) FROM hotels WHERE provider=? AND active=1",
                (brand,),
            ).fetchone()
            state = db.execute(
                "SELECT status,last_error,last_success FROM provider_status WHERE provider=?", (brand,)
            ).fetchone()
            brands[brand] = {
                "hotels": hotel[0],
                "countries_with_metadata": hotel[1],
                "price_records": db.execute(
                    "SELECT count(*) FROM price_history WHERE provider=?", (brand,)
                ).fetchone()[0],
                "stay_observations": db.execute(
                    "SELECT count(*) FROM stay_scans s JOIN hotels h ON h.id=s.hotel_id WHERE h.provider=?",
                    (brand,),
                ).fetchone()[0],
                "jobs": dict(
                    db.execute(
                        "SELECT status,count(*) FROM crawl_jobs WHERE provider=? GROUP BY status", (brand,)
                    )
                ),
                "provider": dict(state) if state else {"status": "NOT_STARTED"},
                "manual_pause": settings.get(PAUSES, {}).get(brand),
            }
        return {
            "initialized": True,
            "scope": "WORLDWIDE",
            "monitoring_days": 365,
            "occupancy": {"adults": 2, "rooms": 1, "nights": 1},
            "max_pending_jobs": config.max_pending_jobs,
            "runtime": settings.get(CONTROL, {}),
            "brands": brands,
            "coverage_complete": False,
            "notifications": "not_configured; confirmed candidates retained locally",
        }


async def run(config, *, max_jobs=None, initialize_only=False):
    from app.providers.browser_session import close_browser_sessions, external_browser
    from app.providers.marriott_passive_bridge import MarriottPassiveBridgeProvider, collector_modules
    from app.services.sqlite_queue import SQLiteQueue

    config.database.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + config.database.as_posix(), connect_args={"timeout": 30}
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    queue = SQLiteQueue(config.queue)
    clients = RuntimeHttpClients()
    owner = await queue.lock("local_global:runner", 600)
    if not owner:
        await queue.close()
        await engine.dispose()
        raise RuntimeError("Global monitor is already running")
    factory = FACTORIES["marriott"]
    chrome_guard = ExitStack()
    dashboard = None
    try:
        async with engine.begin() as db:
            await db.run_sync(Base.metadata.create_all)
        imported = await initialize(sessions, config)
        print(json.dumps({"imported_catalog_hotels": imported}, ensure_ascii=False), flush=True)
        if initialize_only:
            return
        from app.global_dashboard import start_dashboard

        dashboard = start_dashboard(config)
        print(f"全球采集进度：http://127.0.0.1:{config.dashboard_port}/", flush=True)
        # External profile is user-started; no cookies are exported/imported.
        collector_modules()
        from main import RunLock

        chrome_guard.enter_context(
            RunLock(
                Path(__file__).resolve().parents[2]
                / "collector"
                / "data"
                / f"automatic-chrome-{config.chrome_port}.lock"
            )
        )
        if config.profile is not None:
            # Reuse the ordinary Chrome profile; ensure_chrome attaches if it is already open.
            from types import SimpleNamespace

            from passive_capture import ensure_chrome

            await asyncio.to_thread(
                ensure_chrome,
                SimpleNamespace(
                    port=config.chrome_port,
                    profile=config.profile,
                    chrome=None,
                    launch=True,
                    start_url="https://www.marriott.com.cn/",
                ),
            )
        FACTORIES["marriott"] = lambda: MarriottPassiveBridgeProvider(
            port=config.chrome_port,
            browser_channel="chrome",
            session_dir=provider_settings().browser_session_dir,
        )
        settings = config.settings()
        await clients.update(settings)
        await set_control(sessions, stop=False, running=True)
        completed = 0
        next_catalog = 0
        async with sessions() as db:
            cursor = await db.get(AppSetting, "local_global:catalog_turn")
            turn = cursor.value.get("turn", 0) if cursor else 0
        with external_browser(f"http://127.0.0.1:{config.chrome_port}"):
            while not await stopping(sessions):
                # Single worker, plus a long lease refreshed before each bounded cycle.
                if not await queue.renew("local_global:runner", owner, 600):
                    raise RuntimeError("Global monitor lease lost")
                await pin_denials(sessions)
                if asyncio.get_running_loop().time() >= next_catalog:
                    brand = config.brands[turn % len(config.brands)]
                    await set_control(sessions, phase="catalog", provider=brand)
                    try:
                        await interruptible(
                            sessions, DISCOVER[brand](sessions, queue, settings, clients.catalog)
                        )
                    except Exception as exc:
                        print(json.dumps({"catalog": brand, "error": type(exc).__name__}), flush=True)
                    turn += 1
                    async with sessions() as db, db.begin():
                        cursor = await db.get(AppSetting, "local_global:catalog_turn")
                        if cursor:
                            cursor.value = {"turn": turn}
                        else:
                            db.add(AppSetting(key="local_global:catalog_turn", value={"turn": turn}))
                    next_catalog = asyncio.get_running_loop().time() + config.catalog_interval_seconds
                    await pin_denials(sessions)
                if await stopping(sessions):
                    break
                await dispatch(sessions, queue, settings)
                await set_control(sessions, phase="price_or_directory_job", provider=None)
                processed = await interruptible(sessions, process_one(sessions, queue, settings))
                await pin_denials(sessions)
                if processed:
                    completed += 1
                    print(json.dumps({"processed_jobs": completed, "scope": "WORLDWIDE"}), flush=True)
                if max_jobs is not None and completed >= max_jobs:
                    break
                delay = random.uniform(config.min_delay_seconds, config.max_delay_seconds) if processed else 3
                await interruptible(sessions, asyncio.sleep(delay))
    finally:
        FACTORIES["marriott"] = factory
        with contextlib.suppress(Exception):
            await set_control(sessions, running=False, phase="stopped")
        await close_browser_sessions()
        await clients.close()
        if owner:
            await queue.unlock("local_global:runner", owner)
        await queue.close()
        await engine.dispose()
        chrome_guard.close()
        if dashboard is not None:
            await asyncio.to_thread(dashboard.shutdown)
            dashboard.server_close()


async def control(config, *, resume=None, stop=False):
    if not config.database.is_file():
        raise ValueError("Global monitor has not been initialized")
    engine = create_async_engine("sqlite+aiosqlite:///" + config.database.as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if stop:
            await set_control(sessions, stop=True)
        if resume:
            async with sessions() as db, db.begin():
                saved = await db.get(AppSetting, PAUSES)
                if saved:
                    saved.value = {k: v for k, v in saved.value.items() if k != resume}
                state = await db.get(ProviderStatus, resume)
                if state:
                    state.blocked_until, state.last_error, state.status = None, None, "DEGRADED"
                await db.execute(
                    update(CrawlJob)
                    .where(
                        CrawlJob.provider == resume,
                        CrawlJob.status.in_(("PENDING", "QUEUED")),
                    )
                    .values(scheduled_at=utcnow())
                )
    finally:
        await engine.dispose()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--status", action="store_true")
    group.add_argument("--stop", action="store_true")
    group.add_argument("--resume-brand", choices=BRANDS)
    group.add_argument("--initialize-only", action="store_true")
    parser.add_argument("--max-jobs", type=int)
    args = parser.parse_args()
    if args.max_jobs is not None and args.max_jobs < 1:
        parser.error("--max-jobs must be positive")
    config = load_config(args.config)
    if args.status:
        print(json.dumps(report(config), ensure_ascii=False, indent=2))
    elif args.stop or args.resume_brand:
        asyncio.run(control(config, resume=args.resume_brand, stop=args.stop))
    else:
        try:
            asyncio.run(run(config, max_jobs=args.max_jobs, initialize_only=args.initialize_only))
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    cli()
