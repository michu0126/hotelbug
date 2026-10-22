"""Queue tests are local SQLite only, with a fixed clock and no browser requests."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from automatic_queue import Queue, StaleLeaseError

NOW = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
TODAY = NOW.date()
HOTELS = [
    {"hotel_id": "XIYBP", "hotel_url": "https://www.marriott.com.cn/hotels/example-xiybp/overview/"},
    {"hotel_id": "XIYWH", "hotel_url": "https://www.marriott.com.cn/hotels/example-xiywh/overview/"},
]


@pytest.fixture
def queue(tmp_path):
    with Queue(tmp_path / "queue.sqlite") as result:
        yield result


def seed(queue, **kwargs):
    return queue.seed(HOTELS[:1], TODAY, now=NOW, **kwargs)


def test_full_rolling_year_and_idempotent_seed(queue):
    assert seed(queue) == 365
    task = queue.claim(NOW)
    assert task.checkin == TODAY
    queue.complete(task, NOW + timedelta(seconds=1), revisit_seconds=3600)
    assert seed(queue) == 365
    assert queue.report()["successful_queries"] == 1
    assert queue.report()["visited_tasks"] == 1
    assert queue.get(task.id)["due_at"] == (NOW + timedelta(seconds=3601)).isoformat()
    assert queue.seed(HOTELS[:1], TODAY + timedelta(days=1), now=NOW + timedelta(days=1)) == 365
    assert queue.get(task.id)["status"] == "EXPIRED"
    bounds = queue.db.execute(
        "SELECT MIN(checkin),MAX(checkout) FROM automatic_tasks WHERE active=1"
    ).fetchone()
    assert tuple(bounds) == (
        (TODAY + timedelta(days=1)).isoformat(),
        (TODAY + timedelta(days=366)).isoformat(),
    )


def test_hotel_nights_adults_scope_and_removal(queue):
    assert queue.seed(HOTELS, TODAY, days=3, nights=2, adults=2, now=NOW) == 4
    task = queue.claim(NOW)
    assert task.checkout - task.checkin == timedelta(days=2)
    assert task.adults == 2
    queue.seed([], TODAY, days=3, now=NOW)
    assert queue.report()["tasks"] == 0
    with pytest.raises(StaleLeaseError):
        queue.complete(task, NOW, revisit_seconds=3600)
    assert queue.claim(NOW) is None


def test_two_connections_cannot_claim_concurrently_and_restart_preserves_lease(tmp_path):
    path = tmp_path / "q.sqlite"
    with Queue(path) as first, Queue(path) as second:
        seed(first, days=2)
        task = first.claim(NOW, lease_seconds=10)
        assert second.claim(NOW) is None
        assert second.claim(NOW + timedelta(seconds=9)) is None
        reclaimed = second.claim(NOW + timedelta(seconds=10))
        assert reclaimed.id == task.id
        assert reclaimed.lease_token != task.lease_token
        assert reclaimed.attempts == 2
        with pytest.raises(StaleLeaseError):
            first.complete(task, NOW + timedelta(seconds=11), revisit_seconds=3600)
        second.complete(reclaimed, NOW + timedelta(seconds=11), revisit_seconds=3600)
    with Queue(path) as reopened:
        assert reopened.report()["successful_queries"] == 1
        assert reopened.claim(NOW + timedelta(seconds=12)).checkin == TODAY + timedelta(days=1)


def test_expired_lease_cannot_submit_even_before_another_claim(queue):
    seed(queue, days=1)
    task = queue.claim(NOW, lease_seconds=10)
    with pytest.raises(StaleLeaseError):
        queue.complete(task, NOW + timedelta(seconds=10), revisit_seconds=3600)
    with pytest.raises(StaleLeaseError):
        queue.renew(task, NOW + timedelta(seconds=11))


def test_renew_and_complete_revisit(queue):
    seed(queue, days=1)
    task = queue.claim(NOW, lease_seconds=10)
    queue.renew(task, NOW + timedelta(seconds=9), lease_seconds=20)
    assert queue.claim(NOW + timedelta(seconds=10)) is None
    finished = NOW + timedelta(seconds=21)
    queue.complete(task, finished, revisit_seconds=60)
    assert queue.claim(finished + timedelta(seconds=59)) is None
    again = queue.claim(finished + timedelta(seconds=60))
    assert again.id == task.id and again.attempts == 1
    assert queue.report()["attempts"] == 2


def test_pause_is_durable_and_resume_is_explicit(tmp_path):
    path = tmp_path / "q.sqlite"
    with Queue(path) as queue:
        seed(queue, days=1)
        task = queue.claim(NOW)
        queue.pause("HTTP_403", NOW + timedelta(seconds=1), task=task)
        assert queue.report()["paused"]
        with pytest.raises(StaleLeaseError):
            queue.complete(task, NOW + timedelta(seconds=2), revisit_seconds=60)
    with Queue(path) as queue:
        assert queue.claim(NOW + timedelta(hours=1)) is None
        assert queue.report()["pause_reason"] == "HTTP_403"
        queue.resume(NOW + timedelta(hours=1))
        assert queue.claim(NOW + timedelta(hours=1)).id == task.id


def test_retry_backoff_is_persisted_capped_and_eventually_failed(queue):
    seed(queue, days=1)
    current = NOW
    expected_delays = [20, 25, 25]
    for expected in expected_delays:
        task = queue.claim(current)
        due = queue.retry(
            task,
            current,
            "TIMEOUT",
            base_seconds=10,
            max_seconds=25,
            max_attempts=4,
            jitter=lambda low, high: high,
        )
        assert due == current + timedelta(seconds=expected)
        assert queue.claim(due - timedelta(microseconds=1)) is None
        current = due
    task = queue.claim(current)
    assert task.attempts == 4
    assert queue.retry(task, current, "TIMEOUT", max_attempts=4) is None
    assert queue.get(task.id)["status"] == "FAILED"
    assert queue.claim(current + timedelta(days=1)) is None


def test_unsafe_jitter_is_clamped_and_nonfinite_rolls_back(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    with pytest.raises(ValueError):
        queue.retry(task, NOW, "TIMEOUT", jitter=lambda *_: float("nan"))
    assert queue.get(task.id)["status"] == "RUNNING"
    assert queue.retry(task, NOW, "TIMEOUT", jitter=lambda *_: -10) == NOW + timedelta(seconds=30)


def test_confirmation_outranks_new_dates_but_waits_until_due(queue):
    seed(queue, days=3)
    first = queue.claim(NOW)
    queue.complete(first, NOW, revisit_seconds=3600)
    due = NOW + timedelta(seconds=180)
    assert queue.prioritize(
        first.hotel_id,
        first.checkin,
        first.checkout,
        first.adults,
        due_at=due,
        expires_at=due + timedelta(hours=1),
    )
    early = queue.claim(NOW + timedelta(seconds=1))
    assert early.checkin == TODAY + timedelta(days=1)
    queue.complete(early, NOW + timedelta(seconds=2), revisit_seconds=3600)
    confirmation = queue.claim(due)
    assert confirmation.id == first.id and confirmation.priority == 100
    queue.complete(confirmation, due + timedelta(seconds=1), revisit_seconds=3600)
    assert queue.get(first.id)["confirmation_due"] is None
    assert queue.claim(due + timedelta(seconds=2)).checkin == TODAY + timedelta(days=2)


def test_ordinary_early_search_does_not_clear_later_confirmation(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    due = NOW + timedelta(minutes=3)
    queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=due,
        expires_at=due + timedelta(hours=1),
    )
    queue.complete(task, NOW + timedelta(seconds=1), revisit_seconds=3600)
    assert queue.get(task.id)["confirmation_due"] == due.isoformat()
    assert queue.claim(due).priority == 100


def test_repeated_confirmation_hints_do_not_postpone(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    queue.complete(task, NOW, revisit_seconds=3600)
    for offset in [180, 300]:
        queue.prioritize(
            task.hotel_id,
            task.checkin,
            task.checkout,
            task.adults,
            due_at=NOW + timedelta(seconds=offset),
            expires_at=NOW + timedelta(hours=1),
        )
    assert queue.claim(NOW + timedelta(seconds=180)).id == task.id


def test_failed_confirmation_honors_retry_backoff(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    queue.complete(task, NOW, revisit_seconds=3600)
    queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(hours=1),
    )
    confirmation = queue.claim(NOW + timedelta(seconds=1))
    due = queue.retry(confirmation, NOW + timedelta(seconds=1), "TIMEOUT", jitter=lambda low, high: low)
    # The detector can repeat an earlier hint on every polling iteration.
    queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(hours=1),
    )
    assert queue.claim(NOW + timedelta(seconds=1)) is None
    assert queue.claim(due).priority == 100


def test_successful_confirmation_hint_is_consumed_but_new_hint_can_run(queue):
    seed(queue, days=2)
    task = queue.claim(NOW)
    queue.complete(task, NOW, revisit_seconds=86400)
    due = NOW + timedelta(seconds=180)
    args = (task.hotel_id, task.checkin, task.checkout, task.adults)
    assert queue.prioritize(*args, due_at=due, expires_at=due + timedelta(hours=1))
    check = queue.claim(due)
    queue.complete(check, due + timedelta(seconds=1), revisit_seconds=86400)
    assert not queue.prioritize(*args, due_at=due, expires_at=due + timedelta(hours=1))
    next_task = queue.claim(due + timedelta(seconds=2))
    assert next_task.checkin != task.checkin
    queue.complete(next_task, due + timedelta(seconds=3), revisit_seconds=86400)
    later = due + timedelta(minutes=10)
    assert queue.prioritize(*args, due_at=later, expires_at=later + timedelta(hours=1))
    failed = queue.claim(later)
    queue.retry(failed, later, "TIMEOUT")
    assert not queue.prioritize(*args, due_at=due, expires_at=due + timedelta(hours=1))
    assert queue.get(task.id)["last_success_started_at"] == due.isoformat()


def test_query_started_before_due_does_not_consume_hint(queue):
    seed(queue, days=1)
    task = queue.claim(NOW, lease_seconds=600)
    due = NOW + timedelta(seconds=180)
    queue.complete(task, due + timedelta(seconds=1), revisit_seconds=86400)
    assert queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=due,
        expires_at=due + timedelta(hours=1),
    )


def test_defer_unselectable_date_keeps_task_without_recording_success(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    queue.defer(task, NOW, seconds=3600, reason="DATE_NOT_SELECTABLE")
    assert queue.report()["successful_queries"] == 0
    assert queue.get(task.id)["status"] == "WAITING"
    assert queue.get(task.id)["attempts"] == 0
    queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=NOW,
        expires_at=NOW + timedelta(hours=2),
    )
    assert queue.claim(NOW + timedelta(seconds=3599)) is None
    assert queue.claim(NOW + timedelta(seconds=3600)).id == task.id


def test_expired_confirmation_and_stale_stay_not_claimed(queue):
    seed(queue, days=2)
    task = queue.claim(NOW)
    queue.complete(task, NOW, revisit_seconds=86400 * 2)
    queue.prioritize(
        task.hotel_id,
        task.checkin,
        task.checkout,
        task.adults,
        due_at=NOW + timedelta(seconds=1),
        expires_at=NOW + timedelta(seconds=2),
    )
    next_day = queue.claim(NOW + timedelta(days=1))
    assert next_day.checkin == TODAY + timedelta(days=1)
    assert queue.get(task.id)["status"] == "EXPIRED"
    assert queue.get(task.id)["confirmation_due"] is None


def test_today_override_uses_local_calendar_day(queue):
    seed(queue, days=2)
    task = queue.claim(NOW, today=TODAY + timedelta(days=1))
    assert task.checkin == TODAY + timedelta(days=1)


def test_no_confirmation_outside_watchlist_or_wrong_occupancy(queue):
    seed(queue, days=1)
    assert not queue.prioritize(
        "NYCTE",
        TODAY,
        TODAY + timedelta(days=1),
        1,
        due_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )
    assert not queue.prioritize(
        "XIYBP",
        TODAY,
        TODAY + timedelta(days=1),
        2,
        due_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


@pytest.mark.parametrize(
    "kwargs",
    [{"days": 366}, {"days": 0}, {"days": True}, {"nights": 0}, {"adults": 0}, {"adults": True}],
)
def test_invalid_ranges_are_rejected_without_writing(queue, kwargs):
    with pytest.raises(ValueError):
        seed(queue, **kwargs)
    assert queue.report()["tasks"] == 0


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/hotel",
        "http://www.marriott.com/hotel",
        "https://www.marriott.com.evil.example/hotel",
        "https://secret@www.marriott.com/hotel",
        "https://www.marriott.com:8443/hotel",
    ],
)
def test_only_explicit_official_hotel_pages(queue, url):
    with pytest.raises(ValueError):
        queue.seed([{"hotel_id": "XIYBP", "hotel_url": url}], TODAY, now=NOW)
    assert queue.report()["tasks"] == 0


def test_naive_clock_and_duplicate_hotels_rejected(queue):
    with pytest.raises(ValueError):
        queue.seed(HOTELS, TODAY, now=datetime(2026, 10, 20))
    with pytest.raises(ValueError):
        queue.seed([HOTELS[0], HOTELS[0]], TODAY, now=NOW)
    assert queue.report()["tasks"] == 0


def test_timezones_are_normalized_to_utc(queue):
    local_now = NOW.astimezone(timezone(timedelta(hours=8)))
    queue.seed(HOTELS[:1], TODAY, now=local_now, days=1)
    task = queue.claim(local_now)
    assert queue.get(task.id)["last_started_at"] == NOW.isoformat()


def test_report_is_read_only_and_fail_keeps_reason(queue):
    seed(queue, days=1)
    task = queue.claim(NOW)
    assert queue.report(NOW + timedelta(hours=1))["states"] == {"RUNNING": 1}
    queue.fail(task, NOW, "MISSING_PRICE_RESPONSE")
    assert queue.report()["states"] == {"FAILED": 1}
    assert queue.get(task.id)["last_error"] == "MISSING_PRICE_RESPONSE"


def test_seed_accepts_configuration_dataclass(queue):
    @dataclass
    class Hotel:
        hotel_id: str
        hotel_url: str

    assert queue.seed([Hotel(**HOTELS[0])], TODAY, days=1, now=NOW) == 1


def test_read_report_missing_database_creates_no_files(tmp_path):
    path = tmp_path / "not-created" / "q.sqlite"
    result = Queue.read_report(path, NOW)
    assert result["initialized"] is False
    assert result["tasks"] == 0
    assert result["observed_at"] == NOW.isoformat()
    assert not path.parent.exists()


def test_read_report_observes_active_writer_without_releasing_lease(tmp_path):
    path = tmp_path / "q.sqlite"
    with Queue(path) as queue:
        seed(queue, days=1)
        task = queue.claim(NOW)
        summary = Queue.read_report(path, NOW + timedelta(hours=1))
        assert summary["initialized"] is True
        assert summary["states"] == {"RUNNING": 1}
        assert queue.get(task.id)["lease_token"] == task.lease_token
