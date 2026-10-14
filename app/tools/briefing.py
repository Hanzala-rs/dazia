"""Daily briefing tool: today's date, overdue reminders, and the rest of today's reminders.

Built from SQLite only (no LLM, no external API), so it is fast and always grounded.
"""
import sqlite3
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel

from app import db
from app.services.notify import format_due
from app.tools import ToolResult

MAX_ITEMS = 10  # per section, to keep the briefing short (also when spoken)


class DailyBriefingArgs(BaseModel):
    pass


def _greeting(hour: int) -> str:
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _section(heading: str, items: list[db.Reminder], tz: str) -> list[str]:
    lines = [heading, *(f"#{r.id} {r.title} — {format_due(r.due_utc, tz)}" for r in items[:MAX_ITEMS])]
    if len(items) > MAX_ITEMS:
        lines.append(f"…and {len(items) - MAX_ITEMS} more.")
    return lines


def daily_briefing(conn: sqlite3.Connection, args: DailyBriefingArgs, now: datetime, tz: str) -> ToolResult:
    """data={"overdue": [ids], "today": [ids]}. Overdue = still pending and due before now."""
    zone = ZoneInfo(tz)
    local = now.astimezone(zone)
    end_of_day = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=zone)

    overdue = db.list_reminders(conn, end_utc=now, status=db.STATUS_PENDING)
    today = db.list_reminders(conn, start_utc=now, end_utc=end_of_day, status=db.STATUS_PENDING)

    lines = [f"{_greeting(local.hour)}! It's {local:%A}, {local.day} {local:%B}."]
    if overdue:
        lines += _section(f"Overdue ({len(overdue)}):", overdue, tz)
    if today:
        lines += _section(f"Still to do today ({len(today)}):", today, tz)
    else:
        lines.append("Nothing else is scheduled for today.")
    return ToolResult(
        ok=True,
        message="\n".join(lines),
        data={"overdue": [r.id for r in overdue], "today": [r.id for r in today]},
    )
