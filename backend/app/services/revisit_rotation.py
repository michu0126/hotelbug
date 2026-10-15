"""Bounded provider-first keysets for calendar revisits and missing-date repair."""

from collections import deque
from datetime import datetime

from sqlalchemy import and_, or_

from app.models.tables import AppSetting, Hotel


async def fair_revisit_rows(session, query, model, *, providers, key, time_field, id_field, limit):
    """Return round-robin rows and a callback acknowledging only inspected rows.

    A fetched but unconsumed row must not move its cursor. The old shared
    keyset is inherited once per group and retained for upgrade compatibility.
    SQL state, not process memory or Redis, retains both group and date turns.
    """
    legacy = await session.get(AppSetting, key)
    turn = await session.get(AppSetting, key + ":provider")
    last = turn.value.get("provider", "") if turn and isinstance(turn.value, dict) else ""
    if not isinstance(last, str):
        last = ""
    # The registry supplies at most the enabled groups. Do not DISTINCT the
    # full correlated history query merely to discover six provider names.
    providers = sorted(set(providers))
    providers = [p for p in providers if p > last] + [p for p in providers if p <= last]
    states, candidates = {}, {}
    timestamp = getattr(model, time_field)
    for provider in providers:
        state = await session.get(AppSetting, key + ":" + provider)
        states[provider] = state
        source = state or legacy
        value = source.value if source and isinstance(source.value, dict) else {}
        raw_time = value.get(time_field)
        try:
            cursor_time = datetime.fromisoformat(raw_time) if isinstance(raw_time, str) else None
        except ValueError:
            cursor_time = None
        cursor_id = value.get(id_field, "")
        if not isinstance(cursor_id, str):
            cursor_id = ""
        after = (
            or_(timestamp > cursor_time, and_(timestamp == cursor_time, model.id > cursor_id))
            if cursor_time
            else True
        )
        ordered = query.where(Hotel.provider == provider).order_by(timestamp, model.id)
        rows = (await session.execute(ordered.where(after).limit(limit))).all()
        if not rows and cursor_time is not None:
            rows = (await session.execute(ordered.limit(limit))).all()
        candidates[provider] = deque(rows)
    active = deque(p for p in providers if candidates[p])
    chosen = []
    while active and len(chosen) < limit:
        provider = active.popleft()
        chosen.append(candidates[provider].popleft())
        if candidates[provider]:
            active.append(provider)

    def remember(provider, value):
        nonlocal legacy, turn
        state = states[provider]
        if state:
            state.value = dict(value)
        else:
            state = AppSetting(key=key + ":" + provider, value=dict(value))
            session.add(state)
            states[provider] = state
        if turn:
            turn.value = {"provider": provider}
        else:
            turn = AppSetting(key=key + ":provider", value={"provider": provider})
            session.add(turn)
        if legacy:
            legacy.value = dict(value)
        else:
            legacy = AppSetting(key=key, value=dict(value))
            session.add(legacy)

    return chosen, remember
