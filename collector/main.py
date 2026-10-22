"""Mock by default; optional Hilton ordinary-page collection from verified sample schemas."""

import argparse
import asyncio
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from adapters.base import SamplesRequired, TransientFailure, official_url
from adapters.hilton import HiltonAdapter, MockHiltonAdapter
from configuration import load_config
from detector import confirms, detect
from models import Quote, utcnow
from notifier import BarkNotifier, NotificationUncertain
from runtime import BrandPaused, BrandRunner, synchronize_capture_pause
from storage import Storage

log = logging.getLogger("hotelbug.collector")


class Monitor:
    def __init__(self, config, storage, adapters, *, notifier=None, clock=utcnow, runner=None):
        self.config, self.storage, self.adapters, self.clock = config, storage, adapters, clock
        self.runner = runner or BrandRunner(storage, config.requests, clock=clock)
        self.notifier = notifier or BarkNotifier(config.bark)

    async def fetch(self, hotel, stay, *, confirmation=False):
        quotes = await self.runner.fetch(self.adapters[hotel.brand], hotel, stay, confirmation=confirmation)
        now = self.clock()
        for quote in quotes:
            official_url(quote.hotel.brand, quote.source_url)
            if (
                quote.hotel.brand != hotel.brand
                or quote.hotel.hotel_id != hotel.hotel_id
                or quote.stay != stay
                or quote.is_mock != (self.config.mode == "mock")
                or quote.observed_at > now + timedelta(seconds=5)
                or quote.observed_at < now - timedelta(seconds=self.config.requests.timeout_seconds)
            ):
                raise ValueError("Adapter returned a mismatched or stale quote")
        return quotes

    async def observe(self, hotel, stay):
        created = 0
        for quote in await self.fetch(hotel, stay):
            result = detect(
                quote, self.storage.history(quote, self.config.detector.history_days), self.config.detector
            )
            self.storage.save(quote)
            if result and self.storage.candidate(quote, *result, self.config.detector):
                created += 1
                log.info(
                    "%s %s %s: suspected price drop; awaiting independent confirmation%s",
                    hotel.brand,
                    hotel.name,
                    stay.check_in,
                    " [MOCK]" if quote.is_mock else "",
                )
        return created

    async def confirm_due(self):
        confirmed = 0
        for candidate in self.storage.due(self.clock()):
            if datetime.fromisoformat(candidate["expires_at"]) <= self.clock():
                self.storage.finish_candidate(candidate["id"], "EXPIRED", "Confirmation window expired")
                continue
            first = Quote.from_dict(json.loads(candidate["first_quote"]))
            try:
                quotes = await self.fetch(first.hotel, first.stay, confirmation=True)
            except (BrandPaused, TransientFailure):
                continue  # Durable PENDING state remains until retry or expiry.
            for quote in quotes:
                self.storage.save(quote)
            matched = next(
                (
                    quote
                    for quote in quotes
                    if confirms(first, quote, Decimal(candidate["threshold"]), self.config.detector)
                ),
                None,
            )
            self.storage.finish_candidate(
                candidate["id"],
                "READY" if matched else "REJECTED",
                None if matched else "Independent quote did not confirm the same-series drop",
                matched,
            )
            confirmed += matched is not None
        return confirmed

    async def notify_ready(self):
        sent = 0
        for candidate in self.storage.ready():
            quote = Quote.from_dict(json.loads(candidate["confirmation_quote"]))
            if (
                quote.observed_at + timedelta(seconds=self.config.detector.confirmation_max_age_seconds)
                <= self.clock()
            ):
                self.storage.finish_candidate(
                    candidate["id"], "EXPIRED", "Confirmed quote is no longer fresh", quote
                )
                continue
            if quote.is_mock or not self.config.bark.enabled:
                self.storage.finish_candidate(
                    candidate["id"], "DRY_RUN", "Mock or Bark disabled; not sent", quote
                )
                log.info("Confirmed candidate retained as dry-run; no Bark request [MOCK=%s]", quote.is_mock)
                continue
            if not self.notifier.configured:
                log.warning(
                    "Bark device key missing; confirmed candidate retained without delivery or cooldown"
                )
                continue
            reserved = self.storage.reserve_notification(
                candidate, self.clock(), self.config.detector.cooldown_hours
            )
            if reserved != "RESERVED":
                self.storage.finish_candidate(candidate["id"], reserved, "24-hour delivery guard", quote)
                continue
            try:
                await self.notifier.send(quote, Decimal(candidate["baseline"]))
            except (NotificationUncertain, ValueError):
                self.storage.notification_result(
                    quote.series_key, self.clock(), self.config.detector.cooldown_hours, success=False
                )
                self.storage.finish_candidate(
                    candidate["id"], "UNKNOWN", "Bark not acknowledged; no automatic resend", quote
                )
                log.warning("Bark delivery not acknowledged; same-series resend suspended for 24 hours")
            else:
                self.storage.notification_result(
                    quote.series_key, self.clock(), self.config.detector.cooldown_hours, success=True
                )
                self.storage.finish_candidate(candidate["id"], "SENT", confirmation=quote)
                sent += 1
        return sent

    async def tick(self, *, poll=True):
        result = {"candidates": 0, "confirmed": await self.confirm_due(), "sent": await self.notify_ready()}
        if not poll:
            return result
        for hotel in self.config.watchlist:
            for stay in self.config.dates.stays(self.clock().date()):
                try:
                    result["candidates"] += await self.observe(hotel, stay)
                except BrandPaused:
                    log.warning("%s is paused; remaining dates skipped", hotel.brand)
                    break
                except TransientFailure:
                    log.warning(
                        "%s transient failure after bounded retry; no zero-price observation saved",
                        hotel.brand,
                    )
        return result


