"""Validated reminder tools.

Argument models below are the contract the orchestrator validates LLM output against.
Each tool receives an open DB connection; tools never open their own.
"""
import sqlite3
from datetime import datetime, time, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app import db
from app.timeparse import resolve_time
from app.tools import ToolResult


class CreateReminderArgs(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    when: str = Field(min_length=1, max_length=100)  # raw time phrase, e.g. "tomorrow at 9 AM"


class ListRemindersArgs(BaseModel):
    range: Literal["today", "tomorrow", "upcoming", "all"] = "upcoming"


class CompleteReminderArgs(BaseModel):
    reminder_id: int = Field(ge=1)


def _format_local(value: datetime) -> str:
    """'Thu 15 Oct, 9:00 AM' (built by hand: Windows strftime has no %-I)."""
    hour12 = value.hour % 12 or 12
    am_pm = "AM" if value.hour < 12 else "PM"
    return f"{value:%a} {value.day} {value:%b}, {hour12}:{value:%M} {am_pm}"


def _local(reminder: db.Reminder, zone: ZoneInfo) -> datetime:
    return reminder.due_utc.astimezone(zone)


def create_reminder(conn: sqlite3.Connection, args: CreateReminderArgs, now: datetime, tz: str) -> ToolResult:
    """Resolve args.when via timeparse; on ambiguity return needs_clarification=True and save nothing.
    On success data={"reminder": {...id, title, due_local (ISO), tz}}."""
    title = " ".join(args.title.split())  # trim and collapse whitespace/newlines
    if not title:
        return ToolResult(ok=False, needs_clarification=True, message="What should the reminder say?")

    resolution = resolve_time(args.when, now, tz)
    if resolution.due is None:
        return ToolResult(ok=False, needs_clarification=True, message=resolution.clarification or "When?")

    due_local = resolution.due.astimezone(ZoneInfo(tz))
    reminder = db.insert_reminder(conn, title, due_local.astimezone(timezone.utc), tz)
    return ToolResult(
        ok=True,
        message=f"Reminder set: {reminder.title} — {_format_local(due_local)}",
        data={
            "reminder": {
                "id": reminder.id,
                "title": reminder.title,
                "due_local": due_local.isoformat(),
                "tz": tz,
            }
        },
    )


def list_reminders(conn: sqlite3.Connection, args: ListRemindersArgs, now: datetime, tz: str) -> ToolResult:
    """data={"reminders": [{id, title, due_local, status}, ...]}. Ranges use the user's local day boundaries."""
    zone = ZoneInfo(tz)
    today = now.astimezone(zone).date()

    if args.range in ("today", "tomorrow"):
        day = today if args.range == "today" else today + timedelta(days=1)
        start = datetime.combine(day, time(0), tzinfo=zone)  # local midnight
        end = datetime.combine(day + timedelta(days=1), time(0), tzinfo=zone)
        items = db.list_reminders(conn, start_utc=start, end_utc=end)
        label = "today" if args.range == "today" else "for tomorrow"
    elif args.range == "upcoming":
        items = db.list_reminders(conn, start_utc=now, status=db.STATUS_PENDING)
        label = "coming up"
    else:  # "all"
        items = db.list_reminders(conn)
        label = "at all"

    if not items:
        return ToolResult(ok=True, message=f"You have no reminders {label}.", data={"reminders": []})

    lines = []
    for r in items:
        done = " (done)" if r.status == db.STATUS_DONE else ""
        lines.append(f"#{r.id} {r.title} — {_format_local(_local(r, zone))}{done}")
    count = f"{len(items)} reminder" + ("s" if len(items) != 1 else "")
    heading = f"You have {count}" + ("" if args.range == "all" else f" {label}") + ":"
    return ToolResult(
        ok=True,
        message="\n".join([heading, *lines]),
        data={
            "reminders": [
                {"id": r.id, "title": r.title, "due_local": _local(r, zone).isoformat(), "status": r.status}
                for r in items
            ]
        },
    )


def complete_reminder(conn: sqlite3.Connection, args: CompleteReminderArgs, now: datetime, tz: str) -> ToolResult:
    reminder = db.get_reminder(conn, args.reminder_id)
    if reminder is None:
        return ToolResult(ok=False, message=f"I couldn't find reminder #{args.reminder_id}.")
    if reminder.status == db.STATUS_DONE:
        return ToolResult(ok=False, message=f"Reminder #{reminder.id} ({reminder.title}) is already done.")
    if not db.mark_done(conn, reminder.id):
        return ToolResult(ok=False, message=f"Reminder #{reminder.id} could not be marked done.")
    return ToolResult(
        ok=True,
        message=f"Marked done: {reminder.title}",
        data={"reminder": {"id": reminder.id, "title": reminder.title, "status": db.STATUS_DONE}},
    )
