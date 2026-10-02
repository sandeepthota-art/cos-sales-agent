"""OBSOLETE / fully retired, kept only for history -- this script now has no
targets at all and is a guaranteed no-op if run.

It originally backfilled a human-readable internal id (`EML-nnn`/`THR-nnn`)
onto `emails`/`threads` documents that didn't have one yet. Both collections
have since gone through their own collection-by-collection schema cleanup and
had their `id` field removed entirely -- it was a true duplicate of
`message_id`/`thread_id` respectively in both cases, verified by searching
every reader/writer across the codebase:

  - `emails.id` -- removed, see `scripts/remove_email_id_record_id_date_fields.py`.
  - `threads.id` -- removed, see `scripts/remove_thread_id_field.py`.

Running this script's old logic against either collection today would be
actively harmful: it would assign a *new*, different id via `next_id` to any
document "missing" one, which is now *every* document (the field doesn't
exist at all anymore), and that new id would never match the document's own
`message_id`/`thread_id`. `_TARGETS` is therefore left empty below -- do not
add either collection back to it.

Per the approved ID Architecture Audit, this was purely additive: `thread_id`
-- the canonical source/dedup identifier -- was never read, renamed, replaced,
or rewritten by this script. Only a new `id` field was ever set, and only on a
document that didn't already have one.

Uses the exact same atomic `next_id()` counter (`app.entities.ids`, backed by
MongoDB's `find_one_and_update` on the `counters` collection) that already
generates every other internal id (`PER-nnn`, `PRJ-nnn`, etc.) -- no new id
mechanism was introduced for this. Assignment is deterministic and oldest-
first: emails ordered by `timestamp`, threads ordered by `last_message_at`
(the only stable, join-free ordering key already stored on a thread document),
so `EML-001`/`THR-001` are always the oldest records, not an arbitrary pick.

Usage (run from the repo root):
    # Dry run (default) -- reports how many documents in each collection are
    # still missing `id`, prints a sample, writes nothing:
    python -m scripts.backfill_email_thread_internal_ids --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.backfill_email_thread_internal_ids --uri "..." --db cos_sales_production_v1 --confirm

    # Backfill at most N documents per collection this run (e.g. to stage a
    # very large collection across several runs):
    python -m scripts.backfill_email_thread_internal_ids --uri "..." --db cos_sales_production_v1 --confirm --limit 500

Resumability is structural, not session-tracked. The filter this script uses
(`{"id": {"$exists": False}}`) only ever matches documents that still lack an
id -- so if a run is interrupted (crash, a `--limit` cutoff, Ctrl+C) partway
through, simply re-invoking the exact same command finds only the remaining
un-backfilled documents and continues from there. No separate `--resume
RUN_ID`/run-tracking mechanism is needed here, unlike `app/entity_migration.py`'s
heavier multi-stage migrations -- the ID Architecture Audit considered that
pattern and found it unnecessary for a job this simple (assign one missing
field, never touch anything else).

Safety:
  - Every write is scoped to exactly one document at a time, re-checking
    `{"id": {"$exists": False}}` in the update's own filter (not just at read
    time) -- a document that already has an id, or that another concurrent
    process just assigned one to, is structurally never overwritten, even
    under concurrent execution.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-verifies: each collection's total document count is
    unchanged (never inserts/deletes a document); zero duplicate `id` values
    exist anywhere in the collection (not just among newly-assigned ones);
    and, for an unlimited (`--limit` omitted) run, zero documents remain
    missing `id`.
"""
import argparse

from app.database.mongodb import get_client
from app.entities.ids import next_id

# (collection name, id prefix, key field used for logging/dedup, sort-by field)
# Empty: both former targets (emails, threads) have had `id` removed from
# their schema entirely -- see the module docstring. Never repopulate this.
_TARGETS: tuple[tuple[str, str, str, str], ...] = ()
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill THR- internal ids onto existing threads documents"
    )
    parser.add_argument("--uri", required=True, help="MongoDB connection string (needs write access for --confirm)")
    parser.add_argument("--db", required=True, help="Database name (e.g. cos_sales_production_v1)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually perform the write. Without this flag, this is a dry run only.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Backfill at most N documents per collection this run (default: all missing this run)",
    )
    return parser


def _backfill_collection(
    db, collection_name: str, prefix: str, key_field: str, sort_field: str, confirm: bool, limit: int | None
) -> list[str]:
    collection = db[collection_name]
    missing_filter = {"id": {"$exists": False}}

    total_before = collection.count_documents({})
    missing_count = collection.count_documents(missing_filter)

    print(
        f"\n=== {collection_name}: "
        f"({'CONFIRM: will write' if confirm else 'DRY RUN: no write'}) ==="
    )
    print(f"Total documents in {collection_name}: {total_before}")
    print(f"Documents currently missing id: {missing_count}")

    candidates: list[dict] = []
    if missing_count == 0:
        print("Nothing to do -- every document already has an id.")
    else:
        candidates = list(collection.find(missing_filter, {"_id": 0}).sort(sort_field, 1))
        if limit is not None:
            candidates = candidates[:limit]

        print(f"Will assign ids to {len(candidates)} of {missing_count} document(s) this run.")
        print(f"Sample (up to {_SAMPLE_SIZE}):")
        for doc in candidates[:_SAMPLE_SIZE]:
            print(f"  {doc[key_field]}")
        if len(candidates) > _SAMPLE_SIZE:
            print(f"  ... and {len(candidates) - _SAMPLE_SIZE} more")

    if not confirm:
        if candidates:
            print("Dry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return []

    # Verification (duplicate-id / count checks) always runs under --confirm,
    # even when there was nothing to backfill -- a pre-existing duplicate id
    # (from any cause) must never go unreported just because this run had no
    # new documents to assign.
    assigned = 0
    if candidates:
        print(f"Assigning ids, oldest-first by {sort_field}, via the atomic next_id counter...")
        for doc in candidates:
            new_id = next_id(db, prefix)
            result = collection.update_one(
                {key_field: doc[key_field], "id": {"$exists": False}},
                {"$set": {"id": new_id}},
            )
            # matched_count == 0 only if another process already assigned this exact
            # document an id between our read and this write -- never overwritten,
            # by construction of the filter above; the generated id is simply unused.
            if result.matched_count == 1:
                assigned += 1

    remaining = collection.count_documents(missing_filter)
    total_after = collection.count_documents({})
    all_ids = [
        d["id"] for d in collection.find({"id": {"$exists": True}}, {"_id": 0, "id": 1})
    ]
    duplicate_count = len(all_ids) - len(set(all_ids))

    failures: list[str] = []
    if total_after != total_before:
        failures.append(
            f"{collection_name} document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete a document)"
        )
    if duplicate_count:
        failures.append(f"{collection_name} has {duplicate_count} duplicate id value(s) after backfill")
    if limit is None and remaining != 0:
        failures.append(f"{remaining} document(s) in {collection_name} still missing id after an unlimited run")

    print(f"\n--- {collection_name} report ---")
    print(f"Assigned this run: {assigned}")
    print(f"Remaining missing id: {remaining}")
    print(f"Total document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    print(f"Duplicate id values: {duplicate_count}")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return failures


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    client = get_client(args.uri)
    db = client[args.db]

    print(f"=== EML-/THR- internal id backfill: db='{args.db}' ===")

    all_failures: list[str] = []
    for collection_name, prefix, key_field, sort_field in _TARGETS:
        all_failures.extend(
            _backfill_collection(db, collection_name, prefix, key_field, sort_field, args.confirm, args.limit)
        )

    return 1 if all_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
