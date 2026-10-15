"""Execute one unobserved, automatically generated CN Marriott price task.

Uses the marked isolated catalog audit database, not the NAS database. Does not
select a hotel manually, reset dates/retries, or deliver Telegram notifications.
"""

import argparse
import asyncio
import json
from pathlib import Path

from audit_ihg_price import run_existing_price
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting
from app.providers.browser_session import close_browser_sessions
from app.providers.marriott_cn_catalog import ROOT
from app.providers.registry import FACTORIES
from app.services.ihg_catalogs import selected_directory_root
from app.services.queue import Queue


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--session-dir", required=True)
    args = parser.parse_args()
    if not args.database.is_file() or args.report.exists():
        parser.error("Use the existing marked catalog audit database and a new report path")
    settings = Settings(
        _env_file=None,
        browser_channel="chrome",
        browser_session_dir=args.session_dir,
        browser_proxy_url="",
        provider_overrides={name: ProviderPolicy(enabled=name == "marriott") for name in FACTORIES},
        telegram_bot_token="",
        telegram_chat_id="",
        job_timeout_seconds=300,
    )
    engine = create_async_engine("sqlite+aiosqlite:///" + args.database.resolve().as_posix())
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    redis = FakeRedis(decode_responses=True)
    report = {"status": "RUNNING", "telegram_delivery_called": False}
    try:
        async with sessions() as db:
            marker = await db.get(AppSetting, "audit:directory-only")
            catalog = await db.get(AppSetting, "catalog:marriott")
            if not marker or marker.value != {"provider": "marriott"}:
                raise ValueError("Missing isolated Marriott audit marker")
            if not catalog or selected_directory_root(catalog.value, "marriott") != ROOT:
                raise ValueError("Expected the previously verified CN root audit")
        print("stage=automatic_price_worker", flush=True)
        report["price_job"] = await run_existing_price(sessions, Queue(redis), settings, provider="marriott")
        report["status"] = "SUCCEEDED" if report["price_job"]["outcome"] == "SUCCEEDED" else "FAILED"
    except BaseException as exc:
        report.update(status="FAILED", diagnostic_error_type=type(exc).__name__)
        raise
    finally:
        try:
            await close_browser_sessions()
            report["cleanup_status"] = "SUCCEEDED"
        except BaseException:
            report.update(cleanup_status="FAILED", status="FAILED")
            raise
        finally:
            await redis.aclose()
            await engine.dispose()
            args.report.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
            print(json.dumps({k: report.get(k) for k in ("status", "cleanup_status")}), flush=True)
    if report["status"] != "SUCCEEDED":
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
