from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db
from app.scheduler import BriefingJob, ReminderScheduler
from app.services.notify import Notifier
from app.tools.briefing import MAX_ITEMS, DailyBriefingArgs, daily_briefing

TZ = "Asia/Karachi"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 10, 14, 9, 0, tzinfo=ZONE)  # Wednesday 9 AM


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def conn(path):
    connection = db.connect(path)
    yield connection
    connection.close()


def add(conn, title, local_dt):
    return db.insert_reminder(conn, title, local_dt.astimezone(timezone.utc), TZ)


def test_briefing_lists_overdue_and_rest_of_today(conn):
    late = add(conn, "pay rent", NOW - timedelta(days=1))
    soon = add(conn, "call Ali", NOW.replace(hour=15))
    done = add(conn, "gym", NOW.replace(hour=18))
    db.mark_done(conn, done.id)
    add(conn, "tomorrow thing", NOW + timedelta(days=1))

    r = daily_briefing(conn, DailyBriefingArgs(), NOW, TZ)
    assert r.ok
    assert r.message.splitlines() == [
        "Good morning! It's Wednesday, 14 October.",
        "Overdue (1):",
        "#1 pay rent — Tue 13 Oct, 9:00 AM",
        "Still to do today (1):",
        "#2 call Ali — Wed 14 Oct, 3:00 PM",
    ]
    assert r.data == {"overdue": [late.id], "today": [soon.id]}


def test_briefing_empty_day(conn):
    r = daily_briefing(conn, DailyBriefingArgs(), NOW.replace(hour=20), TZ)
    assert r.message == "Good evening! It's Wednesday, 14 October.\nNothing else is scheduled for today."


def test_briefing_caps_long_sections(conn):
    for i in range(MAX_ITEMS + 3):
        add(conn, f"task {i}", NOW + timedelta(minutes=i + 1))
    r = daily_briefing(conn, DailyBriefingArgs(), NOW, TZ)
    assert r.message.endswith("…and 3 more.")
    assert len(r.data["today"]) == MAX_ITEMS + 3


class Sent:
    def __init__(self):
        self.items = []

    def __call__(self, title, body):
        self.items.append((title, body))


def test_job_waits_until_briefing_time(path):
    sent = Sent()
    job = BriefingJob(path, TZ, time(8, 0), sent)
    assert job(NOW.replace(hour=7, minute=59)) is False
    assert sent.items == []


def test_job_sends_once_per_day_even_after_restart(path):
    notifier = Notifier(path, TZ, toast=False)
    now = datetime.now(ZONE)  # the feed records real creation time, so use the real clock here
    assert BriefingJob(path, TZ, time(0, 0), notifier.briefing)(now) is True
    # a new job object = server restarted later the same day
    assert BriefingJob(path, TZ, time(0, 0), notifier.briefing)(now) is False
    conn = db.connect(path)
    try:
        items = db.list_notifications(conn)
    finally:
        conn.close()
    assert len(items) == 1 and items[0].kind == db.KIND_BRIEFING and items[0].title == "Daily briefing"


def test_scheduler_runs_briefing_and_survives_its_errors(path):
    calls = []

    def broken_briefing(now):
        calls.append(now)
        raise RuntimeError("boom")

    sched = ReminderScheduler(path, notify=lambda r, m: None, clock=lambda: NOW, briefing=broken_briefing)
    sched._run_check()  # must not raise
    assert calls == [NOW]


def test_notifier_briefing_toast_uses_env_not_script(path):
    seen = []
    notifier = Notifier(path, TZ, runner=lambda args, env, timeout: seen.append(env) or 0)
    notifier.briefing("Daily briefing", "Overdue (1):\n#1 '; rm -rf")
    assert seen[0]["DAZIA_TOAST_BODY"] == "Overdue (1):\n#1 '; rm -rf"
