"""Generic reminder scheduler -- deliberately separate from the Gmail ingestion
scheduler (app/scheduler.py). Ingestion and reminders are different concerns with
independent polling needs; this is not the ingestion scheduler wearing a second hat.

Reads PersonalItem documents. PersonalItem is the existing entity with both a
date_or_deadline field and a status field already on it (app/entities/models.py) --
FollowUp has neither (it is a pure structural commitment-tracking marker, see
app/entities/resolution.py:derive_follow_up) and is NOT part of this mechanism.

A personal item becomes "due" once its date_or_deadline has passed and its status is
still "open". Triggering a reminder means writing a structured log line -- no email/
Slack/desktop-notification channel exists anywhere in this codebase, and building one
was explicitly out of scope (no Gmail API, no new external integrations) -- and then
setting status="reminded" so it is never delivered a second time. This is idempotent
by construction: a "reminded" item never matches the due-query again, exactly
mirroring how FolderSource's all_completed flag prevents a poll from repeating
already-done work.

CLI usage:
    python -m app.reminders           # poll forever, REMINDER_INTERVAL_MINUTES apart
    python -m app.reminders --once    # run exactly one poll cycle and exit
"""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.database.repositories import PersonalItemRepository

logger = logging.getLogger(__name__)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class ReminderScheduler:
    def __init__(self, db: Database):
        self._repo = PersonalItemRepository(db)

    def run_once(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """Finds due, untriggered personal items, logs a reminder for each, and marks
        them reminded so they're never delivered twice. Never raises: a failure
        parsing/triggering one item is logged and isolated so the rest of the cycle
        still runs. Returns the items reminded this cycle (possibly empty)."""
        now = now or datetime.now(timezone.utc)
        triggered: list[dict[str, Any]] = []
        try:
            candidates = self._repo.find_many({"status": "open"})
        except Exception:
            logger.exception("reminder poll cycle failed during lookup; will retry next cycle")
            return triggered

        for item in candidates:
            raw_due = item.get("date_or_deadline")
            if not raw_due:
                continue
            try:
                due_at = _parse_iso(raw_due)
            except ValueError:
                logger.warning(
                    "reminder scheduler: unparseable date_or_deadline on %s: %r",
                    item.get("id"), raw_due,
                )
                continue
            if due_at > now:
                continue

            try:
                logger.info(
                    "REMINDER id=%s type=%s description=%r due_at=%s",
                    item.get("id"), item.get("type"), item.get("description"), raw_due,
                )
                self._repo.upsert_by_key({"id": item["id"]}, {**item, "status": "reminded"})
                triggered.append(item)
            except Exception:
                logger.exception(
                    "reminder scheduler: failed to trigger reminder for %s; will retry next cycle",
                    item.get("id"),
                )
                continue

        logger.info(
            "reminder poll cycle complete: open=%d triggered=%d", len(candidates), len(triggered)
        )
        return triggered

    def run_forever(self, interval_seconds: int) -> None:
        """Plain time.sleep()-based loop, matching app.scheduler.Scheduler.run_forever.
        Single sequential loop -- no overlapping executions by construction. An
        unexpected exception from run_once() is logged and the loop continues;
        KeyboardInterrupt propagates for a clean Ctrl+C."""
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("unexpected error in reminder poll cycle; continuing")
            time.sleep(interval_seconds)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent -- reminder scheduler")
    parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one poll cycle and exit, instead of polling forever",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)
    scheduler = ReminderScheduler(db)

    if args.once:
        scheduler.run_once()
        return 0

    interval_seconds = settings.reminder_interval_minutes * 60
    logger.info("reminder scheduler starting: interval_minutes=%d", settings.reminder_interval_minutes)
    try:
        scheduler.run_forever(interval_seconds)
    except KeyboardInterrupt:
        logger.info("reminder scheduler stopped (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
