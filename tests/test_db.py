import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import db

UTC = timezone.utc


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(str(tmp_path / "test.db"))
    yield connection
    connection.close()


def test_connect_creates_schema(conn):
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(reminders)")]
    assert columns == ["id", "title", "due_utc", "tz", "status", "created_at", "notified_at"]
    indexes = [row["name"] for row in conn.execute("PRAGMA index_list(reminders)")]
    assert "idx_reminders_status_due" in indexes


def test_connect_twice_is_safe(tmp_path):
    path = str(tmp_path / "test.db")
    db.connect(path).close()
    db.connect(path).close()


def test_insert_and_get_returns_aware_utc(conn):
    due = datetime(2026, 10, 15, 9, 0, tzinfo=ZoneInfo("Asia/Karachi"))
    created = db.insert_reminder(conn, "finish project", due, "Asia/Karachi")

    assert created.id >= 1
    assert created.status == db.STATUS_PENDING
    assert created.notified_at is None
    assert created.due_utc == datetime(2026, 10, 15, 4, 0, tzinfo=UTC)
    assert created.due_utc.utcoffset() == timedelta(0)
    assert created.created_at.tzinfo is not None

    fetched = db.get_reminder(conn, created.id)
    assert fetched == created
    raw = conn.execute("SELECT due_utc FROM reminders WHERE id = ?", (created.id,)).fetchone()[0]
    assert raw == "2026-10-15T04:00:00+00:00"


def test_insert_rejects_naive_datetime(conn):
    with pytest.raises(ValueError):
        db.insert_reminder(conn, "x", datetime(2026, 10, 15, 9, 0), "UTC")


def test_get_missing_returns_none(conn):
    assert db.get_reminder(conn, 999) is None


def test_list_filters_and_order(conn):
    base = datetime(2026, 10, 15, 0, 0, tzinfo=UTC)
    late = db.insert_reminder(conn, "late", base + timedelta(hours=5), "UTC")
    early = db.insert_reminder(conn, "early", base + timedelta(hours=1), "UTC")
    edge = db.insert_reminder(conn, "edge", base + timedelta(hours=10), "UTC")
    db.mark_done(conn, early.id)

    assert [r.id for r in db.list_reminders(conn)] == [early.id, late.id, edge.id]
    # start inclusive, end exclusive
    window = db.list_reminders(conn, start_utc=base + timedelta(hours=1), end_utc=base + timedelta(hours=10))
    assert [r.id for r in window] == [early.id, late.id]
    pending = db.list_reminders(conn, status=db.STATUS_PENDING)
    assert [r.id for r in pending] == [late.id, edge.id]


