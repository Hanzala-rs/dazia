"""SQLite storage for DAZIA.

All datetimes are stored as ISO-8601 UTC strings (e.g. "2026-10-15T04:00:00+00:00")
and returned as timezone-aware UTC datetimes. Every query is parameterized.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

STATUS_PENDING = "pending"
STATUS_DONE = "done"
_STATUSES = (STATUS_PENDING, STATUS_DONE)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    due_utc TEXT NOT NULL,
    tz TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    notified_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reminders_status_due ON reminders (status, due_utc);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    reminder_id INTEGER NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    read_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""

# UPDATE ... RETURNING needs SQLite 3.35+; older builds use a locked SELECT + UPDATE instead.
_HAS_RETURNING = sqlite3.sqlite_version_info >= (3, 35, 0)


@dataclass
class Reminder:
    id: int
    title: str
    due_utc: datetime  # aware, UTC
    tz: str  # IANA zone the user meant, e.g. "Asia/Karachi"
    status: str  # STATUS_PENDING | STATUS_DONE
    created_at: datetime  # aware, UTC
    notified_at: datetime | None  # set once a notification is delivered (Phase 2)


# ---- helpers -------------------------------------------------------------------

def _to_utc_text(value: datetime) -> str:
    """Aware datetime -> fixed-format UTC string. Same format everywhere so text ordering = time ordering."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _from_utc_text(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        title=row["title"],
        due_utc=_from_utc_text(row["due_utc"]),
        tz=row["tz"],
        status=row["status"],
        created_at=_from_utc_text(row["created_at"]),
        notified_at=_from_utc_text(row["notified_at"]) if row["notified_at"] else None,
    )


# ---- public API ----------------------------------------------------------------

