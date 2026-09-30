import json
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from test_domain import fixture_rate
from test_pipeline import FixtureProvider

from app.api.main import app
from app.api.main import settings as api_settings
from app.core.config import Settings
from app.core.logging import redact
from app.crawler.worker import process_one
from app.database.session import get_session
from app.models.tables import CrawlJob, NotificationLog, PriceAlert, utcnow
from app.providers.registry import FACTORIES
from app.schemas.domain import JobInput, JobKind
from app.services.alerts import confirm_drop, detect_drops
from app.services.jobs import enqueue
from app.services.notifications import deliver_one
from app.services.rates import persist_rates, upsert_hotel


def quote(price, captured=None, **changes):
    return fixture_rate().model_copy(
        update={
            "total_price": Decimal(price),
            "cash_price": None,
            "tax": None,
            "captured_at": captured or utcnow(),
            **changes,
        }
    )


async def seed_history(session, prices=("200", "210", "190")):
    hotel = await upsert_hotel(session, await FixtureProvider().get_hotel_details("fixture-only"))
    for index, price in enumerate(prices, 1):
        await persist_rates(session, hotel, [quote(price, utcnow() - timedelta(days=index))], f"old{index}")
    return hotel


async def test_worker_drop_confirmation_and_telegram_end_to_end(sessions, queue, monkeypatch):
    monkeypatch.setenv("MARRIOTT_ENABLED", "true")

    class LowPrice(FixtureProvider):
        async def search_rates(self, request):
            return [quote("70")]

    monkeypatch.setitem(FACTORIES, "marriott", LowPrice)
    settings = Settings(telegram_bot_token=SecretStr("test-token"), telegram_chat_id="123")
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        rate = quote("70")
        job = await enqueue(
            s,
            JobInput(
                provider="marriott",
                kind=JobKind.FETCH_RATE,
                hotel_id=hotel.id,
                check_in=rate.check_in,
                check_out=rate.check_out,
            ),
        )
        job_id = job.id
    await queue.put(job_id, 50)
    assert await process_one(sessions, queue, settings)
    async with sessions() as s, s.begin():
        alert = await s.scalar(select(PriceAlert))
        assert not alert.confirmed
        assert await s.scalar(select(func.count()).select_from(NotificationLog)) == 0
        verification = await s.get(CrawlJob, alert.verification_job_id)
        verification.scheduled_at = utcnow() - timedelta(seconds=1)
        verification_id = verification.id
    # Advance the confirmation delay without a wall-clock sleep in the test.
    await queue.redis.delete("hotelbug:limit:marriott")
    await queue.put(verification_id, 100)
    assert await process_one(sessions, queue, settings)
    requests = []

    def telegram(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(telegram)) as client:
        assert await deliver_one(sessions, settings, client)
        assert not await deliver_one(sessions, settings, client)
    assert len(requests) == 1
    message = requests[0]["text"]
    for field in ("Fixture hotel", "万豪 Marriott", "2027-01-07", "70", "USD", "65.0%"):
        assert field in message
    async with sessions() as s:
        assert (await s.scalar(select(PriceAlert))).confirmed
        assert (await s.scalar(select(NotificationLog))).status == "SENT"


async def test_recovered_price_does_not_notify_and_candidate_is_deduplicated(sessions):
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        assert await detect_drops(s, hotel, [quote("70")], Settings()) == 1
        assert await detect_drops(s, hotel, [quote("60")], Settings()) == 0
        alert = await s.scalar(select(PriceAlert))
        assert not await confirm_drop(s, alert.id, alert.verification_job_id, [quote("190")])
        assert alert.payload["verification"] == "REJECTED"
        assert await s.scalar(select(func.count()).select_from(NotificationLog)) == 0


async def test_baseline_needs_distinct_days_and_matching_currency(sessions):
    async with sessions() as s, s.begin():
        hotel = await seed_history(s, ("200", "200"))
        assert await detect_drops(s, hotel, [quote("30")], Settings()) == 0
        await persist_rates(s, hotel, [quote("200", utcnow() - timedelta(days=3), currency="EUR")], "eur")
        assert await detect_drops(s, hotel, [quote("30")], Settings()) == 0


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 500}}, "PENDING"),
        (401, {"ok": False, "error_code": 401}, "FAILED"),
        (200, {"ok": False, "error_code": 500}, "PENDING"),
    ],
)
async def test_telegram_failure_is_not_marked_sent(sessions, status, body, expected):
    settings = Settings(telegram_bot_token=SecretStr("test-token"), telegram_chat_id="123")
    async with sessions() as s, s.begin():
        hotel = await seed_history(s)
        await detect_drops(s, hotel, [quote("70")], settings)
        alert = await s.scalar(select(PriceAlert))
        await confirm_drop(s, alert.id, alert.verification_job_id, [quote("70")])
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=body))
    ) as client:
        assert await deliver_one(sessions, settings, client)
    async with sessions() as s:
        log = await s.scalar(select(NotificationLog))
        assert log.status == expected and log.sent_at is None and log.attempts == 1
        if status == 429:
            assert log.next_retry_at.replace(tzinfo=utcnow().tzinfo) > utcnow() + timedelta(seconds=490)


def test_telegram_token_redacted_from_url():
    assert "private-token" not in redact("POST https://api.telegram.org/botprivate-token/sendMessage")


async def test_ui_saved_telegram_config_is_used_for_delivery(sessions, monkeypatch):
    monkeypatch.setattr(api_settings, "admin_token", SecretStr("admin-test"))

    async def dependency():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_session] = dependency
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api:
            response = await api.put(
                "/api/settings/telegram",
                headers={"authorization": "Bearer admin-test"},
                json={"bot_token": "ui-test-token", "chat_id": "987", "enabled": True},
            )
            assert response.status_code == 200 and response.json()["configured"]
            saved = await api.get("/api/settings/telegram", headers={"authorization": "Bearer admin-test"})
            assert saved.json()["chat_id"] == "987" and "ui-test-token" not in saved.text
    finally:
        app.dependency_overrides.clear()

    async with sessions() as session, session.begin():
        hotel = await seed_history(session)
        await detect_drops(session, hotel, [quote("70")], Settings())
        alert = await session.scalar(select(PriceAlert))
        await confirm_drop(session, alert.id, alert.verification_job_id, [quote("70")])
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        assert await deliver_one(
            sessions, Settings(telegram_bot_token=SecretStr(""), telegram_chat_id=""), client
        )
    assert len(requests) == 1
    assert "ui-test-token" in str(requests[0].url)
    assert json.loads(requests[0].content)["chat_id"] == "987"
