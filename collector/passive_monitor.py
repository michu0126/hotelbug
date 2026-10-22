"""Local capture -> comparable history -> independent confirmation -> notification.

No browser driver or hotel HTTP client exists in this module. A file timestamp
is not an independent observation. Legacy exports stay isolated as samples.
"""

import asyncio
import json
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from adapters.marriott import RATES_PATH, parse_capture
from configuration import BarkSettings
from models import PriceRecord, utc, utcnow
from notifier import BarkNotifier, NotificationUncertain, TelegramNotifier
from passive_capture import EMAIL_RE, JWT_RE, capture_event, process_capture, redact

MAX_CAPTURE_BYTES = 25_000_000


def build_passive_notifier(settings):
    if settings.channel == "bark":
        return BarkNotifier(
            BarkSettings(
                enabled=True, server=settings.bark_server, device_key_env=settings.bark_device_key_env
            )
        )
    if settings.channel == "telegram":
        return TelegramNotifier(settings)
    return None  # Console is an explicit dry-run, not an acknowledged real delivery.


def booking_link(record):
    params = {
        "propertyCode": record.hotel_id,
        "fromDate": record.checkin.strftime("%m/%d/%Y"),
        "toDate": record.checkout.strftime("%m/%d/%Y"),
        "numberOfRooms": record.rooms,
        "numberOfGuests": record.adults,
        "isSearch": "true",
    }
    return f"https://{record.source_site}/reservation/availabilitySearch.mi?{urlencode(params)}"


def cancel_text(record, now):
    status = record.free_cancel_now(now)
    if status is True:
        return f"免费取消截止：{record.cancellation_policy.deadline_at.isoformat()}"
    if status is False:
        return "当前不可免费取消（明确不可退款或已过截止时间）"
    return "是否免费取消未知，请核对官网完整条款"


def format_alert(rows, settings, now):
    rows = sorted(rows, key=lambda r: Decimal(json.loads(r["confirmed_payload"])["amount_including_tax"]))
    first = PriceRecord.from_dict(json.loads(rows[0]["confirmed_payload"]))
    title = f"酒店降价已复核：{settings.hotel_name(first.hotel_id)}"
    lines = [f"入住：{first.checkin} → {first.checkout}；{first.rooms}间房／{first.adults}成人"]
    for row in rows[:3]:
        record = PriceRecord.from_dict(json.loads(row["confirmed_payload"]))
        nights = (record.checkout - record.checkin).days
        nightly = (record.amount_including_tax / nights).quantize(Decimal("0.01"))
        baseline = Decimal(row["baseline"])
        basis = (
            f"历史每日最低价中位数 {baseline}，下降 {(1 - record.amount_including_tax / baseline) * 100:.1f}%"
            if row["rule"] == "HISTORY"
            else f"不高于设定上限 {(baseline / nights):.2f}/晚"
        )
        lines.append(
            f"{'会员价' if record.member_rate else '公开价'}｜{record.room_name}｜{record.rate_name}\n"
            f"含税费 {nightly} {record.currency}/晚；整段 {record.amount_including_tax} {record.currency}\n"
            f"依据：{basis}\n取消：{cancel_text(record, now)}"
        )
    if len(rows) > 3:
        lines.append(f"同次确认另有 {len(rows) - 3} 条报价命中，已合并本条通知。")
    lines.append(f"官网来源：{first.source_site}；已由两次独立查询确认。请在官网核价后再预订。")
    return title, "\n\n".join(lines), booking_link(first)


