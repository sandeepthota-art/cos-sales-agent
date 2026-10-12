"""Local-only recovery for emails stuck at the retry cap (label_attempts
>= _MAX_LABEL_ATTEMPTS). Deliberately NOT an MCP tool -- never reachable
from a scheduled task, only from a human running this script directly.
Dry-run by default; prints message_id and attempt count only, never
subject/body content.

Usage:
    python scripts/reset_stuck.py            # dry run, lists what would reset
    python scripts/reset_stuck.py --apply     # actually resets
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tools
from app.config import get_settings
from app.db import RawEmailDumpRepository, get_client, initialize_database


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually reset. Default is dry-run.")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    settings = get_settings()
    db = initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)
    stuck = tools.get_stuck_emails(db, limit=args.limit)

    if not stuck:
        print("No stuck emails found.")
        return

    verb = "Resetting" if args.apply else "Would reset"
    print(f"{verb} {len(stuck)} stuck email(s):")
    for doc in stuck:
        print(f"  {doc['message_id']} (attempts={doc['label_attempts']})")

    if not args.apply:
        print("Dry run -- no changes made. Re-run with --apply to reset.")
        return

    repo = RawEmailDumpRepository(db)
    for doc in stuck:
        repo.upsert_by_key(
            {"message_id": doc["message_id"]},
            {"label_attempts": 0, "label_error": None, "label_claimed_at": "", "claim_id": None},
        )
    print(f"Reset {len(stuck)} email(s).")


if __name__ == "__main__":
    main()
