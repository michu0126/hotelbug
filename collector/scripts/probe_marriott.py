"""One explicit Marriott live query; no bulk schedule, sample fallback or notifier."""

import argparse
import asyncio
import json
from pathlib import Path

from adapters.marriott import MarriottAdapter, _stay
from adapters.marriott_browser import DEFAULT_PROFILE, HOMEPAGE
from configuration import BrowserSettings, RequestPolicy
from main import RunLock
from models import Hotel, Stay, utcnow
from runtime import BrandPaused, BrandRunner
from storage import Storage

BASE = Path(__file__).resolve().parents[1]


def synchronize_observed_denial(storage, report):
    """Defence in depth: a logged official 403 must leave a durable pause."""
    responses = report.get("diagnostics", {}).get("responses", [])
    if not any(
        event.get("status") == 403 and event.get("host") in ("www.marriott.com", "www.marriott.com.cn")
        for event in responses
    ):
        return
    storage.pause("marriott", "HTTP_403_OR_VISIBLE_CHALLENGE")
    if report.get("status") != "BRAND_PAUSED":
        report["original_outcome"] = {
            "status": report.get("status"),
            "error_type": report.pop("error_type", None),
        }
        report["status"] = "BRAND_PAUSED"
        report["pause_reconciled_from_observed_http_403"] = True


def reconcile_report(report_path, database):
    """Repair a prior recorded denial without opening any browser or network."""
    report_path, database = Path(report_path), Path(database)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    with RunLock(database.with_suffix(".lock")):
        storage = Storage(database)
        try:
            synchronize_observed_denial(storage, report)
            row = storage.db.execute(
                "SELECT reason,paused_until FROM brand_pauses WHERE brand='marriott'"
            ).fetchone()
            report["pause"] = dict(row) if row else None
            report["state_reconciled_at"] = utcnow().isoformat()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            storage.close()
    return report


async def probe(args):
    code, ci, co = _stay(args.hotel, args.checkin, args.checkout)
    today = utcnow().date()
    if ci < today or (co - today).days > 366 or (co - ci).days != 1:
        raise ValueError("The live acceptance probe requires one future night within a year")
    database = Path(args.database).resolve()
    report_path = Path(args.report).resolve()
    report = {
        "started_at": utcnow().isoformat(),
        "hotel_id": code,
        "checkin": str(ci),
        "checkout": str(co),
        "adults": args.adults,
        "rooms": 1,
        "proxy": args.proxy,
        "mode": "live",
        "headless": args.headless,
        "channel": args.channel,
        "persistent_profile": True,
        "profile_dir": str(DEFAULT_PROFILE),
        "homepage": HOMEPAGE,
        "workflow": "HOMEPAGE_HOTEL_DATES_VIEW_RATES",
        "max_attempts": 1,
        "notifications_enabled": False,
        "nas_modified": False,
        "prices_saved": 0,
    }
    adapter = MarriottAdapter(
        mode="live",
        adults=args.adults,
        hotel_name=getattr(args, "hotel_name", None),
        browser_settings=BrowserSettings(
            channel=args.channel,
            headless=args.headless,
            proxy=args.proxy,
            navigation_timeout_ms=args.timeout_ms,
        ),
    )

    class RunnerBridge:
        async def fetch_rates(self, hotel, stay, *, confirmation=False):
            return await adapter.fetch_prices(hotel.hotel_id, stay.check_in, stay.check_out)

    hotel = Hotel("marriott", code, code, "https://www.marriott.com/")
    stay = Stay(ci, co, args.adults, 1)
    with RunLock(database.with_suffix(".lock")):
        storage = Storage(database)
        try:
            if getattr(args, "resume_brand", False):
                prior = storage.db.execute("SELECT * FROM brand_pauses WHERE brand='marriott'").fetchone()
                report["explicit_resume"] = {
                    "at": utcnow().isoformat(),
                    "prior_pause": dict(prior) if prior else None,
                }
                storage.resume("marriott")
            policy = RequestPolicy(max_attempts=1, timeout_seconds=args.timeout_ms / 1000 * 6 + 15)
            records = await BrandRunner(storage, policy).fetch(RunnerBridge(), hotel, stay)
            for record in records:
                if record.source_kind != "LIVE" or record.captured_at is None or record.adults != args.adults:
                    raise ValueError("Live observation provenance mismatch")
            for record in records:
                storage.save_price_record(record)
            report["status"] = "SUCCESS" if records else "VERIFIED_EMPTY_CASH_RESULT"
            report["prices_saved"] = len(records)
            report["prices"] = [record.to_dict() for record in records]
        except BrandPaused:
            report["status"] = "BRAND_PAUSED"
        except Exception as exc:
            report.update(status="FAILED", error_type=type(exc).__name__)
        finally:
            await adapter.close()
            report["diagnostics"] = adapter.diagnostics
            synchronize_observed_denial(storage, report)
            row = storage.db.execute(
                "SELECT reason,paused_until FROM brand_pauses WHERE brand='marriott'"
            ).fetchone()
            report["pause"] = dict(row) if row else None
            report["finished_at"] = utcnow().isoformat()
            storage.close()
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hotel", required=True)
    parser.add_argument("--checkin", required=True)
    parser.add_argument("--checkout", required=True)
    parser.add_argument("--adults", type=int, default=2)
    parser.add_argument(
        "--hotel-name", help="Official hotel name for homepage search; NYCMQ is already known"
    )
    parser.add_argument(
        "--resume-brand",
        action="store_true",
        help="Explicitly resume Marriott once, preserving prior pause in the report",
    )
    parser.add_argument("--proxy")
    parser.add_argument("--channel", choices=["chrome", "msedge"], default="chrome")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout-ms", type=int, default=45000)
    parser.add_argument("--database", type=Path, default=BASE / "data" / "marriott-live.sqlite")
    parser.add_argument("--report", type=Path, default=BASE / "data" / "marriott-live-probe.json")
    report = asyncio.run(probe(parser.parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "SUCCESS" else 2


if __name__ == "__main__":
    raise SystemExit(cli())