class PassiveMonitor:
    def __init__(self, settings, storage, *, notifier=None, clock=utcnow):
        self.settings, self.storage, self.clock = settings, storage, clock
        self.notifier = notifier if notifier is not None else build_passive_notifier(settings.notifications)

    def ingest(self, document):
        # Reuse the exact observed source/operation checks and require sanitized input.
        if not isinstance(document, dict):
            raise ValueError("Capture must be a mapping")
        serialized = json.dumps(document, ensure_ascii=False)
        if redact(document) != document or JWT_RE.search(serialized) or EMAIL_RE.search(serialized):
            raise ValueError("Capture has not passed privacy validation")
        process_capture(
            "https://" + document.get("source_site", "") + RATES_PATH,
            json.dumps({"operationName": document.get("operation"), "variables": document.get("variables")}),
            json.dumps(document.get("response")),
        )
        records = parse_capture(document)
        if "capture_event" not in document:
            for record in records:
                self.storage.save_price_record(record)
            return {"status": "legacy_sample_only", "offers": len(records), "candidates": 0}
        event = capture_event(document["capture_event"])
        if not records:
            return {"status": "no_cash_offers", "offers": 0, "candidates": 0}
        now = utc(self.clock())
        observed = records[0].captured_at
        if observed > now + timedelta(seconds=self.settings.future_tolerance_seconds):
            raise ValueError("Future capture cannot be used as monitoring evidence")
        # Only this new event-based path promotes a verified passive response to
        # LIVE. The general adapter and all legacy sample imports remain SAMPLE.
        records = [replace(record, source_kind="LIVE") for record in records]
        today = now.astimezone(ZoneInfo(self.settings.timezone)).date()
        eligible = (
            now - observed <= timedelta(seconds=self.settings.max_capture_age_seconds)
            and 0 <= (records[0].checkin - today).days < self.settings.days
            and records[0].checkout <= today + timedelta(days=365)
            and (not self.settings.hotels or self.settings.hotel(records[0].hotel_id) is not None)
        )
        digest = sha256(json.dumps(document, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        self.storage.expire(now)
        return self.storage.ingest(event["event_id"], digest, records, self.settings, now, eligible=eligible)

    async def notify_ready(self):
        now = utc(self.clock())
        groups = defaultdict(list)
        for row in self.storage.ready_passive():
            record = PriceRecord.from_dict(json.loads(row["confirmed_payload"]))
            if record.captured_at + timedelta(seconds=self.settings.max_capture_age_seconds) < now:
                self.storage.finish_passive([row["id"]], "EXPIRED")
                continue
            # An old READY row must also respect the current watchlist and date scope.
            today = now.astimezone(ZoneInfo(self.settings.timezone)).date()
            if (
                not 0 <= (record.checkin - today).days < self.settings.days
                or (self.settings.hotels and self.settings.hotel(record.hotel_id) is None)
                or record.checkout > today + timedelta(days=365)
            ):
                self.storage.finish_passive([row["id"]], "OUT_OF_SCOPE")
                continue
            key = (
                record.source_site,
                record.hotel_id,
                record.checkin,
                record.checkout,
                record.currency,
                record.adults,
                record.rooms,
            )
            groups[key].append(row)
        result = {"sent": 0, "dry_run": 0, "unknown": 0}
        for rows in groups.values():
            if self.settings.notifications.channel == "console":
                title, body, link = format_alert(rows, self.settings, now)
                print(f"[演练，不推送] {title}\n{body}\n{link}")
                self.storage.finish_passive([r["id"] for r in rows], "DRY_RUN")
                result["dry_run"] += 1
                continue
            if not self.notifier.configured:
                continue  # Missing key does not start the cooldown. READY can expire naturally.
            reserved = self.storage.reserve_passive(rows, now, self.settings.detector.cooldown_hours)
            if not reserved:
                continue
            title, body, link = format_alert(reserved, self.settings, now)
            try:
                await self.notifier.send_message(title, body, link)
            except (NotificationUncertain, ValueError):
                self.storage.finish_delivery(reserved, False)
                result["unknown"] += 1
            else:
                self.storage.finish_delivery(reserved, True)
                result["sent"] += 1
        return result


class CaptureWatcher:
    """Portable directory scanner; the shell never needs to expand a Windows wildcard."""

    def __init__(self, monitor):
        self.monitor = monitor
        self.seen_files = {}

    async def tick(self):
        results = []
        pending = []
        directory = self.monitor.settings.captures_dir
        for path in sorted(directory.glob("*.json")):
            signature = None
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if self.seen_files.get(path) == signature:
                    continue
                if not 0 < stat.st_size <= MAX_CAPTURE_BYTES:
                    raise ValueError("Capture size is outside the supported limit")
                text = await asyncio.to_thread(path.read_text, encoding="utf-8-sig")
                document = json.loads(text)
                # Order changed files by collection time, not hotel-name-prefixed filenames.
                captured = utc(datetime.fromisoformat(document["captured_at"].replace("Z", "+00:00")))
                pending.append((captured, path, signature, document))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                if signature is not None:
                    self.seen_files[path] = signature  # Retry if/when the file changes, not every scan.
                results.append({"status": "unreadable_file"})
        for _, path, signature, document in sorted(pending, key=lambda p: (p[0], str(p[1]))):
            try:
                result = self.monitor.ingest(document)
            except (ValueError, TypeError, KeyError, AttributeError):
                result = {"status": "invalid_capture"}  # Never echo browser-supplied private content.
            self.seen_files[path] = signature
            results.append(result)
        self.monitor.storage.expire(self.monitor.clock())
        delivery = await self.monitor.notify_ready()
        return {"files": results, **delivery}
