"""Outbox retries must not deliver alerts after their monitored stay expires."""

from datetime import date, timedelta

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from test_alerts import quote, seed_history

from app.core.config import Settings
from app.models.tables import NotificationLog, PriceAlert
from app.services import jobs, notifications
from app.services.alerts import confirm_drop, detect_drops
from app.services.notifications import deliver_one


@pytest.mark.parametrize(
    "dates",
    [
        (-1, 0),
        (364, 366),
        (0, 0),
        (2, 1),
        ("not-a-date", 1),
        (None, 1),
        (0, None),
        (True, 1),
    ],
)
async def test_expired_or_invalid_stay_is_cancelled_before_telegram_request(sessions, dates):
    settings = Settings(telegram_bot_token=SecretStr("offline-only"), telegram_chat_id="123")
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        await detect_drops(db, hotel, [quote("70")], settings)
        alert = await db.scalar(select(PriceAlert))
        await confirm_drop(db, alert.id, alert.verification_job_id, [quote("70")])
        values = [
            (date.today() + timedelta(days=value)).isoformat() if type(value) is int else value
            for value in dates
        ]
        alert.payload = {**alert.payload, "check_in": values[0], "check_out": values[1]}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await deliver_one(sessions, settings, client)
        assert not await deliver_one(sessions, settings, client)
    assert requests == []
    async with sessions() as db:
        log = await db.scalar(select(NotificationLog))
        assert log.status == "CANCELLED" and log.attempts == 0 and log.sent_at is None
        assert (await db.scalar(select(PriceAlert))).confirmed is True


@pytest.mark.parametrize("offset", [0, 364])
async def test_valid_first_and_last_night_still_send(sessions, offset):
    settings = Settings(telegram_bot_token=SecretStr("offline-only"), telegram_chat_id="123")
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        await detect_drops(db, hotel, [quote("70")], settings)
        alert = await db.scalar(select(PriceAlert))
        await confirm_drop(db, alert.id, alert.verification_job_id, [quote("70")])
        check_in = date.today() + timedelta(days=offset)
        alert.payload = {
            **alert.payload,
            "check_in": check_in.isoformat(),
            "check_out": (check_in + timedelta(days=1)).isoformat(),
        }
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"ok": True}))
    ) as client:
        assert await deliver_one(sessions, settings, client)
    async with sessions() as db:
        assert (await db.scalar(select(NotificationLog))).status == "SENT"


async def test_failed_delivery_recovered_after_checkin_does_not_resend(sessions, monkeypatch):
    settings = Settings(telegram_bot_token=SecretStr("offline-only"), telegram_chat_id="123")
    async with sessions() as db, db.begin():
        hotel = await seed_history(db)
        await detect_drops(db, hotel, [quote("70")], settings)
        alert = await db.scalar(select(PriceAlert))
        await confirm_drop(db, alert.id, alert.verification_job_id, [quote("70")])
        original_payload = dict(alert.payload)
    calls = []

    def limited(request):
        calls.append(request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 120}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        assert await deliver_one(sessions, settings, client)

    class LaterDate(date):
        @classmethod
        def today(cls):
            return date.fromisoformat(original_payload["check_out"])

    later_time = notifications.utcnow() + timedelta(minutes=30)
    monkeypatch.setattr(jobs, "date", LaterDate)
    monkeypatch.setattr(notifications, "utcnow", lambda: later_time)
    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
        assert await deliver_one(sessions, settings, client)
    assert len(calls) == 1
    async with sessions() as db:
        log = await db.scalar(select(NotificationLog))
        assert log.status == "CANCELLED" and log.attempts == 1 and log.next_retry_at is None
        assert (await db.scalar(select(PriceAlert))).payload == original_payload
