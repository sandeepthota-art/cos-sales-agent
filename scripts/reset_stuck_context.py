"""Resets context_attempts/context_last_error for raw-dumped messages that
tripped the context-extraction circuit breaker (see get_stuck_context),
making them eligible for claim_next_thread_for_context again. Dry-run by
default; prints message_id and attempt counts only, never email content.

Local script, NOT an MCP tool -- never reachable from a scheduled task.

Usage:
    python scripts/reset_stuck_context.py            # dry run
    python scripts/reset_stuck_context.py --apply    # actually reset
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.context_config import MAX_CONTEXT_ATTEMPTS
from app.db import RawEmailDumpRepository, get_client, initialize_database


def find_stuck(repo: RawEmailDumpRepository) -> list[dict]:
    docs = repo.find_many({"context_attempts": {"$gte": MAX_CONTEXT_ATTEMPTS}})
    return [{"message_id": d["message_id"], "context_attempts": d["context_attempts"]} for d in docs]


def apply_reset(repo: RawEmailDumpRepository, message_ids: list[str]) -> None:
    if message_ids:
        repo.update_many_by_key(
            {"message_id": {"$in": message_ids}},
            {"context_attempts": 0, "context_last_error": None, "context_claim_id": None, "context_claimed_at": ""},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually reset. Default is dry-run.")
    args = parser.parse_args()

    settings = get_settings()
    db = initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)
    repo = RawEmailDumpRepository(db)

    stuck = find_stuck(repo)
    if not stuck:
        print("No stuck context messages found.")
        return

    verb = "Resetting" if args.apply else "Would reset"
    print(f"{verb} {len(stuck)} message(s):")
    for doc in stuck:
        print(f"  {doc['message_id']} (context_attempts={doc['context_attempts']})")

    if not args.apply:
        print("Dry run -- no changes made. Re-run with --apply to reset them.")
        return

    apply_reset(repo, [d["message_id"] for d in stuck])
    print(f"Reset {len(stuck)} message(s).")


if __name__ == "__main__":
    main()
