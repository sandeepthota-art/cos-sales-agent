"""Loads Phase 5's golden eval cases (labeling + context) into the eval
database through ingest_raw_email_only -- the SAME function the real
pipeline uses -- so ingestion hygiene (idempotency, thread resolution,
default field creation) is exercised too, not bypassed with a direct
insert.

Refuses to run against anything but a database whose name ends with
'_eval' (see scripts/_common.py). Dry-run by default; prints case_ids
and message counts only, never body content.

Usage:
    python scripts/load_golden.py            # dry run
    python scripts/load_golden.py --apply    # actually load
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.db import get_client, initialize_database
from app.models import parse_email
from app.tools import ingest_raw_email_only
from eval.eval_models import ContextCase, GoldenMessage, LabelingCase, load_context_cases, load_labeling_cases, to_email_doc
from scripts._common import require_eval_database

LABELING_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden" / "labeling_cases.jsonl"
CONTEXT_PATH = Path(__file__).resolve().parent.parent / "eval" / "golden" / "context_cases.jsonl"


def collect_messages(cases: list[LabelingCase] | list[ContextCase]) -> list[tuple[str, GoldenMessage]]:
    return [(case.case_id, message) for case in cases for message in case.messages]


def load_messages(db, settings, messages: list[tuple[str, GoldenMessage]]) -> int:
    count = 0
    for _case_id, message in messages:
        email = parse_email(to_email_doc(message))
        ingest_raw_email_only(db, email, settings)
        count += 1
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually load. Default is dry-run.")
    parser.add_argument(
        "--allow-remote-eval",
        action="store_true",
        help="Allow a non-local MONGODB_URI host. Off by default -- eval runs are local-only.",
    )
    args = parser.parse_args()

    settings = get_settings()
    require_eval_database(settings.mongodb_database, uri=settings.mongodb_uri, allow_remote=args.allow_remote_eval)

    labeling_cases = load_labeling_cases(LABELING_PATH)
    context_cases = load_context_cases(CONTEXT_PATH)
    labeling_messages = collect_messages(labeling_cases)
    context_messages = collect_messages(context_cases)

    print(f"Labeling cases: {len(labeling_cases)} ({len(labeling_messages)} messages)")
    print(f"Context cases: {len(context_cases)} ({len(context_messages)} messages)")
    print(f"Total messages: {len(labeling_messages) + len(context_messages)}")

    if not args.apply:
        print(f"Dry run -- no changes made. Target database would be {settings.mongodb_database!r}.")
        print("Re-run with --apply to load them.")
        return

    db = initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)
    loaded = load_messages(db, settings, labeling_messages) + load_messages(db, settings, context_messages)
    print(f"Loaded {loaded} messages into {settings.mongodb_database!r}.")


if __name__ == "__main__":
    main()
