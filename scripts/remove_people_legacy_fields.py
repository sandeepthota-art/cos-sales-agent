"""One-time cleanup: removes six legacy fields from existing `people` documents
in MongoDB: `reports_to`, `review_flag`, `role_in_pillar`, `tier`,
`voice_register`, `preferences`.

All six were removed from `Person` (`app/entities/models.py`) and from every
code path that used to write them (see docs/DATA_DICTIONARY.md). Any person
created from that point on never gets any of these fields written. This script
only cleans up documents that were created *before* that change and still carry
one or more of the now-legacy fields.

Only ever touches these six keys via `$unset` -- never any other field, never
inserts, never deletes a document.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `people` documents currently carry
    # any of the six legacy fields, prints a sample of affected ids, writes
    # nothing:
    python -m scripts.remove_people_legacy_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.remove_people_legacy_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have at least one of
    the six fields set -- a document without any of them is never touched
    (matched or not).
  - The update is exactly `{"$unset": {<the six field names>: ""}}`, asserted
    in code, not just by convention -- no other field is ever part of it.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying any of the six fields
    (must be 0) and re-counts the total `people` document count (must be
    unchanged from before the write -- this script must never insert or delete
    a document).
"""
import argparse

from app.database.mongodb import get_client

_LEGACY_FIELDS = ("reports_to", "review_flag", "role_in_pillar", "tier", "voice_register", "preferences")
_FILTER = {"$or": [{field: {"$exists": True}} for field in _LEGACY_FIELDS]}
_UNSET = {"$unset": {field: "" for field in _LEGACY_FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy reports_to/review_flag/role_in_pillar/tier/voice_register/preferences fields from people documents"
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

    assert set(_UNSET.keys()) == {"$unset"} and set(_UNSET["$unset"].keys()) == set(_LEGACY_FIELDS), (
        "Refusing to build an update touching anything other than the six legacy fields"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.people.count_documents({})
    affected = list(
        db.people.find(_FILTER, {"_id": 0, "id": 1, **{field: 1 for field in _LEGACY_FIELDS}})
    )

    print(
        f"=== people legacy-field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in people: {total_before}")
    print(f"Documents currently carrying at least one legacy field: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has any of these fields. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        present = {field: doc[field] for field in _LEGACY_FIELDS if field in doc}
        print(f"  {doc['id']}: {present}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset the six legacy fields only, {len(affected)} matching document(s)...")
    result = db.people.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.people.count_documents(_FILTER)
    total_after = db.people.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have at least one legacy field after the write")
    if total_after != total_before:
        failures.append(
            f"people document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with a legacy field: {remaining}")
    print(f"Total people document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
