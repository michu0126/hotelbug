from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Hotel(Base):
    __tablename__ = "hotels"
    __table_args__ = (UniqueConstraint("provider", "provider_hotel_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_hotel_id: Mapped[str] = mapped_column(String(128))
    hotel_name: Mapped[str] = mapped_column(String(300))
    brand: Mapped[str | None] = mapped_column(String(100))
    country: Mapped[str | None] = mapped_column(String(100))
    region: Mapped[str | None] = mapped_column(String(100))
    city: Mapped[str | None] = mapped_column(String(100), index=True)
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    default_currency: Mapped[str | None] = mapped_column(String(3))
    official_url: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class RateFields:
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), index=True)
    provider: Mapped[str] = mapped_column(String(32))
    offer_key: Mapped[str] = mapped_column(String(64), index=True)
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    room_type: Mapped[str | None] = mapped_column(String(300))
    room_code: Mapped[str | None] = mapped_column(String(128))
    rate_name: Mapped[str | None] = mapped_column(String(300))
    rate_code: Mapped[str | None] = mapped_column(String(128))
    cash_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    total_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    currency: Mapped[str] = mapped_column(String(3))
    points_price: Mapped[int | None] = mapped_column(Integer)
    refundable: Mapped[bool | None] = mapped_column(Boolean)
    breakfast_included: Mapped[bool | None] = mapped_column(Boolean)
    member_rate: Mapped[bool | None] = mapped_column(Boolean)
    availability: Mapped[bool] = mapped_column(Boolean)
    adults: Mapped[int] = mapped_column(Integer)
    rooms: Mapped[int] = mapped_column(Integer)
    price_basis: Mapped[str] = mapped_column(String(32))
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    source_url: Mapped[str] = mapped_column(Text)
    raw_response_hash: Mapped[str] = mapped_column(String(64))


class Rate(RateFields, Base):
    __tablename__ = "rates"
    __table_args__ = (UniqueConstraint("offer_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)


class PriceHistory(RateFields, Base):
    __tablename__ = "price_history"
    __table_args__ = (
        UniqueConstraint("job_id", "offer_key"),
        Index("ix_history_hotel_date_time", "hotel_id", "check_in", "captured_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(String(36))


class PriceStatistics(Base):
    __tablename__ = "price_statistics"
    offer_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), index=True)
    date: Mapped[date] = mapped_column(Date)
    median_7d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    median_30d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    median_90d: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    historical_low: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    historical_high: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    local_date_median: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    neighbor_date_median: Mapped[Decimal | None] = mapped_column(Numeric(18, 4))
    features: Mapped[dict] = mapped_column(JSON, default=dict)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PriceAlert(Base):
    __tablename__ = "price_alerts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"), index=True)
    offer_key: Mapped[str] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(40))
    anomaly_score: Mapped[int] = mapped_column(Integer)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    verification_job_id: Mapped[str | None] = mapped_column(String(36))
    dedupe_key: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Watchlist(Base):
    __tablename__ = "watchlists"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(32))
    filters: Mapped[dict] = mapped_column(JSON, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    __table_args__ = (
        Index("ix_job_due", "status", "scheduled_at"),
        Index("ix_job_stay_attempt", "hotel_id", "check_in", "check_out", "kind", "created_at"),
        Index(
            "uq_live_job",
            "dedupe_key",
            unique=True,
            postgresql_where=text("status IN ('PENDING','QUEUED','RUNNING')"),
            sqlite_where=text("status IN ('PENDING','QUEUED','RUNNING')"),
        ),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(40))
    hotel_id: Mapped[str | None] = mapped_column(ForeignKey("hotels.id"))
    check_in: Mapped[date | None] = mapped_column(Date)
    check_out: Mapped[date | None] = mapped_column(Date)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    dedupe_key: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    error_type: Mapped[str | None] = mapped_column(String(40))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class StayScan(Base):
    __tablename__ = "stay_scans"
    __table_args__ = (
        UniqueConstraint("hotel_id", "check_in", "check_out", "adults", "rooms"),
        Index("ix_stay_scans_hotel_date", "hotel_id", "check_in"),
        Index("ix_stay_scans_recheck_cursor", "observed_at", "id"),
        Index("ix_stay_scans_window", "check_in", "observed_at"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    hotel_id: Mapped[str] = mapped_column(ForeignKey("hotels.id"))
    check_in: Mapped[date] = mapped_column(Date)
    check_out: Mapped[date] = mapped_column(Date)
    adults: Mapped[int] = mapped_column(Integer)
    rooms: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    job_id: Mapped[str] = mapped_column(String(36))


class ProviderStatus(Base):
    __tablename__ = "provider_status"
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="BROKEN")
    last_success: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    metrics_date: Mapped[date] = mapped_column(Date, default=lambda: utcnow().date())
    requests_today: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    average_latency: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=0)


class NotificationLog(Base):
    __tablename__ = "notification_logs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    alert_id: Mapped[str] = mapped_column(ForeignKey("price_alerts.id"))
    channel: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    dedupe_key: Mapped[str] = mapped_column(String(100), unique=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AppSetting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
