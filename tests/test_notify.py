"""Notifier tests. The DB functions and the PowerShell runner are fakes, so these tests
need neither the real notifications table nor Windows toasts."""
import logging
import subprocess
from datetime import datetime, timezone

import pytest

from app import db
from app.services import notify
from app.services.notify import Notifier

TITLE = "Call Ali'; Remove-Item C:\\ -Recurse #"  # hostile-looking title: must never reach the script


class FakeConn:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


@pytest.fixture
def feed(monkeypatch):
    """Replace db.connect / db.add_notification and record what was written."""
    state = {"rows": [], "conns": []}

    def fake_connect(path):
        conn = FakeConn()
        state["conns"].append((path, conn))
        return conn

    def fake_add(conn, title, body, kind, reminder_id=None):
        state["rows"].append({"title": title, "body": body, "kind": kind, "reminder_id": reminder_id})

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "add_notification", fake_add)
    return state


class FakeRunner:
    def __init__(self, result=0):
        self.result = result
        self.calls = []

    def __call__(self, args, env, timeout):
        self.calls.append((args, env, timeout))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_reminder(due=datetime(2026, 10, 15, 4, 0, tzinfo=timezone.utc)):
    return db.Reminder(42, TITLE, due, "Asia/Karachi", db.STATUS_PENDING, due, None)


def test_on_time_feed_row(feed):
    runner = FakeRunner()
    Notifier("test.db", "Asia/Karachi", runner=runner)(make_reminder(), missed=False)

    # 04:00 UTC = 9:00 AM in Karachi (UTC+5)
    assert feed["rows"] == [{
        "title": "Reminder", "body": f"{TITLE} — due Thu 15 Oct, 9:00 AM",
        "kind": db.KIND_REMINDER, "reminder_id": 42,
    }]
    path, conn = feed["conns"][0]
    assert path == "test.db" and conn.closed


def test_missed_feed_row_and_pm_time(feed):
    due = datetime(2026, 10, 15, 12, 5, tzinfo=timezone.utc)  # 5:05 PM Karachi
    Notifier("test.db", "Asia/Karachi", toast=False)(make_reminder(due), missed=True)
    row = feed["rows"][0]
    assert row["title"] == "Missed reminder"
    assert row["kind"] == db.KIND_MISSED
    assert row["body"].endswith("due Thu 15 Oct, 5:05 PM")


@pytest.mark.parametrize("hour_utc, expected", [(19, "12:00 AM"), (7, "12:00 PM")])
def test_midnight_and_noon(hour_utc, expected):
    due = datetime(2026, 10, 15, hour_utc, 0, tzinfo=timezone.utc)
    assert notify.format_due(due, "Asia/Karachi").endswith(expected)


def test_db_error_propagates_and_skips_toast(feed, monkeypatch):
    def broken_add(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "add_notification", broken_add)
    runner = FakeRunner()
    with pytest.raises(RuntimeError):
        Notifier("test.db", "Asia/Karachi", runner=runner)(make_reminder(), missed=False)
    assert runner.calls == []
    assert feed["conns"][0][1].closed  # connection closed even on error


def test_toast_text_passed_via_env_not_script(feed):
    runner = FakeRunner()
    Notifier("test.db", "Asia/Karachi", runner=runner)(make_reminder(), missed=False)

    assert len(runner.calls) == 1
    args, env, timeout = runner.calls[0]
    assert args[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
    script = args[4]
    assert script == notify.TOAST_SCRIPT  # static script, unchanged
    assert TITLE not in script and "Call Ali" not in script
    assert "$env:DAZIA_TOAST_TITLE" in script and "$env:DAZIA_TOAST_BODY" in script
    assert env["DAZIA_TOAST_TITLE"] == "Reminder"
    assert TITLE in env["DAZIA_TOAST_BODY"]
    assert 0 < timeout <= 30


@pytest.mark.parametrize("result", [1, subprocess.TimeoutExpired("powershell.exe", 10), OSError("not Windows")])
def test_toast_failure_does_not_raise(feed, caplog, result):
    runner = FakeRunner(result)
    with caplog.at_level(logging.WARNING, logger="app.services.notify"):
        Notifier("test.db", "Asia/Karachi", runner=runner)(make_reminder(), missed=False)
    assert len(feed["rows"]) == 1  # feed still written
    assert "Toast for reminder 42 failed" in caplog.text
    assert "Call Ali" not in caplog.text  # no message content in logs


def test_toast_disabled_skips_runner(feed):
    runner = FakeRunner()
    Notifier("test.db", "Asia/Karachi", toast=False, runner=runner)(make_reminder(), missed=False)
    assert runner.calls == []
    assert len(feed["rows"]) == 1
