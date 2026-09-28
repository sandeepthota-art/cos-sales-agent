"""One-time cleanup: removes the legacy `confidence` and `priority` fields from
existing `emails` documents in MongoDB.

Both fields were removed from `EmailAnalysis`, the extraction prompt, and
`EmailRepository.set_entity_metadata` (see docs/DATA_DICTIONARY.md) -- any email
processed from that point on never gets either field written. This script only
cleans up documents that were processed *before* that change and still carry
the now-legacy fields.

Only ever touches the `confidence`/`priority` keys via `$unset` -- never any
other field, never inserts, never deletes a document.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `emails` documents currently have
    # confidence and/or priority, prints a sample of affected message_ids,
    # writes nothing:
    python -m scripts.remove_email_confidence_priority_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.remove_email_confidence_priority_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `confidence` and/or
    `priority` set -- a document without either is never touched (matched or not).
  - The update is exactly `{"$unset": {"confidence": "", "priority": ""}}`,
    asserted in code, not just by convention -- no other field is ever part of it.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying either field (must be 0)
    and re-counts the total `emails` document count (must be unchanged from
    before the write -- this script must never insert or delete a document).
"""
import argparse

from app.database.mongodb import get_client

_FILTER = {"$or": [{"confidence": {"$exists": True}}, {"priority": {"$exists": True}}]}
_UNSET = {"$unset": {"confidence": "", "priority": ""}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy confidence/priority fields from emails documents"
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

    assert set(_UNSET.keys()) == {"$unset"} and set(_UNSET["$unset"].keys()) == {"confidence", "priority"}, (
        "Refusing to build an update touching anything other than confidence/priority"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.emails.count_documents({})
    affected = list(
        db.emails.find(_FILTER, {"_id": 0, "message_id": 1, "confidence": 1, "priority": 1})
    )

    print(
        f"=== confidence/priority field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in emails: {total_before}")
    print(f"Documents currently carrying confidence and/or priority: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has confidence or priority. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['message_id']}: confidence={doc.get('confidence')!r} priority={doc.get('priority')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset confidence/priority only, {len(affected)} matching document(s)...")
    result = db.emails.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.emails.count_documents(_FILTER)
    total_after = db.emails.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have confidence and/or priority after the write")
    if total_after != total_before:
        failures.append(
            f"emails document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with confidence/priority: {remaining}")
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
