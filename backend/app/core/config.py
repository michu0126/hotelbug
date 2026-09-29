import os
from decimal import Decimal
from functools import lru_cache

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: SecretStr = SecretStr("postgresql+psycopg://hotelbug:hotelbug@postgres:5432/hotelbug")
    redis_url: SecretStr = SecretStr("redis://redis:6379/0")
    log_level: str = "INFO"
    admin_token: SecretStr = SecretStr("")
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    job_timeout_seconds: int = Field(default=120, ge=10, le=300)
    scheduler_interval_seconds: int = Field(default=10, ge=1, le=300)
    max_retries: int = Field(default=3, ge=0, le=10)
    provider_min_interval_seconds: int = Field(default=5, ge=1, le=3600)
    provider_concurrency: int = Field(default=1, ge=1, le=4)
    hot_interval_hours: int = Field(default=3, ge=2, le=4)
    warm_interval_hours: int = Field(default=9, ge=6, le=12)
    cold_interval_hours: int = Field(default=48, ge=24, le=72)
    discovery_interval_days: int = Field(default=14, ge=7, le=30)
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""
    telegram_proxy_url: SecretStr = SecretStr("")
    alert_drop_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, lt=1)
    alert_min_history_days: int = Field(default=3, ge=2, le=90)
    alert_confirmation_seconds: int = Field(default=60, ge=5, le=3600)
    global_monitoring_enabled: bool = True
    global_jobs_per_tick: int = Field(default=20, ge=1, le=500)
    max_pending_jobs: int = Field(default=2000, ge=20, le=100000)
    browser_proxy_url: SecretStr = SecretStr("")
    browser_channel: str = ""
    bark_url: SecretStr = SecretStr("")
    webhook_url: SecretStr = SecretStr("")

    @property
    def lease_seconds(self) -> int:
        return self.job_timeout_seconds + 60

    def provider_policy(self, name: str) -> "ProviderPolicy":
        prefix = name.upper()
        return ProviderPolicy(
            enabled=os.getenv(prefix + "_ENABLED", "false"),
            concurrency=os.getenv(prefix + "_CONCURRENCY", str(self.provider_concurrency)),
            interval_seconds=os.getenv(
                prefix + "_RATE_LIMIT_SECONDS", str(self.provider_min_interval_seconds)
            ),
        )


class ProviderPolicy(BaseModel):
    enabled: bool = True
    concurrency: int = Field(default=1, ge=1, le=4)
    interval_seconds: int = Field(default=5, ge=1, le=3600)


@lru_cache
def get_settings() -> Settings:
    return Settings()
