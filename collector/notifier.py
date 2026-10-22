"""Bark JSON POST. Device key is in the body, never in a logged URL."""

import logging
import os
import re
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from models import Quote

BARK_PUSH_PATH = "/push"


class _TelegramLogRedaction(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        sanitized = re.sub(
            r"https://api\.telegram\.org/bot[^/\s]+", "https://api.telegram.org/bot[REDACTED]", message
        )
        if sanitized != message:
            record.msg, record.args = sanitized, ()
        return True


# HTTPX logs full request URLs at INFO, including Telegram's URL-path token.
logging.getLogger("httpx").addFilter(_TelegramLogRedaction())


class NotificationUncertain(Exception):
    pass


def notification_body(quote: Quote, baseline: Decimal):
    drop = (1 - quote.amount / baseline) * 100
    name = quote.rate_name or quote.rate_type
    code = f" [{quote.rate_code}]" if quote.rate_code else ""
    reference = (
        f"\n含税展示参考：{quote.after_tax_display} {quote.currency}（非结算保证）"
        if quote.after_tax_display
        else ""
    )
    conditions = (
        f"\n预付要求：{'是' if quote.deposit_required else '否'}；不可退款：{'是' if quote.non_refundable else '否'}"
        if quote.deposit_required is not None and quote.non_refundable is not None
        else ""
    )
    return (
        f"{quote.hotel.name}\n入住：{quote.stay.check_in} → {quote.stay.check_out}\n"
        f"房价：{quote.amount} {quote.currency}（{quote.tax_basis}）\n"
        f"房型／房价类型：{quote.room_type} / {name}{code}{reference}{conditions}\n"
        f"历史中位数：{baseline}，下降 {drop:.1f}%\n已二次确认；价格异常不等于酒店承认的错误价。"
    )


class BarkNotifier:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        parsed = urlsplit(settings.server)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Bark server must be an HTTPS URL without credentials/query")
        self.client = client

    @property
    def configured(self):
        return bool(self.settings.enabled and os.environ.get(self.settings.device_key_env, "").strip())

    async def send(self, quote, baseline):
        if quote.is_mock:
            raise ValueError("Mock quotes must never be sent to Bark")
        key = os.environ.get(self.settings.device_key_env, "").strip()
        if not self.settings.enabled or not key:
            raise ValueError("Bark is disabled or its device key is missing")
        await self.send_message(
            "酒店价格大幅下降（已复核）", notification_body(quote, baseline), quote.source_url
        )

    async def send_message(self, title, body, url=None):
        key = os.environ.get(self.settings.device_key_env, "").strip()
        if not self.settings.enabled or not key:
            raise ValueError("Bark is disabled or its device key is missing")
        payload = {
            "device_key": key,
            "title": title,
            "body": body,
            "group": self.settings.group,
        }
        if url:
            payload["url"] = url
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        try:
            response = await client.post(self.settings.server.rstrip("/") + BARK_PUSH_PATH, json=payload)
            if response.status_code != 200 or response.json().get("code") != 200:
                raise NotificationUncertain("Bark did not acknowledge successful delivery")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise NotificationUncertain("Bark delivery outcome is unknown") from None
        finally:
            if owned:
                await client.aclose()


class TelegramNotifier:
    """Official sendMessage; never log the token-bearing URL or raw HTTP exceptions."""

    def __init__(self, settings, *, client=None):
        self.settings, self.client = settings, client

    @property
    def configured(self):
        return bool(
            os.environ.get(self.settings.telegram_token_env, "").strip()
            and os.environ.get(self.settings.telegram_chat_id_env, "").strip()
        )

    async def send_message(self, title, body, url=None):
        if not self.configured:
            raise ValueError("Telegram token or chat ID is missing")
        token = os.environ[self.settings.telegram_token_env].strip()
        chat = os.environ[self.settings.telegram_chat_id_env].strip()
        owned = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        payload = {
            "chat_id": chat,
            "text": (f"{title}\n{body}" + (f"\n{url}" if url else ""))[:4096],
            "link_preview_options": {"is_disabled": True},
        }
        try:
            response = await client.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload)
            if response.status_code != 200 or response.json().get("ok") is not True:
                raise NotificationUncertain("Telegram did not acknowledge successful delivery")
        except (httpx.HTTPError, ValueError, AttributeError):
            raise NotificationUncertain("Telegram delivery outcome is unknown") from None
        finally:
            if owned:
                await client.aclose()