class RunLock:
    """OS-owned singleton lock: released even if the process crashes."""

    def __init__(self, path):
        self.path, self.file = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.tell() == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            import os

            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another collector is already running for this database") from None
        return self

    def __exit__(self, *_):
        self.file.close()


async def run(args):
    config = load_config(Path(args.config))
    if args.resume_brand and args.resume_brand != "hilton":
        raise ValueError("Only Hilton is supported in this stage")
    with RunLock(config.sqlite_path.with_suffix(".lock")):
        storage = Storage(config.sqlite_path)
        adapters = {
            "hilton": MockHiltonAdapter()
            if config.mode == "mock"
            else HiltonAdapter(config.browser, config.requests)
        }
        try:
            if config.mode == "live":
                synchronize_capture_pause(
                    storage,
                    Path(__file__).with_name("samples") / "hilton-paused.json",
                    resume=args.resume_brand == "hilton",
                )
            if args.resume_brand:
                storage.resume(args.resume_brand)
            monitor = Monitor(config, storage, adapters)
            log.info(
                "Running in MOCK mode: synthetic data only, no real Bark delivery"
                if config.mode == "mock"
                else "Running Hilton ordinary-page collection; 403/challenge will pause the brand"
            )
            next_poll = 0.0
            while True:
                poll = asyncio.get_running_loop().time() >= next_poll
                log.info("Tick finished: %s", await monitor.tick(poll=poll))
                if poll:
                    next_poll = asyncio.get_running_loop().time() + config.loop_interval_seconds
                if args.once:
                    break
                await asyncio.sleep(
                    min(
                        max(1, next_poll - asyncio.get_running_loop().time()),
                        max(1, config.detector.confirmation_delay_seconds),
                    )
                )
        finally:
            for adapter in adapters.values():
                await adapter.close()
            storage.close()


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.yaml")))
    parser.add_argument(
        "--once", action="store_true", help="One poll; pending confirmations remain in SQLite"
    )
    parser.add_argument("--resume-brand", help="Explicitly clear a reviewed brand pause")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(parser.parse_args()))
    except (ValueError, RuntimeError, SamplesRequired) as exc:
        parser.exit(2, f"{exc}\n")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
