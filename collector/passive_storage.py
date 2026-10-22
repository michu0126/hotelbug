"""Atomic passive ingestion and durable confirmation/delivery states.

These tables do not populate the older automatic runner's quotes or candidates.
Money is always Decimal encoded as TEXT, including the daily minima.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from uuid import uuid4

from models import utc
from storage import Storage


class PassiveStorage(Storage):
    def __init__(self, path):
        super().__init__(path)
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS passive_events (
                id TEXT PRIMARY KEY, digest TEXT NOT NULL, observed_at TEXT NOT NULL,
                result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passive_observations (
                event_id TEXT NOT NULL, series_key TEXT NOT NULL, amount TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY(event_id, series_key)
            );
            CREATE TABLE IF NOT EXISTS passive_daily_min (
                series_key TEXT NOT NULL, day TEXT NOT NULL, amount TEXT NOT NULL,
                PRIMARY KEY(series_key, day)
            );
            CREATE INDEX IF NOT EXISTS passive_series_time
                ON passive_observations(series_key, observed_at);
            CREATE TABLE IF NOT EXISTS passive_alerts (
                id TEXT PRIMARY KEY, series_key TEXT NOT NULL, rule TEXT NOT NULL,
                first_event_id TEXT NOT NULL, first_at TEXT NOT NULL, first_payload TEXT NOT NULL,
                baseline TEXT NOT NULL, threshold TEXT NOT NULL, confirm_after TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL, confirmed_payload TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS passive_one_open_alert
                ON passive_alerts(series_key) WHERE status IN ('PENDING','READY');
            CREATE TABLE IF NOT EXISTS passive_deliveries (
                series_key TEXT PRIMARY KEY, alert_id TEXT NOT NULL, status TEXT NOT NULL,
                blocked_until TEXT NOT NULL
            );
        """)

    def expire(self, now):
        with self.db:
            self.db.execute(
                "UPDATE passive_alerts SET status='EXPIRED' WHERE status='PENDING' AND expires_at<=?",
                (utc(now).isoformat(),),
            )

    def _history(self, record, policy):
        day = record.captured_at.date()
        rows = self.db.execute(
            "SELECT amount FROM passive_daily_min WHERE series_key=? AND day>=? AND day<?",
            (record.series_key, (day - timedelta(days=policy.history_days)).isoformat(), day.isoformat()),
        )
        return [Decimal(row[0]) for row in rows]

    def _trigger(self, record, settings):
        history = self._history(record, settings.detector)
        if len(history) >= settings.detector.min_history_samples:
            baseline = median(history)
            threshold = baseline * (Decimal(1) - settings.detector.drop_fraction)
            if record.amount_including_tax <= threshold:
                return "HISTORY", baseline, threshold
        hotel = settings.hotel(record.hotel_id)
        if hotel and hotel.max_total_per_night is not None and hotel.currency == record.currency:
            cap = hotel.max_total_per_night * (record.checkout - record.checkin).days
            if record.amount_including_tax <= cap:
                return "ABSOLUTE", cap, cap
        return None

    def _blocked(self, series_key, now):
        row = self.db.execute(
            "SELECT blocked_until FROM passive_deliveries WHERE series_key=?", (series_key,)
        ).fetchone()
        return bool(row and datetime.fromisoformat(row[0]) > now)

    def ingest(self, event_id, digest, records, settings, now, *, eligible):
        """Record, daily history, candidate transitions and event dedup commit together."""
        result = {
            "status": "ok" if eligible else "history_only",
            "offers": len(records),
            "candidates": 0,
            "confirmed": 0,
            "rejected": 0,
        }
        with self.db:
            prior = self.db.execute("SELECT digest FROM passive_events WHERE id=?", (event_id,)).fetchone()
            if prior:
                return {"status": "duplicate" if prior[0] == digest else "event_conflict", "offers": 0}
            for record in records:
                key, obs = record.series_key, record.captured_at
                payload = json.dumps(record.to_dict(), ensure_ascii=False)
                latest = self.db.execute(
                    "SELECT observed_at FROM passive_observations WHERE series_key=? ORDER BY observed_at DESC LIMIT 1",
                    (key,),
                ).fetchone()
                self.db.execute(
                    "INSERT INTO passive_observations VALUES (?,?,?,?,?)",
                    (event_id, key, str(record.amount_including_tax), obs.isoformat(), payload),
                )
                # Delayed files enrich history, but cannot reverse a more recent recovery.
                if eligible and (latest is None or obs >= datetime.fromisoformat(latest[0])):
                    pending = self.db.execute(
                        "SELECT * FROM passive_alerts WHERE series_key=? AND status IN ('PENDING','READY')",
                        (key,),
                    ).fetchone()
                    if pending and pending["status"] == "READY":
                        confirmed = json.loads(pending["confirmed_payload"])
                        if obs >= datetime.fromisoformat(confirmed["captured_at"]):
                            if record.amount_including_tax > Decimal(pending["threshold"]):
                                self.db.execute(
                                    "UPDATE passive_alerts SET status='REJECTED' WHERE id=?", (pending["id"],)
                                )
                                result["rejected"] += 1
                            else:
                                self.db.execute(
                                    "UPDATE passive_alerts SET confirmed_payload=? WHERE id=?",
                                    (payload, pending["id"]),
                                )
                    if pending and pending["status"] == "PENDING":
                        first = datetime.fromisoformat(pending["first_at"])
                        if obs > first and event_id != pending["first_event_id"]:
                            if obs > datetime.fromisoformat(pending["expires_at"]):
                                self.db.execute(
                                    "UPDATE passive_alerts SET status='EXPIRED' WHERE id=?", (pending["id"],)
                                )
                                pending = None
                            elif record.amount_including_tax > Decimal(pending["threshold"]):
                                self.db.execute(
                                    "UPDATE passive_alerts SET status='REJECTED' WHERE id=?", (pending["id"],)
                                )
                                result["rejected"] += 1
                            elif obs >= datetime.fromisoformat(pending["confirm_after"]):
                                self.db.execute(
                                    "UPDATE passive_alerts SET status='READY',confirmed_payload=? WHERE id=?",
                                    (payload, pending["id"]),
                                )
                                result["confirmed"] += 1
                    if pending is None and not self._blocked(key, now):
                        trigger = self._trigger(record, settings)
                        if trigger:
                            rule, baseline, threshold = trigger
                            self.db.execute(
                                "INSERT INTO passive_alerts VALUES (?,?,?,?,?,?,?,?,?,?,?,NULL)",
                                (
                                    str(uuid4()),
                                    key,
                                    rule,
                                    event_id,
                                    obs.isoformat(),
                                    payload,
                                    str(baseline),
                                    str(threshold),
                                    (
                                        obs + timedelta(seconds=settings.detector.confirmation_delay_seconds)
                                    ).isoformat(),
                                    (
                                        obs
                                        + timedelta(seconds=settings.detector.confirmation_max_age_seconds)
                                    ).isoformat(),
                                    "PENDING",
                                ),
                            )
                            result["candidates"] += 1
                # A collection day contributes just one minimum, not one vote per refresh.
                day = obs.date().isoformat()
                old = self.db.execute(
                    "SELECT amount FROM passive_daily_min WHERE series_key=? AND day=?", (key, day)
                ).fetchone()
                if old is None or record.amount_including_tax < Decimal(old[0]):
                    self.db.execute(
                        "INSERT OR REPLACE INTO passive_daily_min VALUES (?,?,?)",
                        (key, day, str(record.amount_including_tax)),
                    )
            self.db.execute(
                "INSERT INTO passive_events VALUES (?,?,?,?)",
                (event_id, digest, records[0].captured_at.isoformat(), json.dumps(result)),
            )
        return result

    def ready_passive(self):
        return self.db.execute(
            "SELECT * FROM passive_alerts WHERE status='READY' ORDER BY first_at"
        ).fetchall()

    def finish_passive(self, ids, status):
        with self.db:
            self.db.executemany("UPDATE passive_alerts SET status=? WHERE id=?", [(status, i) for i in ids])

    def reserve_passive(self, candidates, now, cooldown_hours):
        """Reserve every offer in a grouped message before making the one network call."""
        accepted = []
        try:
            self.db.execute("BEGIN IMMEDIATE")
            for row in candidates:
                if self._blocked(row["series_key"], now):
                    self.db.execute("UPDATE passive_alerts SET status='COOLDOWN' WHERE id=?", (row["id"],))
                    continue
                self.db.execute(
                    "INSERT OR REPLACE INTO passive_deliveries VALUES (?,?,?,?)",
                    (
                        row["series_key"],
                        row["id"],
                        "SENDING",
                        (now + timedelta(hours=cooldown_hours)).isoformat(),
                    ),
                )
                # A process crash after this point cannot automatically resend the message.
                self.db.execute("UPDATE passive_alerts SET status='UNKNOWN' WHERE id=?", (row["id"],))
                accepted.append(row)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return accepted

    def finish_delivery(self, candidates, success):
        status = "SENT" if success else "UNKNOWN"
        with self.db:
            for row in candidates:
                self.db.execute(
                    "UPDATE passive_deliveries SET status=? WHERE series_key=?", (status, row["series_key"])
                )
                self.db.execute("UPDATE passive_alerts SET status=? WHERE id=?", (status, row["id"]))

    def report(self):
        return {
            "events": self.db.execute("SELECT COUNT(*) FROM passive_events").fetchone()[0],
            "observations": self.db.execute("SELECT COUNT(*) FROM passive_observations").fetchone()[0],
            "legacy_samples": self.db.execute("SELECT COUNT(*) FROM price_records").fetchone()[0],
            "alerts": dict(self.db.execute("SELECT status,COUNT(*) FROM passive_alerts GROUP BY status")),
        }
