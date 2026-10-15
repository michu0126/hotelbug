"""The user can inspect daily hints without giving them room-offer semantics."""

from datetime import timedelta

from sqlalchemy import func, select
from test_calendar_hint_coverage import NOW, TODAY, hint, hotel

from app.models.tables import AppSetting, PriceHistory, StayScan
from app.providers.marriott_catalog import ROOT as EN_ROOT
from app.providers.marriott_cn_catalog import ROOT
from app.services import calendar_hint_views as subject
from app.services.calendar import get_calendar


async def test_calendar_shows_latest_hint_and_keeps_formal_availability_unknown(sessions, monkeypatch):
    monkeypatch.setattr(subject, "utcnow", lambda: NOW)
    async with sessions() as db, db.begin():
        item = hotel()
        db.add(item)
        db.add(AppSetting(key="catalog:marriott", value={"root": ROOT}))
        db.add_all([hint(age=2, displayed_price=900), hint(age=1, displayed_price=120, currency="EUR")])
        await db.flush()
        calendar = await get_calendar(db, item, TODAY.strftime("%Y-%m"))
        day = next(day for day in calendar["days"] if day["date"] == TODAY.isoformat())
        assert day["status"] == "NO_DATA" and "current_low" not in day
        assert day["calendar_hint"]["displayed_price"] == "120.0000"
        assert day["calendar_hint"]["currency"] == "EUR"
        assert day["calendar_hint"]["captured_at"].endswith("+00:00")
        assert await db.scalar(select(func.count()).select_from(PriceHistory)) == 0
        assert await db.scalar(select(func.count()).select_from(StayScan)) == 0


async def test_hint_does_not_override_verified_no_inventory_and_source_switch_hides_it(sessions, monkeypatch):
    monkeypatch.setattr(subject, "utcnow", lambda: NOW)
    async with sessions() as db, db.begin():
        item = hotel()
        db.add(item)
        source = AppSetting(key="catalog:marriott", value={"root": ROOT})
        db.add(source)
        db.add(hint())
        db.add(
            StayScan(
                hotel_id=item.id,
                check_in=TODAY,
                check_out=TODAY + timedelta(days=1),
                adults=2,
                rooms=1,
                status="UNAVAILABLE",
                observed_at=NOW,
                job_id="offline",
            )
        )
        await db.flush()
        day = (await get_calendar(db, item, TODAY.strftime("%Y-%m")))["days"][TODAY.day - 1]
        assert day["status"] == "UNAVAILABLE" and "calendar_hint" in day
        source.value = {"root": EN_ROOT}
        await db.flush()
        changed = (await get_calendar(db, item, TODAY.strftime("%Y-%m")))["days"][TODAY.day - 1]
        assert changed["status"] == "UNAVAILABLE" and "calendar_hint" not in changed


async def test_view_clips_year_and_excludes_future_captures_or_wrong_conditions(sessions, monkeypatch):
    monkeypatch.setattr(subject, "utcnow", lambda: NOW)
    async with sessions() as db, db.begin():
        item = hotel()
        db.add(item)
        db.add(AppSetting(key="catalog:marriott", value={"root": ROOT}))
        db.add_all(
            [
                hint(-1),
                hint(0),
                hint(1, age=-1),
                hint(2, adults=1),
                hint(364),
                hint(365),
                hint(363, source_url="https://www.marriott.com/"),
            ]
        )
        await db.flush()
        current = await subject.hotel_hint_days(db, item, TODAY.replace(day=1), TODAY + timedelta(days=365))
        assert set(current) == {TODAY.isoformat(), (TODAY + timedelta(days=364)).isoformat()}
