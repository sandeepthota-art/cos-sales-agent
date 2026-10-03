"""One-time cleanup (NOT YET RUN WITH --confirm): removes the legacy
`source` field from existing `organizations` documents in MongoDB.

Found to be genuinely unused by reading the actual code, not just docs/
tooltips: `source` is always just the Pydantic model's default value
(`"gmail"`) -- never explicitly set to anything else, and never read or
branched on anywhere in the codebase (only appeared in dashboard tooltip
text). This is the exact same category already confirmed and removed for
`people.source`/`people.note_link` in
scripts/remove_person_source_note_link_fields.py -- this script is the
Organizations-collection equivalent, deferred from that pass pending its own
verification (see app/ui/column_descriptions.py's ORGANIZATIONS_COLUMN_ORDER
comment).

`organizations.aliases` is DELIBERATELY NOT included here, despite an
earlier pass having proposed it: `aliases` defaults to an empty list and
`app.entities.resolution.resolve_organization` never appends to it, but it
IS actively read -- `app.query.entity_resolution`'s organization-name
matching calls `o.get("aliases")` as a genuine (if currently always-empty)
alias-matching input, the same mechanism that's actively populated and used
for `people.aliases` during person-merge consolidation. That makes it an
unpopulated-but-designed extensibility hook, not dead weight -- a materially
different situation from `source`, which has no reader anywhere.

`source` was NOT YET removed from `app.entities.models.Organization` --
this script is prepared but intentionally not yet wired to a model change,
per explicit instruction to implement/verify first and only clean up after
approval. Run dry-run only until told otherwise.

Only ever touches the `source` key via `$unset` -- never any other field
(especially never `aliases`, for the reason above). Never inserts, never
deletes a document. Never touches `emails`, `threads`, `people`, or any
other collection.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `organizations` documents
    # currently have source, prints a sample of affected organization ids,
    # writes nothing:
    python -m scripts.remove_organization_dead_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output AND
    # receiving explicit approval:
    python -m scripts.remove_organization_dead_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `source` set
    -- a document without it is never touched.
  - The update is exactly `{"$unset": {"source": ""}}`, asserted in code,
    not just by convention -- `aliases`, `name`, `domain`, `id` are never
    part of it.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying `source` (must be 0)
    and re-counts the total `organizations` document count (must be
    unchanged from before the write).
  - Reversible in practice: `source` was always the single constant value
    `"gmail"`. Nothing unique is destroyed.
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("source",)
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove legacy source field from organizations documents")
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
        "Refusing to build an update touching anything other than source"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.organizations.count_documents({})
    affected = list(db.organizations.find(_FILTER, {"_id": 0, "id": 1, "source": 1}))

    print(
        f"=== source field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in organizations: {total_before}")
    print(f"Documents currently carrying source: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has source. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['id']}: source={doc.get('source')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset source only, {len(affected)} matching document(s)...")
    result = db.organizations.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.organizations.count_documents(_FILTER)
    total_after = db.organizations.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have source after the write")
    if total_after != total_before:
        failures.append(
            f"organizations document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with source: {remaining}")
    print(f"Total organizations document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
