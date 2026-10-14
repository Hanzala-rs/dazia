"""Reminder delivery.

Design: SQLite is the source of truth. An APScheduler interval job calls check_now() every
`interval_s` seconds; check_now() claims due reminders in the DB and hands each to `notify`.
This survives restarts, sleep and crashes without keeping per-reminder jobs in sync.
The same job also runs the optional daily briefing (BriefingJob), which is sent once per local day.
"""
import logging
from datetime import datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app import db
from app.tools.briefing import DailyBriefingArgs, daily_briefing

log = logging.getLogger(__name__)

# notify(reminder, missed) — raises if delivery failed (the claim is then released and retried)
NotifyFn = Callable[[db.Reminder, bool], None]

JOB_ID = "reminder-check"
BRIEFING_TITLE = "Daily briefing"


class BriefingJob:
    """Send the daily briefing once per local day, at or after `at`.
    If the laptop was off or asleep at that time, it is sent on the next check that day."""

    def __init__(self, db_path: str, tz: str, at: time, send: Callable[[str, str], None]):
        self.db_path = db_path
        self.tz = tz
        self.at = at
        self.send = send  # send(title, body), e.g. Notifier.briefing

    def __call__(self, now: datetime) -> bool:
        """Returns True if a briefing was sent."""
        zone = ZoneInfo(self.tz)
        local = now.astimezone(zone)
        if local.time() < self.at:
            return False
        start_of_day = datetime.combine(local.date(), time(0), tzinfo=zone)
        conn = db.connect(self.db_path)
        try:
            if db.has_notification_since(conn, db.KIND_BRIEFING, start_of_day):
                return False  # already sent today (survives restarts: the feed row is the record)
            body = daily_briefing(conn, DailyBriefingArgs(), now, self.tz).message
        finally:
            conn.close()
        self.send(BRIEFING_TITLE, body)
        log.info("Daily briefing sent")
        return True


class ReminderScheduler:
    def __init__(
        self,
        db_path: str,
        notify: NotifyFn,
        clock: Callable[[], datetime] | None = None,
        interval_s: int = 30,
        missed_after_s: int = 120,
        briefing: Callable[[datetime], bool] | None = None,
    ):
        """A reminder delivered more than `missed_after_s` after its due time counts as missed."""
        self.db_path = db_path
        self.notify = notify
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval_s = interval_s
        self.missed_after = timedelta(seconds=missed_after_s)
        self.briefing = briefing
        self._scheduler: BackgroundScheduler | None = None

    def check_now(self) -> int:
        """Deliver every due, unclaimed reminder once. Returns how many were delivered."""
        now = self.clock().astimezone(timezone.utc)
        conn = db.connect(self.db_path)
        try:
            delivered = 0
            for reminder in db.claim_due(conn, now):
                missed = (now - reminder.due_utc) > self.missed_after
                try:
                    self.notify(reminder, missed)
                except Exception:
                    # Ids only in logs: reminder titles are private.
                    log.exception("Delivering reminder #%s failed; will retry next check", reminder.id)
                    try:
                        db.release_claim(conn, reminder.id)
                    except Exception:
                        log.exception("Releasing reminder #%s failed", reminder.id)
                    continue
                delivered += 1
                log.info("Delivered reminder #%s (missed=%s)", reminder.id, missed)
            return delivered
        finally:
            conn.close()

    def _run_check(self) -> None:
        """Job wrapper: never let an error escape into the scheduler thread."""
        try:
            self.check_now()
        except Exception:
            log.exception("Reminder check failed")
        if self.briefing is not None:
            try:
                self.briefing(self.clock())
            except Exception:
                log.exception("Daily briefing failed; will retry next check")

    def start(self) -> None:
        """Run check_now() immediately (catches reminders missed while stopped), then every interval_s."""
        if self._scheduler is not None:
            return
        self._run_check()  # startup recovery, synchronous
        scheduler = BackgroundScheduler(timezone=timezone.utc)
        scheduler.add_job(
            self._run_check,
            "interval",
            seconds=self.interval_s,
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=None,  # after sleep, run once late rather than skip
        )
        scheduler.start()
        self._scheduler = scheduler

    def shutdown(self) -> None:
        """Stop the background job. Safe to call more than once."""
        scheduler, self._scheduler = self._scheduler, None
        if scheduler is not None:
            scheduler.shutdown(wait=False)
