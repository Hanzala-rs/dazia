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
"""


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
