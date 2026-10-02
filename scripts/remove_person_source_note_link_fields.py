"""One-time cleanup: removes the legacy `source` and `note_link` fields from
existing `people` documents in MongoDB.

Found to be genuinely unused by reading the actual code (not just docs/
tooltips, which have been stale before on similar questions this project):
`source` was always just the Pydantic model's default value (`"gmail"`) --
never explicitly set to anything else, and never read/branched on anywhere
in the codebase (only appeared in dashboard tooltip text). `note_link` was
never read OR written by any code path at all -- zero references beyond the
model declaration and tooltip text.

This is a different situation from `goal_pillar` (also investigated in the
same pass): `goal_pillar` IS read by
`app.entities.person_context.get_bounded_person_context_for_llm`, which feeds
the real pipeline's LLM-context step, even though nothing populates it yet --
so it was kept in the schema (just hidden from the dashboard), not removed.
This script only ever touches `source`/`note_link`.

Both fields were removed from `app.entities.models.Person` entirely -- any
Person created/updated from that point on never gets either field written.
This script only cleans up documents that were created *before* that change
and still carry one or both of the now-legacy fields.

Only ever touches the `source`/`note_link` keys via `$unset` -- never any
other field, never inserts, never deletes a document. Never touches `emails`,
`threads`, or any other collection -- this is a `people`-only cleanup,
independent of the earlier emails/threads-collection migrations.

Usage (run from the repo root):
    # Dry run (default) -- reports how many `people` documents currently have
    # source and/or note_link, prints a sample of affected person ids, writes
    # nothing:
    python -m scripts.remove_person_source_note_link_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output:
    python -m scripts.remove_person_source_note_link_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `source` and/or
    `note_link` set -- a document without either is never touched (matched
    or not).
  - The update is exactly `{"$unset": {"source": "", "note_link": ""}}`,
    asserted in code, not just by convention -- no other field (including
    `goal_pillar`, `role`, `status`, `merged_into`) is ever part of it.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying either field (must be
    0) and re-counts the total `people` document count (must be unchanged
    from before the write -- this script must never insert or delete a
    document).
  - Reversible in practice: `source` was always the single constant value
    `"gmail"` -- nothing unique is destroyed. `note_link` was never set to
    anything meaningful by any code path, so there is nothing to lose either.
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("source", "note_link")
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy source/note_link fields from people documents"
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
        "Refusing to build an update touching anything other than source/note_link"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.people.count_documents({})
    affected = list(
        db.people.find(_FILTER, {"_id": 0, "id": 1, "source": 1, "note_link": 1})
    )

    print(
        f"=== source/note_link field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in people: {total_before}")
    print(f"Documents currently carrying source and/or note_link: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has source or note_link. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['id']}: source={doc.get('source')!r} note_link={doc.get('note_link')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset source/note_link only, {len(affected)} matching document(s)...")
    result = db.people.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.people.count_documents(_FILTER)
    total_after = db.people.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have source and/or note_link after the write")
    if total_after != total_before:
        failures.append(
            f"people document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with source/note_link: {remaining}")
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
