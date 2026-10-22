"""Small synchronous SQLite transactions; no network calls hold a DB lock."""

import json
import sqlite3
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from models import PriceRecord, Quote, utc


class Storage:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS quotes (
                id TEXT PRIMARY KEY, series_key TEXT NOT NULL, amount TEXT NOT NULL,
                observed_at TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS quotes_series_time ON quotes(series_key, observed_at);
            CREATE TABLE IF NOT EXISTS candidates (
                id TEXT PRIMARY KEY, series_key TEXT NOT NULL, first_quote TEXT NOT NULL,
                baseline TEXT NOT NULL, threshold TEXT NOT NULL, confirm_after TEXT NOT NULL,
                expires_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                confirmation_quote TEXT, reason TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_pending_candidate ON candidates(series_key)
                WHERE status IN ('PENDING', 'READY');
            CREATE TABLE IF NOT EXISTS notifications (
                series_key TEXT PRIMARY KEY, candidate_id TEXT NOT NULL, status TEXT NOT NULL,
                blocked_until TEXT NOT NULL, sent_at TEXT, reason TEXT
            );
            CREATE TABLE IF NOT EXISTS brand_pauses (
                brand TEXT PRIMARY KEY, reason TEXT NOT NULL, paused_until TEXT
            );
            CREATE TABLE IF NOT EXISTS price_records (
                id TEXT PRIMARY KEY, series_key TEXT NOT NULL, brand TEXT NOT NULL,
                hotel_id TEXT NOT NULL, checkin TEXT NOT NULL, checkout TEXT NOT NULL,
                member_rate INTEGER NOT NULL CHECK(member_rate IN (0,1)), currency TEXT NOT NULL,
                amount_excluding_tax TEXT NOT NULL, amount_including_tax TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS price_records_lookup
                ON price_records(brand,hotel_id,checkin,checkout,member_rate);
        """)

    def close(self):
        self.db.close()

    def save(self, quote: Quote):
        if not isinstance(quote, Quote):
            raise TypeError("PriceRecord must use save_price_record, not automatic monitoring history")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO quotes VALUES (?,?,?,?,?)",
                (
                    quote.observation_id,
                    quote.series_key,
                    str(quote.amount),
                    quote.observed_at.isoformat(),
                    json.dumps(quote.to_dict(), ensure_ascii=False),
                ),
            )

    def save_price_record(self, record: PriceRecord):
        """Keep sample membership classes apart from live monitoring history."""
        if not isinstance(record, PriceRecord):
            raise TypeError("A PriceRecord is required")
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO price_records VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.record_id,
                    record.series_key,
                    record.brand,
                    record.hotel_id,
                    str(record.checkin),
                    str(record.checkout),
                    int(record.member_rate),
                    record.currency,
                    str(record.amount_excluding_tax),
                    str(record.amount_including_tax),
                    json.dumps(record.to_dict(), ensure_ascii=False),
                ),
            )

    def price_records(self, brand, hotel_id, checkin, checkout, *, member_rate=None):
        query = "SELECT payload FROM price_records WHERE brand=? AND hotel_id=? AND checkin=? AND checkout=?"
        parameters = [brand, hotel_id, str(checkin), str(checkout)]
        if member_rate is not None:
            if type(member_rate) is not bool:
                raise ValueError("Member-rate filter must be boolean")
            query += " AND member_rate=?"
            parameters.append(int(member_rate))
        return [
            PriceRecord.from_dict(json.loads(row[0]))
            for row in self.db.execute(query + " ORDER BY id", parameters)
        ]

    def history(self, quote: Quote, history_days: int):
        rows = self.db.execute(
            "SELECT amount FROM quotes WHERE series_key=? AND observed_at>=? AND observed_at<? AND id<>?",
            (
                quote.series_key,
                (quote.observed_at - timedelta(days=history_days)).isoformat(),
                quote.observed_at.isoformat(),
                quote.observation_id,
            ),
        )
        return [Decimal(row[0]) for row in rows]

    def candidate(self, quote, baseline, threshold, policy):
        identity = str(uuid4())
        with self.db:
            cursor = self.db.execute(
                "INSERT OR IGNORE INTO candidates(id,series_key,first_quote,baseline,threshold,confirm_after,expires_at) VALUES (?,?,?,?,?,?,?)",
                (
                    identity,
                    quote.series_key,
                    json.dumps(quote.to_dict()),
                    str(baseline),
                    str(threshold),
                    (quote.observed_at + timedelta(seconds=policy.confirmation_delay_seconds)).isoformat(),
                    (quote.observed_at + timedelta(seconds=policy.confirmation_max_age_seconds)).isoformat(),
                ),
            )
        return identity if cursor.rowcount else None

    def due(self, now):
        return self.db.execute(
            "SELECT * FROM candidates WHERE status='PENDING' AND confirm_after<=? ORDER BY confirm_after LIMIT 100",
            (utc(now).isoformat(),),
        ).fetchall()

    def ready(self):
        return self.db.execute(
            "SELECT * FROM candidates WHERE status='READY' ORDER BY confirm_after LIMIT 100"
        ).fetchall()

    def finish_candidate(self, identity, status, reason=None, confirmation=None):
        with self.db:
            self.db.execute(
                "UPDATE candidates SET status=?,reason=?,confirmation_quote=? WHERE id=?",
                (
                    status,
                    reason,
                    json.dumps(confirmation.to_dict()) if confirmation else None,
                    identity,
                ),
            )

    def pause(self, brand, reason, until=None):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO brand_pauses VALUES (?,?,?)",
                (brand, reason, utc(until).isoformat() if until else None),
            )

    def paused(self, brand, now):
        row = self.db.execute("SELECT * FROM brand_pauses WHERE brand=?", (brand,)).fetchone()
        return bool(
            row and (row["paused_until"] is None or datetime.fromisoformat(row["paused_until"]) > utc(now))
        )

    def resume(self, brand):
        with self.db:
            self.db.execute("DELETE FROM brand_pauses WHERE brand=?", (brand,))

    def reserve_notification(self, candidate, now, cooldown_hours):
        """Atomic reservation before delivery. Crash/timeouts cannot trigger an immediate resend."""
        now = utc(now)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute(
                "SELECT * FROM notifications WHERE series_key=?", (candidate["series_key"],)
            ).fetchone()
            if row and datetime.fromisoformat(row["blocked_until"]) > now:
                self.db.commit()
                return "UNKNOWN" if row["status"] in ("SENDING", "UNKNOWN") else "COOLDOWN"
            self.db.execute(
                "INSERT OR REPLACE INTO notifications VALUES (?,?,?,?,NULL,NULL)",
                (
                    candidate["series_key"],
                    candidate["id"],
                    "SENDING",
                    (now + timedelta(hours=cooldown_hours)).isoformat(),
                ),
            )
            self.db.commit()
            return "RESERVED"
        except BaseException:
            self.db.rollback()
            raise

    def notification_result(self, series_key, now, cooldown_hours, *, success):
        now = utc(now)
        with self.db:
            self.db.execute(
                "UPDATE notifications SET status=?,sent_at=?,blocked_until=?,reason=? WHERE series_key=?",
                (
                    "SENT" if success else "UNKNOWN",
                    now.isoformat() if success else None,
                    (now + timedelta(hours=cooldown_hours)).isoformat(),
                    None if success else "Delivery outcome uncertain; do not automatically resend",
                    series_key,
                ),
            )
