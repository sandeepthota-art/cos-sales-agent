"""One-time cleanup: permanently deletes leftover diagnostic/test documents from
MongoDB -- any document whose `source_record` or `thread_id` field matches
`test_diag_*` (e.g. `test_diag_003`), such as the `CMT-001` commitment and its
derived `FUP-001` follow-up created by a prior diagnostic run. These were
never produced by the real Gmail ingestion pipeline, which always stamps
`source_record`/`thread_id` with a genuine Gmail message/thread ID, never a
`test_diag_` label.

Two separate fields are checked because different entities carry the
diagnostic marker under different keys: `Commitment` has `source_record` but
no `thread_id`; `FollowUp` has `thread_id` but no `source_record`.

Unlike the other `scripts/remove_*` cleanups in this repo, this one deletes
whole documents rather than unsetting a field -- there is no legitimate data
to preserve on a diagnostic-only record.

Scans every collection in the database (not just `commitments`), since a
diagnostic run could have written a `test_diag_*` record into any collection.

Usage (run from the repo root):
    # Dry run (default) -- reports every test_diag_* document found, across
    # every collection, with its full contents. Deletes nothing:
    python -m scripts.delete_test_diagnostic_records --uri "..." --db cos_sales_production_v1

    # Actually perform the deletion, after reviewing the dry-run output:
    python -m scripts.delete_test_diagnostic_records --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents whose `source_record` or
    `thread_id` field starts with the literal string `test_diag_` -- a
    document without that exact prefix in either field is never touched
    (matched or not).
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After deleting, re-counts documents still matching the filter in each
    collection (must be 0) and confirms the collection's total document count
    dropped by exactly the number deleted (never more, never less).
"""
import argparse

from app.database.mongodb import get_client

_COLLECTIONS = (
    "emails", "threads", "context_snapshots", "knowledge_items", "reply_drafts",
    "calendar_actions", "processing_runs", "entities", "opportunities",
    "activities", "counters", "people", "organizations", "projects",
    "commitments", "follow_ups", "meetings", "personal_items",
    "ingested_files", "migration_runs",
)
_TEST_DIAG_REGEX = {"$regex": "^test_diag_"}
_FILTER = {"$or": [{"source_record": _TEST_DIAG_REGEX}, {"thread_id": _TEST_DIAG_REGEX}]}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Delete leftover test_diag_* diagnostic documents from every collection"
    )
    parser.add_argument("--uri", required=True, help="MongoDB connection string (needs write access for this run)")
    parser.add_argument("--db", required=True, help="Database name (e.g. cos_sales_production_v1)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually perform the deletion. Without this flag, this is a dry run only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    assert set(_FILTER.keys()) == {"$or"} and {tuple(clause.keys()) for clause in _FILTER["$or"]} == {
        ("source_record",), ("thread_id",)
    }, "Refusing to build a filter matching anything other than source_record/thread_id"

    client = get_client(args.uri)
    db = client[args.db]

    print(
        f"=== test_diag_* diagnostic record deletion: db='{args.db}' "
        f"({'CONFIRM: will delete' if args.confirm else 'DRY RUN: no delete'}) ===\n"
    )

    per_collection_before: dict[str, int] = {}
    per_collection_affected: dict[str, list[dict]] = {}
    total_affected = 0

    for name in _COLLECTIONS:
        collection = db[name]
        before = collection.count_documents({})
        affected = list(collection.find(_FILTER))
        per_collection_before[name] = before
        if affected:
            per_collection_affected[name] = affected
            total_affected += len(affected)

    if not total_affected:
        print("Nothing to do -- no document in any collection has a test_diag_* source_record or thread_id. Exiting.")
        return 0

    print(f"Documents matching test_diag_* across all collections: {total_affected}\n")
    for name, affected in per_collection_affected.items():
        print(f"  {name}: {len(affected)} document(s)")
        for doc in affected[:_SAMPLE_SIZE]:
            print(f"    {doc}")
        if len(affected) > _SAMPLE_SIZE:
            print(f"    ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no delete performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting delete_many on {len(per_collection_affected)} collection(s)...")
    failures: list[str] = []
    total_deleted = 0

    for name, affected in per_collection_affected.items():
        collection = db[name]
        expected_after = per_collection_before[name] - len(affected)

        result = collection.delete_many(_FILTER)
        total_deleted += result.deleted_count
        print(f"  {name}: delete_many result: deleted={result.deleted_count}")

        remaining = collection.count_documents(_FILTER)
        total_after = collection.count_documents({})

        if remaining != 0:
            failures.append(f"{name}: {remaining} document(s) still match test_diag_* after the delete")
        if total_after != expected_after:
            failures.append(
                f"{name}: document count mismatch: before={per_collection_before[name]} "
                f"after={total_after} expected={expected_after}"
            )

    print("\n=== Report ===")
    print(f"Documents deleted: {total_deleted}")
    print(f"Collections touched: {len(per_collection_affected)}")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
