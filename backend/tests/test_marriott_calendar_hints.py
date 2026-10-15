"""Sanitized public calendar structures and values observed on 2026-10-14.

The 2026-10-15 diagnostic confirms the normal guest update removes the hotel
query parameter and the calendar has no rewards input. Cash is explicit in
the one-night ISO-currency total; the visible hotel heading/link fixes identity.
"""

import calendar
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.errors import ErrorCode, ProviderError
from app.providers import marriott_calendar_hints as hints
from app.schemas.calendar_hints import CalendarHintRequest


def request(**overrides):
    return CalendarHintRequest(
        **{
            "provider_hotel_id": "PBHMD",
            "start_date": date(2026, 10, 14),
            "end_date": date(2026, 11, 1),
            **overrides,
        }
    )


def snapshot(month=10, year=2026):
    # Public prices copied from report marriott-cn-calendar-two-months-20261014.
    october = [617, 617, 617, 712, 902, 1140, 641, 617, 617, 617, 617, 617, 617, 617, 617, 807, 617, 617]
    november = [1235] * 7 + [624 if day in (16, 19, 23, 26) else 617 for day in range(8, 31)]
    amounts = [None] * 13 + october if month == 10 else november
    cells = []
    for index in range(calendar.monthrange(year, month)[1]):
        day = date(year, month, index + 1)
        amount = amounts[index] if index < len(amounts) else 617
        cells.append(
            {
                "label": f"{hints.WEEKDAYS[day.weekday()]} {list(hints.MONTHS)[month - 1]} {day.day:02} {year}",
                "disabled": amount is None,
                "outside": False,
                "price_text": f"${amount:,}" if amount is not None else "",
            }
        )
    cells.append({"label": "Tue Dec 01 2026", "outside": True, "disabled": False, "price_text": "$546"})
    return {
        "buttons": [
            # This stayed October even after the actual grid moved to November.
            {"text": "日期 (1 晚)\n2026年 10月", "label": ""},
            {"text": "1 间客房, 2 宾客", "label": ""},
            {"text": "更新搜索", "label": ""},
            {"text": "常规优惠房价", "label": ""},
            {"text": "客房费用总额\n1晚客房总费用 (USD)", "label": ""},
            {"text": "", "label": "Previous Month", "disabled": month == 10},
            {"text": "", "label": "Next Month", "disabled": False},
        ],
        "cells": cells,
        "rewards_selected": False,
        "hotels": [{"name": "Le Méridien Thimphu", "href": "/hotels/travel/pbhmd-le-meridien-thimphu/"}],
        "busy": False,
    }


@pytest.mark.parametrize("month,count", [(10, 18), (11, 30)])
def test_observed_months_parse_cash_hints_but_not_room_quotes(month, count):
    result = hints.parse_month(snapshot(month), request())
    assert result.first_day == date(2026, month, 1)
    assert result.currency == "USD"
    assert len(result.days) == count
    assert all(not hasattr(day, "total_price") and not hasattr(day, "availability") for day in result.days)
    assert all(day.check_in.month == month for day in result.days)
    assert result.days[0].displayed_price == (Decimal("617") if month == 10 else Decimal("1235"))


@pytest.mark.parametrize("selected", [True, "false"])
def test_rewards_or_unknown_rewards_state_is_not_cash_evidence(selected):
    data = snapshot()
    data["rewards_selected"] = selected
    with pytest.raises(ProviderError, match="rewards state"):
        hints.parse_month(data, request())


def test_observed_absent_rewards_control_uses_explicit_iso_cash_total():
    data = snapshot()
    data["rewards_selected"] = None
    assert hints.parse_month(data, request()).currency == "USD"
    data["buttons"][4]["text"] = "积分总额 50,000"
    with pytest.raises(ProviderError, match="explicit total currency"):
        hints.parse_month(data, request())


@pytest.mark.parametrize("hotels", [[], [{"name": "Wrong", "href": "/hotels/travel/pbhpr-other/"}]])
def test_missing_or_changed_rendered_hotel_cannot_reuse_previous_prices(hotels):
    data = snapshot()
    data["hotels"] = hotels
    with pytest.raises(ProviderError, match="rendered hotel"):
        hints.parse_month(data, request())


async def test_normal_guest_update_can_remove_property_parameter_but_source_retains_identity(monkeypatch):
    provider, page = browser()
    original = page.url

    async def update_guests(*_):
        page.url = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&isSearch=true&currency=default"

    monkeypatch.setattr(hints, "set_adults", update_guests)
    monkeypatch.setattr(
        hints,
        "settled_calendar",
        AsyncMock(return_value=(hints.parse_month(snapshot(), request()), snapshot())),
    )
    result = await hints.collect_calendar_hints(provider, request())
    assert str(result.source_url) == original
    hints.verify_calendar_url(page.url, "PBHMD", allow_missing_code=True)
    with pytest.raises(ProviderError):
        hints.verify_calendar_url(page.url, "PBHMD")


