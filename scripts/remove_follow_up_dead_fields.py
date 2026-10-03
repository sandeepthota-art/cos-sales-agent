"""One-time cleanup (NOT YET RUN WITH --confirm): removes the legacy
`surfaced` field from existing `follow_ups` documents in MongoDB.

Found to be genuinely unused by reading the actual code: `surfaced` exists
to support the BRD 6.4 escalation ladder ("the item is surfaced in a brief"),
but no scheduler anywhere in this codebase ever sets it to anything but its
default `False` -- zero reads, zero writes beyond `app.pipeline.
_process_entities`'s initial `FollowUp(..., surfaced=False)` construction.

`escalation_level` is DELIBERATELY NOT included here, despite an earlier
pass having proposed it alongside `surfaced`: a full-codebase re-check (not
just the resolution/construction code, which is all the earlier pass
checked) found it IS actively read and used:
  - `app.entities.person_context` reads `follow_up['escalation_level']` via
    direct bracket access (no default) when building a person's LLM
    context -- removing the field would raise `KeyError` there.
  - `app.mcp.tools.list_follow_ups` exposes `escalation_level` as a real,
    documented filter parameter, built directly into the Mongo query.
  - `app.query.commitments` has a dedicated function that filters
    `FollowUp.escalation_level` with its own validation.
Every follow_up today happens to be created at level 1, but the FIELD
itself is live, shipped functionality -- not dead weight. Keeping it.

`surfaced` was NOT removed from `app.entities.models.FollowUp` yet -- this
script is prepared but intentionally not yet wired to a model change, per
explicit instruction to implement/verify first and only clean up after
approval. Run dry-run only until told otherwise.

Only ever touches the `surfaced` key via `$unset` -- never any other field
(especially never `escalation_level`, `status`, `audience`,
`follow_up_earliest_at`/`follow_up_latest_at`, or any id field). Never
inserts, never deletes a document. Never touches `emails`, `threads`,
`people`, or any other collection.

Usage (run from the repo root):
    # Dry run (default):
    python -m scripts.remove_follow_up_dead_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output AND
    # receiving explicit approval:
    python -m scripts.remove_follow_up_dead_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have `surfaced`
    set -- a document without it is never touched.
  - The update is exactly `{"$unset": {"surfaced": ""}}`, asserted in code,
    not just by convention -- `escalation_level` is never part of it.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying `surfaced` (must be
    0) and re-counts the total `follow_ups` document count (must be
    unchanged from before the write).
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("surfaced",)
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Remove legacy surfaced field from follow_ups documents")
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
        "Refusing to build an update touching anything other than surfaced"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.follow_ups.count_documents({})
    affected = list(db.follow_ups.find(_FILTER, {"_id": 0, "id": 1, "surfaced": 1}))

    print(
        f"=== surfaced field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in follow_ups: {total_before}")
    print(f"Documents currently carrying surfaced: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has surfaced. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        print(f"  {doc['id']}: surfaced={doc.get('surfaced')!r}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset surfaced only, {len(affected)} matching document(s)...")
    result = db.follow_ups.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.follow_ups.count_documents(_FILTER)
    total_after = db.follow_ups.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have surfaced after the write")
    if total_after != total_before:
        failures.append(
            f"follow_ups document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with surfaced: {remaining}")
    print(f"Total follow_ups document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
