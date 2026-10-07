"""Database-backed configuration snapshots shared by API, Scheduler and Worker."""

from contextlib import AsyncExitStack
from decimal import Decimal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from app.core.anomaly_config import AnomalyPolicy
from app.core.config import ProviderPolicy, Settings
from app.models.tables import AppSetting, utcnow
from app.schemas.domain import ProviderName

KEY = "monitoring"


class MonitoringPolicy(ProviderPolicy):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class MonitoringInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: dict[ProviderName, MonitoringPolicy] | None = None
    global_monitoring_enabled: bool | None = None
    browser_proxy_url: str | None = Field(default=None, max_length=2048)
    telegram_proxy_url: str | None = Field(default=None, max_length=2048)
    alert_drop_fraction: Decimal | None = Field(default=None, gt=0, lt=1)
    alert_renotify_drop_fraction: Decimal | None = Field(default=None, gt=0, lt=1)
    alert_historical_lows_enabled: bool | None = None
    anomaly_policy: AnomalyPolicy | None = None
    alert_min_history_days: int | None = Field(default=None, ge=2, le=90)
    alert_confirmation_seconds: int | None = Field(default=None, ge=5, le=3600)

    @field_validator("browser_proxy_url", "telegram_proxy_url")
    @classmethod
    def proxy_url(cls, value):
        if value is None or not value.strip():
            return value if value is None else ""
        value = value.strip()
        try:
            parsed = urlsplit(value)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname
                and parsed.port is not None
                and 1 <= parsed.port <= 65535
                and parsed.path in {"", "/"}
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("Use an HTTP(S) proxy URL with an explicit port and no path")
        return value.rstrip("/")


def apply_saved(base: Settings, value: dict) -> Settings:
    saved = MonitoringInput.model_validate(value)
    updates = saved.model_dump(exclude_none=True, exclude={"providers"})
    for key in ("browser_proxy_url", "telegram_proxy_url"):
        if key in updates:
            updates[key] = SecretStr(updates[key])
    if saved.providers is not None:
        updates["provider_overrides"] = {**base.provider_overrides, **saved.providers}
    if saved.anomaly_policy is not None:
        updates["anomaly_policy"] = saved.anomaly_policy
    # Do not mutate the startup singleton: running jobs retain their own snapshot.
    return base.model_copy(update=updates)


async def load_runtime_settings(db, base: Settings) -> Settings:
    row = await db.get(AppSetting, KEY)
    return apply_saved(base, row.value) if row else base.model_copy()


async def save_runtime_settings(db, data: MonitoringInput) -> None:
    row = await db.get(AppSetting, KEY)
    value = dict(row.value) if row else {}
    incoming = data.model_dump(mode="json", exclude_none=True)
    if "providers" in incoming:
        incoming["providers"] = {**value.get("providers", {}), **incoming["providers"]}
    value.update(incoming)
    if row:
        row.value, row.updated_at = value, utcnow()
    else:
        db.add(AppSetting(key=KEY, value=value))


def proxy_display(value: SecretStr) -> str:
    raw = value.get_secret_value()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")


def public_monitoring_settings(settings: Settings, implemented: set[str]) -> dict:
    return {
        "providers": {
            name.value: {
                **settings.provider_policy(name).model_dump(),
                "enabled": settings.provider_policy(name).enabled and name in implemented,
                "implemented": name in implemented,
            }
            for name in ProviderName
        },
        "global_monitoring_enabled": settings.global_monitoring_enabled,
        "monitoring_days": 365,
        "timezone": "Asia/Shanghai",
        "browser_proxy_configured": bool(settings.browser_proxy_url.get_secret_value()),
        "browser_proxy_display": proxy_display(settings.browser_proxy_url),
        "telegram_proxy_configured": bool(settings.telegram_proxy_url.get_secret_value()),
        "telegram_proxy_display": proxy_display(settings.telegram_proxy_url),
        "alert_drop_fraction": str(settings.alert_drop_fraction),
        "alert_renotify_drop_fraction": str(settings.alert_renotify_drop_fraction),
        "alert_historical_lows_enabled": settings.alert_historical_lows_enabled,
        "anomaly_policy": settings.anomaly_policy.model_dump(mode="json"),
        "alert_min_history_days": settings.alert_min_history_days,
        "alert_confirmation_seconds": settings.alert_confirmation_seconds,
    }


class RuntimeHttpClients:
    """Reuse HTTP pools until a saved proxy changes; close replaced pools cleanly."""

    def __init__(self):
        self.stack = None
        self.key = None
        self.catalog = None
        self.telegram = None

    async def update(self, settings: Settings):
        browser = settings.browser_proxy_url.get_secret_value() or None
        telegram = settings.telegram_proxy_url.get_secret_value() or browser
        key = (browser, telegram)
        if self.stack is not None and self.key == key:
            return
        stack = AsyncExitStack()
        try:
            catalog = await stack.enter_async_context(httpx.AsyncClient(follow_redirects=True, proxy=browser))
            notification = await stack.enter_async_context(
                httpx.AsyncClient(follow_redirects=False, proxy=telegram)
            )
        except BaseException:
            await stack.aclose()
            raise
        old = self.stack
        self.stack, self.key, self.catalog, self.telegram = stack, key, catalog, notification
        if old:
            await old.aclose()

    async def close(self):
        if self.stack:
            await self.stack.aclose()
            self.stack, self.key, self.catalog, self.telegram = None, None, None, None
