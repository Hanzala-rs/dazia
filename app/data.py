"""Back up or wipe DAZIA's data (Phase 10). Safe to run while DAZIA is running.

    .venv/Scripts/python -m app.data backup            -> backups/dazia-YYYYMMDD-HHMMSS.db
    .venv/Scripts/python -m app.data backup --dir D:/  -> another folder (e.g. a USB drive)
    .venv/Scripts/python -m app.data wipe              -> delete all reminders and notifications (asks first)
"""
import argparse
from datetime import datetime
from pathlib import Path

from app import db
from app.config import settings

CONFIRM_WORD = "DELETE"


def backup(db_path: str, dest_dir: str) -> Path:
    folder = Path(dest_dir)
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"dazia-{datetime.now():%Y%m%d-%H%M%S}.db"
    if dest.exists():
        raise SystemExit(f"{dest} already exists; try again in a second.")
    conn = db.connect(db_path)
    try:
        db.backup_to(conn, str(dest))
    finally:
        conn.close()
    return dest


def wipe(db_path: str, answer: str) -> dict[str, int] | None:
    """Delete user data only if `answer` is exactly CONFIRM_WORD. Returns deleted counts, or None if cancelled."""
    if answer.strip() != CONFIRM_WORD:
        return None
    conn = db.connect(db_path)
    try:
        return db.wipe_user_data(conn)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app.data", description="Back up or wipe DAZIA's data.")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("backup", help="copy the database to a timestamped file")
    b.add_argument("--dir", default="backups", help="folder for the backup (default: backups)")
    sub.add_parser("wipe", help="delete all reminders and notifications (asks for confirmation)")
    args = parser.parse_args()

    if not Path(settings.db_path).exists():
        raise SystemExit(f"No database at {settings.db_path} (DAZIA_DB_PATH). Nothing to do.")

    if args.command == "backup":
        dest = backup(settings.db_path, args.dir)
        print(f"Backup saved: {dest}")
        print("To restore: stop DAZIA, copy this file over your database file, start DAZIA.")
        return

    conn = db.connect(settings.db_path)
    try:
        counts = db.count_user_data(conn)
    finally:
        conn.close()
    print(f"This permanently deletes {counts['reminders']} reminder(s) and {counts['notifications']} "
          f"notification(s) from {settings.db_path}. Login sessions are kept.")
    print("Make a backup first if you might want them back: python -m app.data backup")
    deleted = wipe(settings.db_path, input(f"Type {CONFIRM_WORD} to continue: "))
    if deleted is None:
        print("Cancelled. Nothing was deleted.")
        return
    print(f"Deleted {deleted['reminders']} reminder(s) and {deleted['notifications']} notification(s).")
    print("If DAZIA is running, restart it to also clear the chat it remembers in memory.")


if __name__ == "__main__":
    main()