async def test_entry_currency_rewrite_keeps_the_observed_hotel_specific_entry_link(monkeypatch):
    provider, page = browser()
    original = page.url
    page.url += "default"
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    monkeypatch.setattr(
        hints,
        "settled_calendar",
        AsyncMock(return_value=(hints.parse_month(snapshot(), request()), snapshot())),
    )
    result = await hints.collect_calendar_hints(provider, request())
    assert str(result.source_url) == original


@pytest.mark.parametrize(
    "index,replacement,error",
    [
        (0, "日期 (2 晚)\n2026年 10月", "one-night"),
        (1, "1 间客房, 1 宾客", "occupancy"),
        (3, "会员专属房价", "regular-rate"),
        (4, "客房费用总额\n1晚客房总费用 ($)", "explicit total currency"),
    ],
)
def test_ambiguous_or_mismatched_conditions_are_rejected(index, replacement, error):
    data = snapshot()
    data["buttons"][index]["text"] = replacement
    with pytest.raises(ProviderError, match=error):
        hints.parse_month(data, request())


@pytest.mark.parametrize("amount", ["$0", "$-2", "$1,23", "$12-18", "$NaN", "617 points", "CNY617", ""])
def test_ambiguous_nonpositive_or_conflicting_amount_is_not_a_hint(amount):
    data = snapshot()
    data["cells"][13]["price_text"] = amount
    with pytest.raises(ProviderError):
        hints.parse_month(data, request())


def test_usd_comes_from_explicit_total_label_not_dollar_guessing():
    data = snapshot()
    data["buttons"][4]["text"] = "客房费用总额\n1晚客房总费用 (CAD)"
    assert hints.parse_month(data, request()).currency == "CAD"
    data["buttons"][4]["text"] = "$ 每晚"
    with pytest.raises(ProviderError, match="explicit total currency"):
        hints.parse_month(data, request())


def test_disabled_cells_are_omitted_not_marked_unavailable():
    data = snapshot()
    data["cells"][13].update(disabled=True, price_text="$617")
    result = hints.parse_month(data, request())
    assert len(result.days) == 17
    assert result.days[0].check_in == date(2026, 10, 15)


@pytest.mark.parametrize(
    "mutation", ["missing", "duplicate", "different_month", "weekday", "empty", "all_disabled"]
)
def test_incomplete_month_and_unconfirmed_empty_calendar_are_not_success(mutation):
    data = snapshot()
    if mutation == "missing":
        data["cells"].pop(0)
    elif mutation == "duplicate":
        data["cells"].append(deepcopy(data["cells"][13]))
    elif mutation == "different_month":
        data["cells"][-1]["outside"] = False
    elif mutation == "weekday":
        data["cells"][0]["label"] = "Fri Oct 01 2026"
    elif mutation == "empty":
        data["cells"] = []
    else:
        for cell in data["cells"]:
            cell["disabled"] = True
    with pytest.raises(ProviderError):
        hints.parse_month(data, request())


def browser():
    page = Mock()
    page.url = "https://www.marriott.com.cn/search/availabilityCalendar.mi?isRateCalendar=true&propertyCode=PBHMD&isSearch=true&currency="
    page.get_by_role.return_value.first.wait_for = AsyncMock()
    page.get_by_role.return_value.is_enabled = AsyncMock(return_value=True)
    page.get_by_role.return_value.click = AsyncMock()
    provider = SimpleNamespace(
        _root=hints.ORIGIN,
        _get_page=AsyncMock(return_value=page),
        _open_hotel=AsyncMock(return_value=page),
        _check_access=AsyncMock(),
    )
    return provider, page


async def test_collection_walks_normal_month_controls_and_clips_range(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2026, 11, 1), end_date=date(2026, 12, 1))
    captures = [(hints.parse_month(snapshot(month), req), snapshot(month)) for month in (10, 11)]
    settled = AsyncMock(side_effect=captures)
    monkeypatch.setattr(hints, "settled_calendar", settled)
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    result = await hints.collect_calendar_hints(provider, req)
    assert result.currency == "USD"
    assert result.basis == "CALENDAR_MINIMUM_TAX_UNCONFIRMED"
    assert len(result.days) == 30
    assert result.days[0].check_in == date(2026, 11, 1)
    assert result.days[-1].check_in == date(2026, 11, 30)
    provider._open_hotel.assert_awaited_once_with("PBHMD")
    page.get_by_role.assert_any_call("button", name="Next Month", exact=True)
    page.get_by_role.return_value.click.assert_awaited_once()
    assert settled.await_args_list[1].kwargs == {"expected_month": date(2026, 11, 1)}
    assert page.on.call_count == page.remove_listener.call_count == 4
    assert result.captured_at.tzinfo is not None
    assert len(result.raw_response_hash) == 64