def test_list_rejects_naive_bounds_and_bad_status(conn):
    with pytest.raises(ValueError):
        db.list_reminders(conn, start_utc=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        db.list_reminders(conn, status="nope")


def test_mark_done(conn):
    r = db.insert_reminder(conn, "x", datetime(2026, 10, 15, tzinfo=UTC), "UTC")
    assert db.mark_done(conn, r.id) is True
    assert db.get_reminder(conn, r.id).status == db.STATUS_DONE
    assert db.mark_done(conn, r.id) is False  # already done
    assert db.mark_done(conn, 999) is False  # missing


def test_writes_are_committed(tmp_path):
    path = str(tmp_path / "test.db")
    conn = db.connect(path)
    r = db.insert_reminder(conn, "persist me", datetime(2026, 10, 15, tzinfo=UTC), "UTC")
    db.mark_done(conn, r.id)
    conn.close()

    reopened = db.connect(path)
    assert reopened.execute("SELECT status FROM reminders").fetchone()[0] == db.STATUS_DONE
    reopened.close()


def test_sql_injection_title_is_stored_literally(conn):
    title = "x'); DROP TABLE reminders; --"
    r = db.insert_reminder(conn, title, datetime(2026, 10, 15, tzinfo=UTC), "UTC")
    assert db.get_reminder(conn, r.id).title == title


# ---- Phase 2: claiming, snooze, notifications --------------------------------------

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def add(conn, title, due):
    return db.insert_reminder(conn, title, due, "UTC")


@pytest.fixture(params=[True, False], ids=["returning", "fallback"])
def claim_path(request, monkeypatch):
    """Run claim tests on both the UPDATE ... RETURNING path and the BEGIN IMMEDIATE fallback."""
    monkeypatch.setattr(db, "_HAS_RETURNING", request.param)


def test_connect_creates_notifications_table(conn):
    columns = [row["name"] for row in conn.execute("PRAGMA table_info(notifications)")]
    assert columns == ["id", "reminder_id", "title", "body", "kind", "created_at", "read_at"]


def test_old_schema_db_upgrades_on_connect(tmp_path):
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE reminders (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, due_utc TEXT NOT NULL, tz TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, notified_at TEXT);
        INSERT INTO reminders (title, due_utc, tz, status, created_at)
            VALUES ('old one', '2026-10-15T04:00:00+00:00', 'UTC', 'pending', '2026-10-14T00:00:00+00:00');
    """)
    old.commit()
    old.close()

    conn = db.connect(path)
    try:
        assert [r.title for r in db.list_reminders(conn)] == ["old one"]
        n = db.add_notification(conn, "old one", "Due now", db.KIND_REMINDER, reminder_id=1)
        assert db.list_notifications(conn) == [n]
    finally:
        conn.close()


def test_claim_due_claims_each_reminder_once(conn, claim_path):
    a = add(conn, "a", NOW - timedelta(minutes=5))
    b = add(conn, "b", NOW)  # due exactly now counts
    future = add(conn, "future", NOW + timedelta(minutes=1))
    done = add(conn, "done", NOW - timedelta(hours=1))
    db.mark_done(conn, done.id)

    claimed = db.claim_due(conn, NOW)
    assert [r.id for r in claimed] == [a.id, b.id]
    assert all(r.notified_at == NOW for r in claimed)
    assert db.claim_due(conn, NOW) == []
    assert db.claim_due(conn, NOW + timedelta(seconds=30)) == []
    assert db.get_reminder(conn, future.id).notified_at is None
    assert db.get_reminder(conn, done.id).notified_at is None

    # the future one becomes claimable once it is due, and only once
    later = NOW + timedelta(minutes=1)
    assert [r.id for r in db.claim_due(conn, later)] == [future.id]
    assert db.claim_due(conn, later) == []


def test_claim_due_is_exclusive_across_connections(tmp_path, claim_path):
    path = str(tmp_path / "test.db")
    first, second = db.connect(path), db.connect(path)
    try:
        r = add(first, "x", NOW - timedelta(minutes=1))
        assert [c.id for c in db.claim_due(second, NOW)] == [r.id]
        assert db.claim_due(first, NOW) == []
    finally:
        first.close()
        second.close()


def test_claim_due_rejects_naive_now(conn):
    with pytest.raises(ValueError):
        db.claim_due(conn, datetime(2026, 10, 15, 12, 0))


def test_release_claim_makes_it_claimable_again(conn, claim_path):
    r = add(conn, "x", NOW - timedelta(minutes=1))
    assert len(db.claim_due(conn, NOW)) == 1
    db.release_claim(conn, r.id)
    assert db.get_reminder(conn, r.id).notified_at is None
    assert [c.id for c in db.claim_due(conn, NOW)] == [r.id]


def test_reschedule_clears_notified_at(conn):
    r = add(conn, "x", NOW - timedelta(minutes=1))
    db.claim_due(conn, NOW)
    new_due = NOW + timedelta(minutes=10)
    assert db.reschedule(conn, r.id, new_due) is True
    stored = db.get_reminder(conn, r.id)
    assert stored.due_utc == new_due and stored.notified_at is None
    assert db.claim_due(conn, NOW) == []
    assert [c.id for c in db.claim_due(conn, new_due)] == [r.id]


def test_reschedule_only_pending(conn):
    r = add(conn, "x", NOW)
    db.mark_done(conn, r.id)
    assert db.reschedule(conn, r.id, NOW + timedelta(hours=1)) is False
    assert db.get_reminder(conn, r.id).due_utc == NOW
    assert db.reschedule(conn, 999, NOW) is False
    with pytest.raises(ValueError):
        db.reschedule(conn, r.id, datetime(2026, 10, 15, 13, 0))


def test_notifications_add_list_and_mark_read(conn):
    first = db.add_notification(conn, "call Ali", "Due 9:00 AM", db.KIND_REMINDER, reminder_id=7)
    second = db.add_notification(conn, "pay bills", "Missed at 8:00 AM", db.KIND_MISSED)
    assert first.reminder_id == 7 and second.reminder_id is None
    assert first.read_at is None and first.created_at.tzinfo is not None

    assert [n.id for n in db.list_notifications(conn)] == [second.id, first.id]  # newest first
    assert [n.id for n in db.list_notifications(conn, limit=1)] == [second.id]

    assert db.mark_notification_read(conn, first.id) is True
    assert db.mark_notification_read(conn, first.id) is False  # already read
    assert db.mark_notification_read(conn, 999) is False
    assert [n.id for n in db.list_notifications(conn)] == [second.id]
    everything = db.list_notifications(conn, unread_only=False)
    assert [n.id for n in everything] == [second.id, first.id]
    assert everything[1].read_at is not None


def test_add_notification_rejects_unknown_kind(conn):
    with pytest.raises(ValueError):
        db.add_notification(conn, "x", "y", "spam")
    assert db.list_notifications(conn, unread_only=False) == []
