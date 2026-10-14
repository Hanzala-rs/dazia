from datetime import datetime, timezone

import pytest

from app import data, db

DUE = datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def path(tmp_path):
    p = str(tmp_path / "dazia.db")
    conn = db.connect(p)
    try:
        r = db.insert_reminder(conn, "secret-title-xyz", DUE, "Asia/Karachi")
        db.add_notification(conn, "Reminder", "secret-title-xyz", db.KIND_REMINDER, reminder_id=r.id)
        db.add_session(conn, "hash123", DUE)
    finally:
        conn.close()
    return p


def test_backup_is_a_full_copy_while_db_is_open(path, tmp_path):
    live = db.connect(path)  # DAZIA running: another connection is open
    try:
        dest = data.backup(path, str(tmp_path / "backups"))
    finally:
        live.close()
    assert dest.name.startswith("dazia-") and dest.suffix == ".db"
    copy = db.connect(str(dest))
    try:
        assert [r.title for r in db.list_reminders(copy)] == ["secret-title-xyz"]
        assert len(db.list_notifications(copy)) == 1
    finally:
        copy.close()


def test_wipe_needs_exact_confirmation(path):
    for answer in ["", "delete", "yes", "DELETE please"]:
        assert data.wipe(path, answer) is None
    conn = db.connect(path)
    try:
        assert db.count_user_data(conn) == {"reminders": 1, "notifications": 1}
    finally:
        conn.close()


def test_wipe_deletes_user_data_keeps_sessions_and_leaves_no_trace(path):
    assert data.wipe(path, " DELETE\n") == {"reminders": 1, "notifications": 1}
    conn = db.connect(path)
    try:
        assert db.count_user_data(conn) == {"reminders": 0, "notifications": 0}
        assert db.get_session_valid(conn, "hash123", datetime(2026, 1, 1, tzinfo=timezone.utc))
    finally:
        conn.close()
    with open(path, "rb") as f:
        assert b"secret-title-xyz" not in f.read()  # VACUUM rewrote the file
