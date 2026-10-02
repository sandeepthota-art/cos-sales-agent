"""One-time cleanup: removes the legacy `id`, `record_id`, and `date` fields
from existing `emails` documents in MongoDB.

All three were found to be true duplicates/near-duplicates by reading the
actual pipeline code (not just docs/tooltips, which were stale on this exact
point): `id` and `record_id` always held the same value as `message_id`
(`app.pipeline.ingest_raw_email` reassigns `message_id` to the canonical
`EML-nnn` value; `record_id` is set from `message_id` in
`app.mcp.tools.persist_email_analysis`); `date` was always `timestamp`'s
date-only component (`EmailRepository.set_entity_metadata`). None of the
three is read as a lookup/foreign key anywhere -- the query/evidence system
uses `message_id` directly for emails (`app/query/service.py`), and the
canonical, permanent original Gmail id lives in `source_message_id`, a
separate field this script never touches.

All three fields were removed from `ingest_raw_email`/`set_entity_metadata`
entirely -- any email processed from that point on never gets any of them
written. This script only cleans up documents that were processed *before*
that change and still carry one or more of the now-legacy fields.

Only ever touches the `id`/`record_id`/`date` keys via `$unset` -- never any
other field, never inserts, never deletes a document.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `emails` documents currently have
    # id, record_id, and/or date, prints a sample of affected message_ids,
    # writes nothing:
    python -m scripts.remove_email_id_record_id_date_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.remove_email_id_record_id_date_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `id`,
    `record_id`, and/or `date` set -- a document without any of the three is
    never touched (matched or not).
  - The update is exactly `{"$unset": {"id": "", "record_id": "", "date": ""}}`,
    asserted in code, not just by convention -- no other field is ever part
    of it. `message_id`, `thread_id`, `source_message_id`, and
    `source_thread_id` are never read or written by this script.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying any of the three fields
    (must be 0) and re-counts the total `emails` document count (must be
    unchanged from before the write -- this script must never insert or
    delete a document).
  - Reversible in practice: `id` and `record_id` can always be recomputed as
    `message_id`'s own value, and `date` as `timestamp`'s date component --
    nothing unique is destroyed, only a redundant copy of data that still
    exists elsewhere on the same document.
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("id", "record_id", "date")
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy id/record_id/date fields from emails documents"
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

    assert set(_UNSET.keys()) == {"$unset"} and set(_UNSET["$unset"].keys()) == set(_FIELDS), (
        "Refusing to build an update touching anything other than id/record_id/date"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.emails.count_documents({})
    affected = list(
        db.emails.find(_FILTER, {"_id": 0, "message_id": 1, "id": 1, "record_id": 1, "date": 1})
    )

    print(
        f"=== id/record_id/date field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in emails: {total_before}")
    print(f"Documents currently carrying id, record_id, and/or date: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has id, record_id, or date. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(
            f"  {doc['message_id']}: id={doc.get('id')!r} record_id={doc.get('record_id')!r} "
            f"date={doc.get('date')!r}"
        )
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset id/record_id/date only, {len(affected)} matching document(s)...")
    result = db.emails.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.emails.count_documents(_FILTER)
    total_after = db.emails.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have id, record_id, and/or date after the write")
    if total_after != total_before:
        failures.append(
            f"emails document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with id/record_id/date: {remaining}")
    print(f"Total emails document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
