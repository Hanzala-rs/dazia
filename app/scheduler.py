"""Reminder delivery.

Design: SQLite is the source of truth. An APScheduler interval job calls check_now() every
`interval_s` seconds; check_now() claims due reminders in the DB and hands each to `notify`.
This survives restarts, sleep and crashes without keeping per-reminder jobs in sync.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from apscheduler.schedulers.background import BackgroundScheduler

from app import db

log = logging.getLogger(__name__)

# notify(reminder, missed) — raises if delivery failed (the claim is then released and retried)
NotifyFn = Callable[[db.Reminder, bool], None]

JOB_ID = "reminder-check"


class ReminderScheduler:
    def __init__(
        self,
        db_path: str,
        notify: NotifyFn,
        clock: Callable[[], datetime] | None = None,
        interval_s: int = 30,
        missed_after_s: int = 120,
    ):
        """A reminder delivered more than `missed_after_s` after its due time counts as missed."""
        self.db_path = db_path
        self.notify = notify
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.interval_s = interval_s
        self.missed_after = timedelta(seconds=missed_after_s)
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
