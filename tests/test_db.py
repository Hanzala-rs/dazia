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
