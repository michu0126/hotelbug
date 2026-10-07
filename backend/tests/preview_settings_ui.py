"""Local UI acceptance sandbox: temporary DB, fake queue, no worker or notifications."""

import argparse
import asyncio
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fakeredis.aioredis import FakeRedis
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api import main as api
from app.core.config import Settings
from app.database.session import get_session
from app.models.tables import AppSetting, Base, Hotel, PriceAlert, Watchlist, utcnow
from app.providers.marriott_catalog import ROOT
from app.schemas.domain import HotelData, RateData
from app.services.rates import persist_rates, upsert_hotel
from app.services.runtime_settings import MonitoringInput, save_runtime_settings


async def main(
    catalog_audit=False, history_audit=False, alert_audit=False, offer_audit=False, watch_audit=False
):
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        raise SystemExit("Build frontend first")
    with TemporaryDirectory(prefix="hotelbug-ui-preview-") as directory:
        engine = create_async_engine("sqlite+aiosqlite:///" + str(Path(directory) / "test.db"))
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with sessions() as db, db.begin():
            await save_runtime_settings(
                db,
                MonitoringInput(
                    providers={
                        name: {"enabled": False}
                        for name in ("marriott", "ihg", "gha", "accor", "hilton", "hyatt")
                    }
                ),
            )
            if watch_audit:
                # Disposable UI-only ranges and metadata; never production
                # hotels or quotes. Every group stays disabled, no Worker runs.
                for index in range(105):
                    db.add(
                        Watchlist(
                            id=f"offline-watch-{index:03}",
                            name=f"离线分页范围 {index:03}",
                            scope="country",
                            filters={"country": f"OFFLINE-{index:03}", "days_ahead": 365},
                        )
                    )
                db.add(
                    Hotel(
                        id="offline-watch-hotel",
                        provider="marriott",
                        provider_hotel_id="OFFLN",
                        hotel_name="离线范围测试酒店（非真实酒店，无报价）",
                        city="Shanghai",
                        country="CN",
                        brand="Marriott",
                        official_url="https://example.com/offline-watch-test",
                    )
                )
            if catalog_audit:
                # Explicitly synthetic UI-only progress. No hotels, prices,
                # worker, real Telegram or production database are involved.
                leaf = "https://www.marriott.com/en/destinations/offline-preview.mi"
                now = utcnow()
                db.add(
                    AppSetting(
                        key="catalog:marriott",
                        value={
                            "pages": {
                                ROOT: {
                                    "status": "PARTIAL",
                                    "page_complete": True,
                                    "reported_total": 3,
                                    "last_scan_at": now.timestamp(),
                                    "directory_urls": [leaf],
                                    "hotel_ids": [],
                                },
                                leaf: {
                                    "status": "PARTIAL",
                                    "page_complete": True,
                                    "reported_total": 3,
                                    "last_scan_at": now.timestamp(),
                                    "directory_urls": [leaf + "?pg=2"],
                                    "hotel_ids": ["OFFLINE-PREVIEW"],
                                },
                                leaf + "?pg=2": {"next_scan_at": (now - timedelta(seconds=1)).timestamp()},
                            }
                        },
                    )
                )
            if history_audit or alert_audit or offer_audit:
                h = await upsert_hotel(
                    db,
                    HotelData(
                        provider="accor",
                        provider_hotel_id="TEST",
                        hotel_name="离线界面测试（非真实酒店，报价为测试数据）",
                        default_currency="EUR",
                        official_url="https://example.com/offline-ui-test",
                    ),
                )
                for i, age in enumerate((29, 8, 2, 1)):
                    rate = RateData(
                        provider="accor",
                        provider_hotel_id="TEST",
                        check_in=date.today() + timedelta(days=8),
                        check_out=date.today() + timedelta(days=9),
                        room_type="测试标准房（非真实房型）",
                        room_code="TEST-KING",
                        rate_name="离线公开方案（非真实报价）",
                        rate_code="TEST-PUBLIC",
                        currency="EUR",
                        total_price=(200, 180, 160, 70)[i],
                        adults=2,
                        rooms=1,
                        availability=True,
                        member_rate=False,
                        captured_at=utcnow() - timedelta(days=age),
                        source_url="https://example.com/offline-ui-test",
                        raw_response_hash="0" * 64,
                    )
                    await persist_rates(db, h, [rate], f"offline-{i}")
                    await persist_rates(
                        db,
                        h,
                        [
                            rate.model_copy(
                                update={
                                    "rate_code": "TEST-SECOND",
                                    "rate_name": "第二个测试方案（不混入第一方案）",
                                    "total_price": Decimal(100 + i),
                                }
                            )
                        ],
                        f"offline-other-{i}",
                    )
                    if offer_audit:
                        for name in ("Advance Purchase", "Spa Package", "Corporate Rate"):
                            await persist_rates(
                                db,
                                h,
                                [
                                    rate.model_copy(
                                        update={
                                            "rate_name": name + "（离线测试，非真实报价）",
                                            "rate_code": "TEST-" + name.upper().replace(" ", "-"),
                                            "total_price": Decimal(20 + i),
                                        }
                                    )
                                ],
                                f"offline-special-{name}-{i}",
                            )
                if alert_audit:
                    # Explicit UI-only states, not observed hotel events. No
                    # worker or notification outbox exists in this preview.
                    for state in (
                        "PENDING",
                        "FAILED",
                        "CANCELLED",
                        "REJECTED",
                        "CONFIRMED",
                        "HISTORICAL_LOW",
                        *(("EXCLUDED_OFFER",) if offer_audit else ()),
                    ):
                        db.add(
                            PriceAlert(
                                hotel_id=h.id,
                                offer_key=rate.offer_key(),
                                event_type="NEW_HISTORICAL_LOW"
                                if state == "HISTORICAL_LOW"
                                else "PRICE_DROP",
                                anomaly_score=30
                                if state == "HISTORICAL_LOW"
                                else 55
                                if state == "CONFIRMED"
                                else 45,
                                confirmed=state in ("CONFIRMED", "HISTORICAL_LOW"),
                                dedupe_key=f"offline-alert-ui-{state}",
                                payload={
                                    "hotel_name": f"离线界面测试 · {state}（非真实行情）",
                                    "provider": h.provider,
                                    "check_in": rate.check_in.isoformat(),
                                    "check_out": rate.check_out.isoformat(),
                                    "price": "180" if state == "HISTORICAL_LOW" else "70",
                                    "currency": "EUR",
                                    "baseline": "200",
                                    "baseline_window_days": 30,
                                    "verification": "CONFIRMED"
                                    if state == "HISTORICAL_LOW"
                                    else "REJECTED"
                                    if state == "EXCLUDED_OFFER"
                                    else state,
                                    "new_historical_low": True,
                                    "historical_low": "190",
                                    "anomaly_score": 30
                                    if state == "HISTORICAL_LOW"
                                    else 55
                                    if state == "CONFIRMED"
                                    else 45,
                                    "anomaly_level": "NORMAL" if state == "HISTORICAL_LOW" else "LOW_PRICE",
                                    "score_breakdown": {"new_low": 20, "verified": 10}
                                    if state == "HISTORICAL_LOW"
                                    else {
                                        "drop_50": 25,
                                        "new_low": 20,
                                        **({"verified": 10} if state == "CONFIRMED" else {}),
                                    },
                                    "verification_error": "TIMEOUT" if state == "FAILED" else None,
                                    "verification_reason": "PROVIDER_DISABLED"
                                    if state == "CANCELLED"
                                    else "EXCLUDED_OFFER"
                                    if state == "EXCLUDED_OFFER"
                                    else None,
                                    "offer_classification": {
                                        "alert_eligible": False,
                                        "tags": ["POINTS_PLUS_CASH"],
                                    }
                                    if state == "EXCLUDED_OFFER"
                                    else None,
                                },
                            )
                        )

        async def dependency():
            async with sessions() as db:
                yield db

        # No production database or real Telegram credentials enter this sandbox.
        api.settings = Settings(
            _env_file=None, admin_token="preview-only", telegram_bot_token="", telegram_chat_id=""
        )
        api.redis = FakeRedis(decode_responses=True)
        api.app.dependency_overrides[get_session] = dependency
        api.app.mount("/", StaticFiles(directory=dist, html=True), name="preview")
        print("Local disposable UI preview: http://127.0.0.1:8099 · token: preview-only", flush=True)
        try:
            await uvicorn.Server(
                uvicorn.Config(api.app, host="127.0.0.1", port=8099, log_level="warning")
            ).serve()
        finally:
            await api.redis.aclose()
            await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--catalog-audit", action="store_true", help="Synthetic catalog graph for local UI QA only"
    )
    parser.add_argument(
        "--history-audit", action="store_true", help="Explicitly synthetic history for UI QA only"
    )
    parser.add_argument(
        "--alert-audit", action="store_true", help="Explicitly synthetic alert statuses for UI QA only"
    )
    parser.add_argument(
        "--offer-audit", action="store_true", help="Explicitly synthetic special rate plans for UI QA only"
    )
    parser.add_argument(
        "--watch-audit", action="store_true", help="105 synthetic scopes for pagination UI QA, no crawler"
    )
    args = parser.parse_args()
    asyncio.run(
        main(args.catalog_audit, args.history_audit, args.alert_audit, args.offer_audit, args.watch_audit)
    )