async def test_all_disabled_requested_range_does_not_look_successful(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2026, 10, 1), end_date=date(2026, 10, 10))
    monkeypatch.setattr(
        hints, "settled_calendar", AsyncMock(return_value=(hints.parse_month(snapshot(), req), snapshot()))
    )
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    with pytest.raises(ProviderError, match="range has no cash-price hints"):
        await hints.collect_calendar_hints(provider, req)
    assert page.remove_listener.call_count == 4


async def test_single_month_window_clips_both_bounds_without_opening_rooms(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2026, 10, 20), end_date=date(2026, 10, 25))
    monkeypatch.setattr(
        hints, "settled_calendar", AsyncMock(return_value=(hints.parse_month(snapshot(), req), snapshot()))
    )
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    result = await hints.collect_calendar_hints(provider, req)
    assert [day.check_in.day for day in result.days] == [20, 21, 22, 23, 24]
    page.get_by_role.return_value.click.assert_not_awaited()


async def test_currency_change_while_navigating_is_not_combined_into_one_series(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2026, 11, 1), end_date=date(2026, 12, 1))
    following = snapshot(11)
    following["buttons"][4]["text"] = "客房费用总额\n1晚客房总费用 (CAD)"
    captures = [(hints.parse_month(data, req), data) for data in (snapshot(), following)]
    monkeypatch.setattr(hints, "settled_calendar", AsyncMock(side_effect=captures))
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    with pytest.raises(ProviderError, match="currency changed"):
        await hints.collect_calendar_hints(provider, req)
    assert page.remove_listener.call_count == 4


@pytest.mark.parametrize("current,action", [(1, "添加 成人"), (3, "删除 成人")])
async def test_guest_control_uses_observed_buttons_and_submits_once(current, action):
    _, page = browser()
    page.get_by_role.return_value.inner_text = AsyncMock(return_value=f"1 间客房, {current} 宾客")
    page.get_by_role.return_value.wait_for = AsyncMock()
    activity = hints.PageActivity()
    activity.started = 3
    before = await hints.set_adults(page, activity, 2)
    assert before == 3
    calls = page.get_by_role.call_args_list
    assert any(getattr(call.kwargs.get("name"), "pattern", None) == action + "$" for call in calls)
    page.get_by_role.assert_any_call("button", name="更新搜索", exact=True)
    page.get_by_role.assert_any_call("button", name="1 间客房, 2 宾客", exact=True)
    assert page.get_by_role.return_value.click.await_count == 3


async def test_already_correct_guest_count_does_not_requery_calendar():
    _, page = browser()
    page.get_by_role.return_value.inner_text = AsyncMock(return_value="1 间客房, 2 宾客")
    assert await hints.set_adults(page, hints.PageActivity(), 2) is None
    page.get_by_role.return_value.click.assert_not_awaited()


async def test_closed_next_month_is_not_partial_success(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2026, 11, 1), end_date=date(2026, 12, 1))
    page.get_by_role.return_value.is_enabled.return_value = False
    monkeypatch.setattr(
        hints, "settled_calendar", AsyncMock(return_value=(hints.parse_month(snapshot(), req), snapshot()))
    )
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    with pytest.raises(ProviderError) as error:
        await hints.collect_calendar_hints(provider, req)
    assert error.value.code == ErrorCode.BOOKING_WINDOW_CLOSED
    assert page.remove_listener.call_count == 4


async def test_failure_removes_observers_without_new_session_or_retry(monkeypatch):
    provider, page = browser()
    provider._open_hotel.side_effect = ProviderError(ErrorCode.RATE_LIMITED, "HTTP 429")
    with pytest.raises(ProviderError) as error:
        await hints.collect_calendar_hints(provider, request())
    assert error.value.code == ErrorCode.RATE_LIMITED
    provider._get_page.assert_awaited_once()
    provider._open_hotel.assert_awaited_once()
    assert page.remove_listener.call_count == 4


async def test_other_source_does_not_open_any_page():
    provider, _ = browser()
    provider._root = "https://www.marriott.com"
    with pytest.raises(ProviderError) as error:
        await hints.collect_calendar_hints(provider, request())
    assert error.value.code == ErrorCode.NOT_IMPLEMENTED
    provider._get_page.assert_not_awaited()