def connect(path: str) -> sqlite3.Connection:
    """Open a connection and create the schema if missing."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def insert_reminder(conn: sqlite3.Connection, title: str, due_utc: datetime, tz: str) -> Reminder:
    due_text = _to_utc_text(due_utc)
    created_text = _to_utc_text(datetime.now(timezone.utc))
    cur = conn.execute(
        "INSERT INTO reminders (title, due_utc, tz, status, created_at) VALUES (?, ?, ?, ?, ?)",
        (title, due_text, tz, STATUS_PENDING, created_text),
    )
    conn.commit()
    reminder = get_reminder(conn, cur.lastrowid)
    assert reminder is not None  # we just inserted it
    return reminder


def get_reminder(conn: sqlite3.Connection, reminder_id: int) -> Reminder | None:
    row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return _row_to_reminder(row) if row else None


def list_reminders(
    conn: sqlite3.Connection,
    start_utc: datetime | None = None,
    end_utc: datetime | None = None,
    status: str | None = None,
) -> list[Reminder]:
    """Reminders ordered by due time; bounds are inclusive start, exclusive end."""
    # The WHERE pieces are fixed strings; only values go in as parameters.
    conditions: list[str] = []
    params: list[str] = []
    if start_utc is not None:
        conditions.append("due_utc >= ?")
        params.append(_to_utc_text(start_utc))
    if end_utc is not None:
        conditions.append("due_utc < ?")
        params.append(_to_utc_text(end_utc))
    if status is not None:
        if status not in _STATUSES:
            raise ValueError(f"unknown status: {status!r}")
        conditions.append("status = ?")
        params.append(status)

    sql = "SELECT * FROM reminders"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += " ORDER BY due_utc, id"
    return [_row_to_reminder(row) for row in conn.execute(sql, params).fetchall()]


def mark_done(conn: sqlite3.Connection, reminder_id: int) -> bool:
    """Return True if a pending reminder was marked done."""
    cur = conn.execute(
        "UPDATE reminders SET status = ? WHERE id = ? AND status = ?",
        (STATUS_DONE, reminder_id, STATUS_PENDING),
    )
    conn.commit()
    return cur.rowcount == 1


# ---- Phase 2: delivery and notifications -----------------------------------------
# Delivery is idempotent: a reminder is "claimed" by setting notified_at in the same UPDATE
# that selects it, so two checks can never deliver the same reminder twice.

def claim_due(conn: sqlite3.Connection, now_utc: datetime) -> list[Reminder]:
    """Atomically set notified_at=now for pending reminders with notified_at NULL and due_utc <= now;
    return exactly the reminders this call claimed."""
    now_text = _to_utc_text(now_utc)
    if _HAS_RETURNING:
        rows = conn.execute(
            "UPDATE reminders SET notified_at = ? "
            "WHERE status = ? AND notified_at IS NULL AND due_utc <= ? RETURNING *",
            (now_text, STATUS_PENDING, now_text),
        ).fetchall()
        conn.commit()
    else:
        # BEGIN IMMEDIATE takes the write lock first, so no other connection can claim in between.
        conn.commit()  # end any implicit transaction before starting our own
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = ? AND notified_at IS NULL AND due_utc <= ?",
                (STATUS_PENDING, now_text),
            ).fetchall()
            conn.executemany(
                "UPDATE reminders SET notified_at = ? WHERE id = ?", [(now_text, row["id"]) for row in rows]
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        rows = [dict(row, notified_at=now_text) for row in rows]
    reminders = [_row_to_reminder(row) for row in rows]
    reminders.sort(key=lambda r: (r.due_utc, r.id))
    return reminders


def release_claim(conn: sqlite3.Connection, reminder_id: int) -> None:
    """Undo a claim (notified_at=NULL) when delivery failed, so the next check retries it."""
    conn.execute("UPDATE reminders SET notified_at = NULL WHERE id = ?", (reminder_id,))
    conn.commit()


def reschedule(conn: sqlite3.Connection, reminder_id: int, due_utc: datetime) -> bool:
    """Snooze: set a new due time and clear notified_at. Only pending reminders. True if updated."""
    cur = conn.execute(
        "UPDATE reminders SET due_utc = ?, notified_at = NULL WHERE id = ? AND status = ?",
        (_to_utc_text(due_utc), reminder_id, STATUS_PENDING),
    )
    conn.commit()
    return cur.rowcount == 1


KIND_REMINDER = "reminder"  # delivered on time
KIND_MISSED = "missed"  # delivered late (laptop was off/asleep or server stopped)
KIND_BRIEFING = "briefing"  # Phase 7: the scheduled daily briefing
_KINDS = (KIND_REMINDER, KIND_MISSED, KIND_BRIEFING)


@dataclass
class Notification:
    id: int
    reminder_id: int | None
    title: str
    body: str
    kind: str  # KIND_REMINDER | KIND_MISSED | KIND_BRIEFING
    created_at: datetime  # aware, UTC
    read_at: datetime | None


def _row_to_notification(row: sqlite3.Row) -> Notification:
    return Notification(
        id=row["id"],
        reminder_id=row["reminder_id"],
        title=row["title"],
        body=row["body"],
        kind=row["kind"],
        created_at=_from_utc_text(row["created_at"]),
        read_at=_from_utc_text(row["read_at"]) if row["read_at"] else None,
    )


def add_notification(
    conn: sqlite3.Connection, title: str, body: str, kind: str, reminder_id: int | None = None
) -> Notification:
    if kind not in _KINDS:
        raise ValueError(f"unknown notification kind: {kind!r}")
    created_text = _to_utc_text(datetime.now(timezone.utc))
    cur = conn.execute(
        "INSERT INTO notifications (reminder_id, title, body, kind, created_at) VALUES (?, ?, ?, ?, ?)",
        (reminder_id, title, body, kind, created_text),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM notifications WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_notification(row)


def list_notifications(conn: sqlite3.Connection, unread_only: bool = True, limit: int = 50) -> list[Notification]:
    """Newest first."""
    sql = "SELECT * FROM notifications"
    if unread_only:
        sql += " WHERE read_at IS NULL"
    sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
    return [_row_to_notification(row) for row in conn.execute(sql, (limit,)).fetchall()]


def has_notification_since(conn: sqlite3.Connection, kind: str, since_utc: datetime) -> bool:
    """True if a notification of `kind` was created at or after `since_utc` (used to send a briefing once a day)."""
    row = conn.execute(
        "SELECT 1 FROM notifications WHERE kind = ? AND created_at >= ? LIMIT 1", (kind, _to_utc_text(since_utc))
    ).fetchone()
    return row is not None


def mark_notification_read(conn: sqlite3.Connection, notification_id: int) -> bool:
    """Return True if an unread notification was marked read."""
    cur = conn.execute(
        "UPDATE notifications SET read_at = ? WHERE id = ? AND read_at IS NULL",
        (_to_utc_text(datetime.now(timezone.utc)), notification_id),
    )
    conn.commit()
    return cur.rowcount == 1


# ---- Phase 3: login sessions -------------------------------------------------------
# Only sha256(token) is stored, never the token itself, so a copied database can't be used to log in.

def add_session(conn: sqlite3.Connection, token_hash: str, expires_utc: datetime) -> None:
    conn.execute(
        "INSERT INTO sessions (token_hash, created_at, expires_at) VALUES (?, ?, ?)",
        (token_hash, _to_utc_text(datetime.now(timezone.utc)), _to_utc_text(expires_utc)),
    )
    conn.commit()


def get_session_valid(conn: sqlite3.Connection, token_hash: str, now_utc: datetime) -> bool:
    """True if the session exists and has not expired."""
    row = conn.execute(
        "SELECT 1 FROM sessions WHERE token_hash = ? AND expires_at > ?", (token_hash, _to_utc_text(now_utc))
    ).fetchone()
    return row is not None


def delete_session(conn: sqlite3.Connection, token_hash: str) -> bool:
    cur = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))
    conn.commit()
    return cur.rowcount == 1


def delete_expired_sessions(conn: sqlite3.Connection, now_utc: datetime) -> int:
    """Remove sessions with expires_at <= now. Returns how many were removed."""
    cur = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (_to_utc_text(now_utc),))
    conn.commit()
    return cur.rowcount


# ---- Phase 10: backup and wipe ------------------------------------------------------

def backup_to(conn: sqlite3.Connection, dest_path: str) -> None:
    """Copy the whole database to `dest_path` with SQLite's online backup (safe while DAZIA is running)."""
    dest = sqlite3.connect(dest_path)
    try:
        conn.backup(dest)
    finally:
        dest.close()


def count_user_data(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        "reminders": conn.execute("SELECT COUNT(*) FROM reminders").fetchone()[0],
        "notifications": conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0],
    }


def wipe_user_data(conn: sqlite3.Connection) -> dict[str, int]:
    """Delete all reminders and notifications (login sessions are kept), then VACUUM so the
    deleted text is not left behind in free pages of the file. Returns how many rows were deleted."""
    deleted = count_user_data(conn)
    with conn:  # one transaction: both tables or neither
        conn.execute("DELETE FROM notifications")
        conn.execute("DELETE FROM reminders")
    conn.execute("VACUUM")
    return deleted
