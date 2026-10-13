"""Marks old backfilled mail as history-only: no classification, ever.
Deliberately a local script, NOT an MCP tool -- never reachable from a
scheduled task. Dry-run by default; prints message_id and sort_key only,
never subject/body content.

A history_only=true document is permanently excluded from both claim
tools (app/db.py::claim_next_unclassified, used by single and batch
claims) -- label_applied is never touched, no Gmail label is ever applied,
no LLM/Claude judgment is ever spent on it.

Usage:
    python scripts/mark_history_only.py --before 2026-09-01            # dry run
    python scripts/mark_history_only.py --before 2026-09-01 --apply    # actually marks
"""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import RawEmailDumpRepository, get_client, initialize_database


def find_docs_before_cutoff(repo: RawEmailDumpRepository, cutoff_iso: str) -> list[dict]:
    """Every raw_emails_dump document with sort_key < cutoff_iso, not
    already marked history_only. Returns id/sort_key only -- no content."""
    docs = repo.find_many({"sort_key": {"$lt": cutoff_iso}, "history_only": {"$ne": True}})
    return [{"message_id": d["message_id"], "sort_key": d["sort_key"]} for d in docs]


def apply_history_only(repo: RawEmailDumpRepository, message_ids: list[str]) -> None:
    if message_ids:
        repo.update_many_by_key({"message_id": {"$in": message_ids}}, {"history_only": True})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--before",
        required=True,
        help="YYYY-MM-DD -- mark every document with sort_key before this date (UTC midnight) as history_only.",
    )
    parser.add_argument("--apply", action="store_true", help="Actually mark them. Default is dry-run.")
    args = parser.parse_args()

    cutoff_iso = datetime.strptime(args.before, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()

    settings = get_settings()
    db = initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)
    repo = RawEmailDumpRepository(db)

    matches = find_docs_before_cutoff(repo, cutoff_iso)
    if not matches:
        print(f"No documents found with sort_key before {args.before}.")
        return

    verb = "Marking" if args.apply else "Would mark"
    print(f"{verb} {len(matches)} document(s) as history_only (sort_key < {args.before}):")
    for doc in matches:
        print(f"  {doc['message_id']}")

    if not args.apply:
        print("Dry run -- no changes made. Re-run with --apply to mark them.")
        return

    apply_history_only(repo, [d["message_id"] for d in matches])
    print(f"Marked {len(matches)} document(s) as history_only.")


if __name__ == "__main__":
    main()
