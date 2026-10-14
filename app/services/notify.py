"""Reminder notifications: in-app feed (SQLite) + Windows toast.

The scheduler calls a Notifier for each due reminder:
    1. write a row to the notifications feed (errors propagate so the scheduler can retry)
    2. show a Windows toast (best effort: failures are logged and ignored)

Security: the reminder title is untrusted text (typed by the user or produced by the LLM).
It is NEVER put inside the PowerShell script. The script below is a fixed string; the text
travels in environment variables and is inserted as XML text nodes, so it can't run as code
or change the toast's XML structure.
"""
import logging
import os
import subprocess
import sys
from datetime import datetime
from typing import Callable
from zoneinfo import ZoneInfo

from app import db

log = logging.getLogger(__name__)

TOAST_TIMEOUT_S = 10.0
ENV_TITLE = "DAZIA_TOAST_TITLE"
ENV_BODY = "DAZIA_TOAST_BODY"

# Static script: it only reads $env:DAZIA_TOAST_TITLE / $env:DAZIA_TOAST_BODY.
# The app id is Windows PowerShell's own AUMID, so no app registration is needed.
TOAST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).AppendChild($xml.CreateTextNode([string]$env:DAZIA_TOAST_TITLE)) | Out-Null
$texts.Item(1).AppendChild($xml.CreateTextNode([string]$env:DAZIA_TOAST_BODY)) | Out-Null
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""

# A runner takes (command args, environment, timeout seconds) and returns the exit code.
Runner = Callable[[list[str], dict[str, str], float], int]


def run_powershell(args: list[str], env: dict[str, str], timeout: float) -> int:
    """Default runner: start PowerShell hidden (no console window flashes up)."""
    if sys.platform != "win32":
        raise OSError("toasts are only supported on Windows")
    result = subprocess.run(
        args,
        env=env,
        timeout=timeout,
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result.returncode


def format_due(due_utc: datetime, tz: str) -> str:
    """e.g. 'Thu 15 Oct, 9:00 AM' in the given zone. 12-hour time is built by hand
    because Windows strftime has no %-I (hour without a leading zero)."""
    local = due_utc.astimezone(ZoneInfo(tz))
    hour12 = local.hour % 12 or 12
    am_pm = "AM" if local.hour < 12 else "PM"
    return f"{local.strftime('%a')} {local.day} {local.strftime('%b')}, {hour12}:{local.minute:02d} {am_pm}"


class Notifier:
    def __init__(self, db_path: str, tz: str, toast: bool = True, runner: Runner = run_powershell):
        self.db_path = db_path
        self.tz = tz
        self.toast = toast
        self.runner = runner

    def __call__(self, reminder: db.Reminder, missed: bool) -> None:
        """Record a feed entry (raise if that fails), then show a Windows toast (log and continue on failure)."""
        title = "Missed reminder" if missed else "Reminder"
        kind = db.KIND_MISSED if missed else db.KIND_REMINDER
        body = f"{reminder.title} — due {format_due(reminder.due_utc, self.tz)}"

        conn = db.connect(self.db_path)
        try:
            db.add_notification(conn, title, body, kind, reminder_id=reminder.id)
        finally:
            conn.close()

        if self.toast:
            self._show_toast(f"reminder {reminder.id}", title, body)

    def briefing(self, title: str, body: str) -> None:
        """Record the daily briefing in the feed (raise if that fails), then show a toast (best effort)."""
        conn = db.connect(self.db_path)
        try:
            db.add_notification(conn, title, body, db.KIND_BRIEFING)
        finally:
            conn.close()
        if self.toast:
            self._show_toast("briefing", title, body)

    def _show_toast(self, label: str, title: str, body: str) -> None:
        # Untrusted text goes in env vars only; the script itself never changes.
        env = dict(os.environ)
        env[ENV_TITLE] = title
        env[ENV_BODY] = body
        args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", TOAST_SCRIPT]
        try:
            code = self.runner(args, env, TOAST_TIMEOUT_S)
        except Exception as exc:  # timeout, not Windows, PowerShell missing, ...
            # Log only the error type, never the reminder text.
            log.warning("Toast for %s failed: %s", label, type(exc).__name__)
            return
        if code != 0:
            log.warning("Toast for %s failed: PowerShell exit code %s", label, code)
