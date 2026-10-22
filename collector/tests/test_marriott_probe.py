"""Probe orchestration with fake adapters; no real network or notifications."""

import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from adapters.base import BrandBlocked, TransientFailure
from adapters.marriott import parse_prices
from configuration import RequestPolicy
from models import PriceRecord, utcnow
from scripts import probe_marriott
from storage import Storage


def arguments(tmp_path):
    ci = utcnow().date() + timedelta(days=30)
    return SimpleNamespace(
        hotel="NYCMQ",
        checkin=str(ci),
        checkout=str(ci + timedelta(days=1)),
        adults=2,
        database=tmp_path / "live.sqlite",
        report=tmp_path / "probe.json",
        channel="chrome",
        headless=True,
        proxy=None,
        timeout_ms=5000,
    )


@pytest.fixture
def fast_policy(monkeypatch):
    monkeypatch.setattr(
        probe_marriott,
        "RequestPolicy",
        lambda **kwargs: RequestPolicy(delay_min_seconds=0, delay_max_seconds=0, **kwargs),
    )


async def test_probe_persists_denial_and_second_call_cannot_resume(tmp_path, monkeypatch, fast_policy):
    calls, closed = [], []

    class Denied:
        diagnostics = {"stage": "PUBLIC_PAGE", "responses": [{"status": 403}]}

        async def fetch_prices(self, *args):
            calls.append(args)
            raise BrandBlocked("Fixture denial")

        async def close(self):
            closed.append(True)

    monkeypatch.setattr(probe_marriott, "MarriottAdapter", lambda **kwargs: Denied())
    args = arguments(tmp_path)
    first = await probe_marriott.probe(args)
    second = await probe_marriott.probe(args)
    assert first["status"] == second["status"] == "BRAND_PAUSED"
    assert first["pause"]["reason"] == "HTTP_403_OR_VISIBLE_CHALLENGE"
    assert len(calls) == 1 and len(closed) == 2
    assert not first["notifications_enabled"] and not first["nas_modified"]
    assert first["prices_saved"] == second["prices_saved"] == 0
    assert json.loads(args.report.read_text())["status"] == "BRAND_PAUSED"


async def test_live_records_are_stored_separately_from_samples_and_monitoring(
    tmp_path, monkeypatch, fast_policy
):
    body = json.loads((Path(__file__).parent / "fixtures/marriott_observed.json").read_text())["rates"]
    (sample,) = parse_prices(body, "NYCMQ", "2026-10-07", "2026-10-08")
    payload = sample.to_dict()
    args = arguments(tmp_path)
    payload.update(
        checkin=args.checkin,
        checkout=args.checkout,
        adults=2,
        source_kind="LIVE",
        captured_at=utcnow().isoformat(),
    )
    live = PriceRecord.from_dict(payload)

    class Successful:
        diagnostics = {"stage": "COMPLETE"}

        async def fetch_prices(self, *args):
            return [live]

        async def close(self):
            pass

    monkeypatch.setattr(probe_marriott, "MarriottAdapter", lambda **kwargs: Successful())
    report = await probe_marriott.probe(args)
    assert report["status"] == "SUCCESS" and report["prices_saved"] == 1
    db = Storage(args.database)
    try:
        (saved,) = db.price_records("marriott", "NYCMQ", args.checkin, args.checkout, member_rate=True)
        assert saved == live and saved.source_kind == "LIVE"
        assert saved.series_key != sample.series_key
        assert db.db.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0
        assert db.db.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    finally:
        db.close()


async def test_late_logged_403_is_not_left_as_unpaused_timeout(tmp_path, monkeypatch, fast_policy):
    class LateDenied:
        diagnostics = {"responses": [{"host": "www.marriott.com", "status": 403}]}

        async def fetch_prices(self, *args):
            raise TransientFailure("Synthetic initial timeout before delayed response")

        async def close(self):
            pass

    monkeypatch.setattr(probe_marriott, "MarriottAdapter", lambda **kwargs: LateDenied())
    args = arguments(tmp_path)
    report = await probe_marriott.probe(args)
    assert report["status"] == "BRAND_PAUSED"
    assert report["original_outcome"]["error_type"] == "TransientFailure"
    assert report["pause_reconciled_from_observed_http_403"]
    assert report["pause"]["reason"] == "HTTP_403_OR_VISIBLE_CHALLENGE"
    repaired = probe_marriott.reconcile_report(args.report, args.database)
    assert repaired["original_outcome"] == report["original_outcome"]
    assert repaired["state_reconciled_at"]


async def test_explicit_resume_preserves_prior_pause_and_denial_pauses_again(
    tmp_path, monkeypatch, fast_policy
):
    args = arguments(tmp_path)
    db = Storage(args.database)
    db.pause("marriott", "PREVIOUS_DENIAL")
    db.close()
    calls = []

    class Denied:
        diagnostics = {"stage": "HOMEPAGE"}

        async def fetch_prices(self, *values):
            calls.append(values)
            raise BrandBlocked("Fixture denial")

        async def close(self):
            pass

    monkeypatch.setattr(probe_marriott, "MarriottAdapter", lambda **kwargs: Denied())
    args.resume_brand = True
    report = await probe_marriott.probe(args)
    assert len(calls) == 1 and report["status"] == "BRAND_PAUSED"
    assert report["explicit_resume"]["prior_pause"]["reason"] == "PREVIOUS_DENIAL"
    assert report["pause"]["reason"] == "HTTP_403_OR_VISIBLE_CHALLENGE"
    args.resume_brand = False
    await probe_marriott.probe(args)
    assert len(calls) == 1
