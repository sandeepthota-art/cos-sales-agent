"""One-time cleanup (NOT YET RUN WITH --confirm): removes the legacy
`description` field from existing `opportunities` documents in MongoDB.

Found to be genuinely unused by reading the actual code: confirmed never set
at creation (`app.entities.resolution._resolve_opportunity_impl`) nor by
`update_opportunity_fields` (which only ever accepts
stage/owner/value/currency/expected_close_date/next_action). See
app/ui/column_descriptions.py's OPPORTUNITIES_COLUMN_ORDER comment, which
already reasoned through this.

`description` was NOT removed from `app.entities.models.Opportunity` yet --
this script is prepared but intentionally not yet wired to a model change,
per explicit instruction to implement/verify first and only clean up after
approval. Run dry-run only until told otherwise.

Only ever touches the `description` key via `$unset` -- never any other
field (especially never `status`, which is a real, actively-used field
despite only ever being set to "open" today -- that is a separate,
not-yet-closed product gap, not dead data). Never inserts, never deletes a
document. Never touches `emails`, `threads`, `people`, or any other
collection.

Usage (run from the repo root):
    # Dry run (default):
    python -m scripts.remove_opportunity_description_field --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output AND
    # receiving explicit approval:
    python -m scripts.remove_opportunity_description_field --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `description`
    set -- a document without it is never touched.
  - The update is exactly `{"$unset": {"description": ""}}`, asserted in
    code, not just by convention.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying `description` (must be
    0) and re-counts the total `opportunities` document count (must be
    unchanged from before the write).
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("description",)
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove legacy description field from opportunities documents")
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
        "Refusing to build an update touching anything other than description"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.opportunities.count_documents({})
    affected = list(db.opportunities.find(_FILTER, {"_id": 0, "id": 1, "description": 1}))

    print(
        f"=== description field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in opportunities: {total_before}")
    print(f"Documents currently carrying description: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has description. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['id']}: description={doc.get('description')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset description only, {len(affected)} matching document(s)...")
    result = db.opportunities.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.opportunities.count_documents(_FILTER)
    total_after = db.opportunities.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have description after the write")
    if total_after != total_before:
        failures.append(
            f"opportunities document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with description: {remaining}")
    print(f"Total opportunities document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
