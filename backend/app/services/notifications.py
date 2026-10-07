"""Transactional Telegram outbox with bounded retries and no token logging."""

from datetime import date, timedelta

import httpx
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.models.tables import AppSetting, NotificationLog, PriceAlert, utcnow
from app.services.jobs import within_monitoring_window

GROUP_NAMES = {
    "marriott": "万豪 Marriott",
    "ihg": "IHG 洲际",
    "gha": "GHA DISCOVERY",
    "accor": "雅高 Accor",
    "hilton": "希尔顿 Hilton",
    "hyatt": "凯悦 Hyatt",
}


def telegram_text(payload: dict) -> str:
    duration = "每晚" if payload.get("price_basis") == "nightly" else "整段住宿"
    basis = duration + (
        "官网展示价，全部税费未确认" if payload.get("price_field") == "cash_price" else "含税费"
    )
    title = "酒店历史新低提醒" if payload.get("event_type") == "NEW_HISTORICAL_LOW" else "酒店降价提醒"
    historical_line = (
        f"此前同报价历史最低：{payload['historical_low']} {payload['currency']}\n"
        if payload.get("historical_low")
        else ""
    )
    score_line = (
        f"异常评分：{payload['anomaly_score']} / 100 · {payload['anomaly_level']}\n"
        if payload.get("anomaly_level")
        else ""
    )
    return (
        f"{title}（已复查）\n"
        f"酒店：{payload['hotel_name']}\n"
        f"集团：{GROUP_NAMES.get(payload['provider'], payload['provider'])}\n"
        f"入住：{payload['check_in']}\n退房：{payload['check_out']}\n"
        f"价格：{payload['price']} {payload['currency']}（{basis}）\n"
        f"历史基准：{payload['baseline']} {payload['currency']}"
        f"{('（' + str(payload['baseline_window_days']) + '日每日中位数）') if payload.get('baseline_window_days') else ''}\n"
        f"降幅：{payload['drop_percent']}%\n"
        f"{historical_line}{score_line}"
        f"房型：{payload.get('room_type') or '未提供'}\n"
        f"房价方案：{payload.get('rate_name') or '未提供'}\n"
        f"官网：{payload['source_url']}"
    )[:4096]


async def deliver_one(sessions: async_sessionmaker, settings: Settings, client: httpx.AsyncClient) -> bool:
    async with sessions() as session, session.begin():
        saved = await session.get(AppSetting, "telegram")
        if saved and saved.value.get("enabled") is False:
            return False
        token = (
            saved.value.get("bot_token") if saved else None
        ) or settings.telegram_bot_token.get_secret_value()
        chat_id = (saved.value.get("chat_id") if saved else None) or settings.telegram_chat_id
        if not token or not chat_id:
            return False
        now = utcnow()
        log = await session.scalar(
            select(NotificationLog)
            .where(
                NotificationLog.channel == "telegram",
                NotificationLog.status == "PENDING",
                or_(NotificationLog.next_retry_at.is_(None), NotificationLog.next_retry_at <= now),
            )
            .order_by(NotificationLog.next_retry_at, NotificationLog.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if log is None:
            return False
        alert = await session.get(PriceAlert, log.alert_id)
        if not alert or not alert.confirmed:
            log.status = "CANCELLED"
            return True
        try:
            stay_current = within_monitoring_window(
                date.fromisoformat(alert.payload["check_in"]),
                date.fromisoformat(alert.payload["check_out"]),
            )
        except (KeyError, TypeError, ValueError):
            stay_current = False
        if not stay_current:
            # A paused bot/network may recover after the booked night passed.
            # Retain the verified historical alert, but don't send stale stays.
            log.status, log.next_retry_at = "CANCELLED", None
            return True
        log.attempts += 1
        retry_after = None
        permanent = False
        try:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": telegram_text(alert.payload),
                    "link_preview_options": {"is_disabled": True},
                },
                timeout=15,
            )
            data = response.json()
            if response.is_success and isinstance(data, dict) and data.get("ok") is True:
                log.status, log.sent_at, log.next_retry_at = "SENT", now, None
                return True
            code = (
                data.get("error_code", response.status_code)
                if isinstance(data, dict)
                else response.status_code
            )
            permanent = code in (400, 401, 403, 404)
            parameters = data.get("parameters", {}) if isinstance(data, dict) else {}
            if isinstance(parameters, dict) and type(parameters.get("retry_after")) is int:
                retry_after = max(0, parameters["retry_after"])
        except (httpx.HTTPError, ValueError):
            pass
        log.status = "FAILED" if permanent or log.attempts >= 8 else "PENDING"
        log.next_retry_at = now + timedelta(seconds=max(retry_after or 0, min(3600, 30 * 2**log.attempts)))
        return True
