"""One-time cleanup: removes the legacy `id` field from existing `threads`
documents in MongoDB.

Found to be a true duplicate by reading the actual pipeline code (not just
docs/tooltips, which were stale on this exact point, same as the earlier
emails-collection cleanup): `app.pipeline._upsert_thread` always set `id` to
the exact same value as `thread_id` (`document = {"id": thread_id,
"thread_id": thread_id, ...}`). `threads.id` is never used as a lookup/
foreign-key anywhere -- every one of the 13+ files that reference a thread
(`emails.thread_id`, `context_snapshots.thread_id`, `knowledge_items.thread_id`,
`commitments.thread_id`, `follow_ups.thread_id`, `meetings.thread_id`,
`reply_drafts.thread_id`, `calendar_actions.thread_id`, `people.open_threads`,
`thread_events.thread_id`) does so via `thread_id`, never `threads.id`. The
genuinely distinct field -- the real, permanent original Gmail/provider
thread id -- is `source_thread_id`, a separate field this script never
touches.

`id` was removed from `app.pipeline._upsert_thread` entirely -- any thread
created/updated from that point on never gets it written. This script only
cleans up documents that were created *before* that change and still carry
the now-legacy field.

Only ever touches the `id` key via `$unset` -- never any other field, never
inserts, never deletes a document.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `threads` documents currently
    # have id, prints a sample of affected thread_ids, writes nothing:
    python -m scripts.remove_thread_id_field --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.remove_thread_id_field --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `id` set -- a
    document without it is never touched (matched or not).
  - The update is exactly `{"$unset": {"id": ""}}`, asserted in code, not
    just by convention -- no other field is ever part of it. `thread_id`,
    `source_thread_id`, `message_ids`, and `source_message_ids` are never
    read or written by this script.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying `id` (must be 0) and
    re-counts the total `threads` document count (must be unchanged from
    before the write -- this script must never insert or delete a document).
  - Reversible in practice: `id` can always be recomputed as `thread_id`'s
    own value -- nothing unique is destroyed, only a redundant copy of data
    that still exists elsewhere on the same document.
  - Never touches the `emails` collection or any other collection -- this is
    a `threads`-only cleanup, independent of the earlier emails-collection
    migration.
"""
import argparse

from app.database.mongodb import get_client

_FILTER = {"id": {"$exists": True}}
_UNSET = {"$unset": {"id": ""}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy id field from threads documents"
    )
    parser.add_argument("--uri", required=True, help="MongoDB connection string (needs write access for this run)")
    parser.add_argument("--db", required=True, help="Database name (e.g. cos_sales_production_v1)")
    parser.add_argument(
        "--confirm", action="store_true",
        help="Actually perform the write. Without this flag, this is a dry run only.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    assert set(_UNSET.keys()) == {"$unset"} and set(_UNSET["$unset"].keys()) == {"id"}, (
        "Refusing to build an update touching anything other than id"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.threads.count_documents({})
    affected = list(db.threads.find(_FILTER, {"_id": 0, "thread_id": 1, "id": 1}))

    print(
        f"=== threads.id field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in threads: {total_before}")
    print(f"Documents currently carrying id: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has id. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['thread_id']}: id={doc.get('id')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset id only, {len(affected)} matching document(s)...")
    result = db.threads.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.threads.count_documents(_FILTER)
    total_after = db.threads.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have id after the write")
    if total_after != total_before:
        failures.append(
            f"threads document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with id: {remaining}")
    print(f"Total threads document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
