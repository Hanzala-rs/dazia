from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app import db
from app.tools.reminders import (
    CompleteReminderArgs,
    CreateReminderArgs,
    ListRemindersArgs,
    SnoozeReminderArgs,
    complete_reminder,
    create_reminder,
    list_reminders,
    snooze_reminder,
)

TZ = "Asia/Karachi"
ZONE = ZoneInfo(TZ)
NOW = datetime(2026, 10, 14, 15, 0, tzinfo=ZONE)  # Wednesday 3 PM


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def create(conn, title, when):
    return create_reminder(conn, CreateReminderArgs(title=title, when=when), NOW, TZ)


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]


def test_create_success(conn):
    result = create(conn, "  finish my project ", "tomorrow at 9 AM")
    assert result.ok and not result.needs_clarification
    assert result.message == "Reminder set: finish my project — Thu 15 Oct, 9:00 AM"
    reminder = result.data["reminder"]
    assert reminder["title"] == "finish my project"
    assert reminder["due_local"] == "2026-10-15T09:00:00+05:00"
    assert reminder["tz"] == TZ

    stored = db.get_reminder(conn, reminder["id"])
    assert stored.due_utc == datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
    assert stored.tz == TZ


@pytest.mark.parametrize("when", ["tomorrow at 9", "tomorrow", "today at 1 pm", "blah blah"])
def test_create_needs_clarification_saves_nothing(conn, when):
    result = create(conn, "call mom", when)
    assert not result.ok
    assert result.needs_clarification
    assert result.message
    assert count(conn) == 0


def test_create_ambiguous_message(conn):
    assert create(conn, "call mom", "tomorrow at 9").message == "Did you mean 9 AM or 9 PM?"


def test_create_blank_title(conn):
    result = create(conn, "   ", "tomorrow at 9 AM")
    assert not result.ok and result.needs_clarification
    assert count(conn) == 0


def test_args_validation():
    with pytest.raises(ValidationError):
        CreateReminderArgs(title="", when="tomorrow at 9 AM")
    with pytest.raises(ValidationError):
        CreateReminderArgs(title="x" * 201, when="tomorrow at 9 AM")
    with pytest.raises(ValidationError):
        ListRemindersArgs(range="yesterday")
    with pytest.raises(ValidationError):
        CompleteReminderArgs(reminder_id=0)


def add(conn, title, local_dt):
    return db.insert_reminder(conn, title, local_dt.replace(tzinfo=ZONE), TZ)


def ids(result):
    return [r["id"] for r in result.data["reminders"]]


def test_list_ranges_use_local_days(conn):
    # 00:30 local on the 15th is still the 14th in UTC - must count as "tomorrow".
    past_today = add(conn, "past today", datetime(2026, 10, 14, 9, 0))
    later_today = add(conn, "later today", datetime(2026, 10, 14, 23, 59))
    early_tomorrow = add(conn, "early tomorrow", datetime(2026, 10, 15, 0, 30))
    next_week = add(conn, "next week", datetime(2026, 10, 21, 10, 0))

    def run(range_):
        return list_reminders(conn, ListRemindersArgs(range=range_), NOW, TZ)

    assert ids(run("today")) == [past_today.id, later_today.id]
    assert ids(run("tomorrow")) == [early_tomorrow.id]
    assert ids(run("upcoming")) == [later_today.id, early_tomorrow.id, next_week.id]
    assert ids(run("all")) == [past_today.id, later_today.id, early_tomorrow.id, next_week.id]

    item = run("tomorrow").data["reminders"][0]
    assert item == {
        "id": early_tomorrow.id,
        "title": "early tomorrow",
        "due_local": "2026-10-15T00:30:00+05:00",
        "status": "pending",
    }
    assert "early tomorrow" in run("tomorrow").message


def test_upcoming_excludes_done(conn):
    r = add(conn, "done one", datetime(2026, 10, 16, 10, 0))
    db.mark_done(conn, r.id)
    result = list_reminders(conn, ListRemindersArgs(range="upcoming"), NOW, TZ)
    assert ids(result) == []
    assert result.message.startswith("You have no reminders")
    all_result = list_reminders(conn, ListRemindersArgs(range="all"), NOW, TZ)
    assert all_result.data["reminders"][0]["status"] == "done"


def test_list_empty(conn):
    result = list_reminders(conn, ListRemindersArgs(), NOW, TZ)
    assert result.ok
    assert result.data == {"reminders": []}
    assert result.message == "You have no reminders coming up."


def test_complete(conn):
    r = add(conn, "pay bills", datetime(2026, 10, 16, 10, 0))
    args = CompleteReminderArgs(reminder_id=r.id)

    first = complete_reminder(conn, args, NOW, TZ)
    assert first.ok
    assert "pay bills" in first.message
    assert db.get_reminder(conn, r.id).status == db.STATUS_DONE

    second = complete_reminder(conn, args, NOW, TZ)
    assert not second.ok
    assert "already done" in second.message


def test_complete_missing(conn):
    result = complete_reminder(conn, CompleteReminderArgs(reminder_id=42), NOW, TZ)
    assert not result.ok
    assert "#42" in result.message


def snooze(conn, reminder_id, when):
    return snooze_reminder(conn, SnoozeReminderArgs(reminder_id=reminder_id, when=when), NOW, TZ)


def test_snooze_moves_due_time_and_clears_claim(conn):
    r = add(conn, "stretch", datetime(2026, 10, 14, 14, 0))
    assert [c.id for c in db.claim_due(conn, NOW)] == [r.id]

    result = snooze(conn, r.id, "tomorrow at 9 AM")
    assert result.ok and not result.needs_clarification
    assert result.message == "Snoozed: stretch — Thu 15 Oct, 9:00 AM"
    assert result.data["reminder"]["due_local"] == "2026-10-15T09:00:00+05:00"
    stored = db.get_reminder(conn, r.id)
    assert stored.due_utc == datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)
    assert stored.notified_at is None


def test_snooze_relative(conn):
    r = add(conn, "tea", datetime(2026, 10, 14, 14, 0))
    result = snooze(conn, r.id, "in 10 minutes")
    assert result.ok
    assert db.get_reminder(conn, r.id).due_utc == datetime(2026, 10, 14, 10, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize("when", ["tomorrow at 9", "blah blah"])
def test_snooze_needs_clarification_changes_nothing(conn, when):
    r = add(conn, "stretch", datetime(2026, 10, 14, 14, 0))
    result = snooze(conn, r.id, when)
    assert not result.ok and result.needs_clarification and result.message
    assert db.get_reminder(conn, r.id).due_utc == r.due_utc


def test_snooze_missing_and_done(conn):
    missing = snooze(conn, 42, "in 10 minutes")
    assert not missing.ok and not missing.needs_clarification and "#42" in missing.message

    r = add(conn, "pay bills", datetime(2026, 10, 16, 10, 0))
    db.mark_done(conn, r.id)
    done = snooze(conn, r.id, "in 10 minutes")
    assert not done.ok and "already done" in done.message
    assert db.get_reminder(conn, r.id).due_utc == r.due_utc


def test_snooze_args_validation():
    with pytest.raises(ValidationError):
        SnoozeReminderArgs(reminder_id=0, when="in 10 minutes")
    with pytest.raises(ValidationError):
        SnoozeReminderArgs(reminder_id=1, when="")
    with pytest.raises(ValidationError):
        SnoozeReminderArgs(reminder_id=1, when="x" * 101)