@pytest.mark.parametrize("status", [403, 429])
def test_page_activity_records_denial_and_completion(status):
    activity = hints.PageActivity()
    req = SimpleNamespace(
        url="https://www.marriott.com.cn/mi/query/normal-page-request", resource_type="fetch"
    )
    activity.request(req)
    assert activity.started == 1 and activity.pending
    activity.response(SimpleNamespace(request=req, status=status))
    activity.finished(req)
    assert activity.rejected_status == status
    assert not activity.pending
    unrelated = SimpleNamespace(url="https://www.marriott.com.cn.evil.invalid/request", resource_type="fetch")
    activity.request(unrelated)
    assert activity.started == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://www.marriott.com.cn/search/availabilityCalendar.mi?propertyCode=PBHMD",
        "https://www.marriott.com.cn:8443/search/availabilityCalendar.mi?propertyCode=PBHMD",
        "https://www.marriott.com.cn/search/availabilityCalendar.mi?propertyCode=PBHPR",
        "https://www.marriott.com.cn/search/availabilityCalendar.mi?propertyCode=PBHMD&propertyCode=PBHPR",
        "https://www.marriott.com.cn/other?propertyCode=PBHMD",
    ],
)
def test_url_must_remain_the_same_public_hotel_calendar(url):
    with pytest.raises(ProviderError):
        hints.verify_calendar_url(url, "PBHMD")


async def test_reward_selection_stops_before_waiting_or_advancing():
    provider, page = browser()
    page.evaluate = AsyncMock(return_value={"rewards_selected": True})
    with pytest.raises(ProviderError, match="rewards calendar"):
        await hints.settled_calendar(provider, page, hints.PageActivity(), request())


async def test_settle_waits_for_replaced_cells_even_when_search_summary_is_unchanged(monkeypatch):
    provider, page = browser()
    data = [snapshot(10), snapshot(11)]
    page.evaluate = AsyncMock(side_effect=lambda script: data.pop(0) if len(data) > 1 else data[0])
    clock = [0.0]
    monkeypatch.setattr(hints.time, "monotonic", lambda: clock[0])

    async def advance(_):
        clock[0] += 0.5

    monkeypatch.setattr(hints.asyncio, "sleep", advance)
    month, captured = await hints.settled_calendar(
        provider, page, hints.PageActivity(), request(), expected_month=date(2026, 11, 1)
    )
    assert month.first_day == date(2026, 11, 1)
    assert len(month.days) == 30
    assert captured["buttons"][0]["text"].endswith("2026年 10月")
    assert clock[0] >= 2.0


async def test_unpriced_month_is_an_observed_failure_after_wait_not_empty_success(monkeypatch):
    provider, page = browser()
    data = snapshot()
    for cell in data["cells"]:
        cell["disabled"] = True
    page.evaluate = AsyncMock(return_value=data)
    clock = [0.0]
    monkeypatch.setattr(hints.time, "monotonic", lambda: clock[0])

    async def advance(_):
        clock[0] += 30

    monkeypatch.setattr(hints.asyncio, "sleep", advance)
    with pytest.raises(ProviderError) as error:
        await hints.settled_calendar(provider, page, hints.PageActivity(), request())
    assert error.value.code == ErrorCode.TIMEOUT
    assert "no confirmed cash-price hints" in str(error.value.__cause__)


def test_month_rollover_and_leap_year_cell_labels():
    assert hints.next_month(date(2026, 12, 1)) == date(2027, 1, 1)
    assert hints.cell_date("Thu Feb 29 2024") == date(2024, 2, 29)
    with pytest.raises(ProviderError):
        hints.cell_date("Sat Feb 29 2025")


async def test_starting_ahead_of_requested_month_is_not_wrong_month_success(monkeypatch):
    provider, _ = browser()
    req = request()
    monkeypatch.setattr(
        hints,
        "settled_calendar",
        AsyncMock(return_value=(hints.parse_month(snapshot(11), req), snapshot(11))),
    )
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    with pytest.raises(ProviderError, match="precedes displayed month"):
        await hints.collect_calendar_hints(provider, req)


async def test_navigation_never_walks_more_than_thirteen_forward_months(monkeypatch):
    provider, page = browser()
    req = request(start_date=date(2027, 10, 1), end_date=date(2027, 10, 2))
    initial = date(2026, 1, 1)
    months = [initial]
    for _ in range(13):
        months.append(hints.next_month(months[-1]))
    captures = [(hints.CalendarMonth(day, "USD", ()), {}) for day in months]
    monkeypatch.setattr(hints, "settled_calendar", AsyncMock(side_effect=captures))
    monkeypatch.setattr(hints, "set_adults", AsyncMock(return_value=None))
    with pytest.raises(ProviderError, match="navigation bound"):
        await hints.collect_calendar_hints(provider, req)
    assert page.get_by_role.return_value.click.await_count == 13
    assert page.remove_listener.call_count == 4
