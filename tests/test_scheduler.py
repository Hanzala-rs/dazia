from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.scheduler import ReminderScheduler

UTC = timezone.utc
NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


class Clock:
    """Injectable clock the tests move by hand."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class Recorder:
    """notify() fake: records deliveries; raises for reminder ids listed in `fail_ids`."""

    def __init__(self):
        self.delivered = []  # [(reminder_id, missed)]
        self.fail_ids = set()

    def __call__(self, reminder, missed):
        if reminder.id in self.fail_ids:
            raise RuntimeError("toast failed")
        self.delivered.append((reminder.id, missed))


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "test.db")


def add(path, title, due):
    conn = db.connect(path)
    try:
        return db.insert_reminder(conn, title, due, "UTC")
    finally:
        conn.close()


def make(path, notify, clock, **kwargs):
    return ReminderScheduler(path, notify, clock=clock, **kwargs)


def test_due_reminder_delivered_once_across_checks(path):
    r = add(path, "x", NOW - timedelta(seconds=10))
    future = add(path, "later", NOW + timedelta(minutes=5))
    notify, clock = Recorder(), Clock(NOW)
    s = make(path, notify, clock)

    assert s.check_now() == 1
    assert s.check_now() == 0
    assert notify.delivered == [(r.id, False)]

    clock.now = NOW + timedelta(minutes=5)
    assert s.check_now() == 1
    assert notify.delivered[-1] == (future.id, False)


def test_missed_flag(path):
    on_time = add(path, "on time", NOW - timedelta(seconds=120))  # exactly at the limit: not missed
    overdue = add(path, "overdue", NOW - timedelta(seconds=121))
    notify = Recorder()
    make(path, notify, Clock(NOW), missed_after_s=120).check_now()
    assert sorted(notify.delivered) == sorted([(on_time.id, False), (overdue.id, True)])


def test_failed_notify_is_released_and_retried(path):
    r = add(path, "x", NOW)
    notify, clock = Recorder(), Clock(NOW)
    s = make(path, notify, clock)
    notify.fail_ids.add(r.id)

    assert s.check_now() == 0
    conn = db.connect(path)
    assert db.get_reminder(conn, r.id).notified_at is None
    conn.close()

    notify.fail_ids.clear()
    clock.now = NOW + timedelta(seconds=30)
    assert s.check_now() == 1
    assert notify.delivered == [(r.id, False)]


def test_one_failure_does_not_block_others(path):
    bad = add(path, "bad", NOW - timedelta(seconds=20))
    good = add(path, "good", NOW - timedelta(seconds=10))
    notify = Recorder()
    notify.fail_ids.add(bad.id)
    assert make(path, notify, Clock(NOW)).check_now() == 1
    assert notify.delivered == [(good.id, False)]


def test_failure_logs_ids_not_titles(path, caplog):
    r = add(path, "secret doctor visit", NOW)
    notify = Recorder()
    notify.fail_ids.add(r.id)
    with caplog.at_level("INFO", logger="app.scheduler"):
        make(path, notify, Clock(NOW)).check_now()
    assert f"#{r.id}" in caplog.text
    assert "secret doctor visit" not in caplog.text


def test_start_runs_startup_check_then_shutdown(path):
    r = add(path, "missed while off", NOW - timedelta(hours=3))
    notify = Recorder()
    s = make(path, notify, Clock(NOW), interval_s=3600)
    s.start()
    try:
        assert notify.delivered == [(r.id, True)]  # delivered synchronously before start() returned
        job = s._scheduler.get_job("reminder-check")
        assert job is not None and job.max_instances == 1 and job.coalesce is True
    finally:
        s.shutdown()
    s.shutdown()  # second call is safe
    assert s._scheduler is None
