"""The diagnostic records multi-date hints, never invents dated room offers."""

from copy import deepcopy
from datetime import timedelta

import pytest

from app.models.tables import AppSetting, Hotel, ProviderStatus, utcnow
from app.providers.marriott_cn_catalog import ROOT
from tests.probe_marriott_cn_calendar import calendar_summary, cell_date, select_audit_hotel


def snapshot():
    return {
        "buttons": [
            {"text": "日期 (1 晚)\n2026年 10月"},
            {"text": "1 间客房, 2 宾客"},
            {"text": "1晚客房总费用 (USD)", "label": ""},
        ],
        "labels": [],
        "currency_controls": [],
        "cells": [
            {"label": "Wed Sep 30 2026", "outside": True, "price_text": "$10"},
            {"label": "Thu Oct 01 2026", "disabled": True, "price_text": ""},
            {"label": "Wed Oct 14 2026", "disabled": False, "price_text": "$617"},
            {"label": "Thu Oct 15 2026", "disabled": False, "price_text": "$1,140"},
        ],
    }


def test_explicit_day_labels_and_hint_price_basis_do_not_become_room_quotes():
    result = calendar_summary(snapshot())
    assert result["calendar_month"] == "2026-10"
    assert result["priced_hint_days"] == 2
    assert [hint["check_in"] for hint in result["hints"]] == ["2026-10-14", "2026-10-15"]
    assert result["hints"][1]["displayed_amount"] == "$1,140"
    assert result["price_kind"] == "unverified_calendar_hint"
    assert result["currency_labels"] == [" 1晚客房总费用 (USD)"]
    assert all("total_price" not in hint and "currency" not in hint for hint in result["hints"])


def test_ambiguous_month_or_missing_day_identity_is_not_a_successful_snapshot():
    data = deepcopy(snapshot())
    data["cells"].append({"label": "Sun Nov 01 2026", "price_text": "$900"})
    with pytest.raises(ValueError, match="one unambiguous"):
        calendar_summary(data)
    with pytest.raises(ValueError, match="explicit public date"):
        cell_date("14")


async def test_saved_provider_pause_is_checked_before_browser_creation(sessions):
    async with sessions() as db, db.begin():
        db.add(AppSetting(key="audit:directory-only", value={"provider": "marriott"}))
        db.add(AppSetting(key="catalog:marriott", value={"root": ROOT}))
        db.add(
            ProviderStatus(provider="marriott", status="BLOCKED", blocked_until=utcnow() + timedelta(hours=1))
        )
        db.add(
            Hotel(
                provider="marriott",
                provider_hotel_id="PBHMD",
                hotel_name="Offline hotel",
                official_url="https://www.marriott.com.cn/hotels/pbhmd-test",
            )
        )
    with pytest.raises(ValueError, match="remains paused"):
        await select_audit_hotel(sessions)


async def test_unmarked_database_cannot_trigger_browser_requests(sessions):
    with pytest.raises(ValueError, match="audit marker"):
        await select_audit_hotel(sessions)
