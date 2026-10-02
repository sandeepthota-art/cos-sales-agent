"""One-time cleanup (NOT YET RUN WITH --confirm): removes four legacy fields
from existing `meetings` documents in MongoDB: `minutes_record`,
`next_meeting_date`, `agenda_target`, `agenda_written`.

Found to be genuinely unused by reading the actual code: confirmed never set
by `app.entities.resolution`'s `Meeting(...)` construction. Unlike `title`
(which is deliberately a UI-layer-only derived value, never persisted) and
`actionable` (which IS set, varying true/false), these four are schema
fields that no code path has ever written a value into. See
app/ui/column_descriptions.py's MEETINGS_COLUMN_ORDER comment, which already
reasoned through this.

None of these fields were removed from `app.entities.models.Meeting` yet --
this script is prepared but intentionally not yet wired to a model change,
per explicit instruction to implement/verify first and only clean up after
approval. Run dry-run only until told otherwise.

Only ever touches these four keys via `$unset` -- never any other field
(especially never `date`, `attendees`, `person_ids`, `org_id`,
`project_or_pillar`, `actions_raised`, `actionable`, or `thread_id`, all of
which are real and actively used). Never inserts, never deletes a document.
Never touches `emails`, `threads`, `people`, or any other collection.

Usage (run from the repo root):
    # Dry run (default):
    python -m scripts.remove_meeting_dead_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output AND
    # receiving explicit approval:
    python -m scripts.remove_meeting_dead_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have at least one
    of these four fields set -- a document without any of them is never
    touched.
  - The update is exactly `{"$unset": {<these four keys>: ""}}`, asserted in
    code, not just by convention.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying any of the four fields
    (must be 0) and re-counts the total `meetings` document count (must be
    unchanged from before the write).
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("minutes_record", "next_meeting_date", "agenda_target", "agenda_written")
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy minutes_record/next_meeting_date/agenda_target/agenda_written fields from meetings documents"
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
        "Refusing to build an update touching anything other than the four named dead fields"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.meetings.count_documents({})
    projection = {"_id": 0, "id": 1, **{field: 1 for field in _FIELDS}}
    affected = list(db.meetings.find(_FILTER, projection))

    print(
        f"=== meeting dead-field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in meetings: {total_before}")
    print(f"Documents currently carrying any of {_FIELDS}: {len(affected)}")

    if not affected:
        print("\nNothing to do -- no document currently has any of these fields. Exiting.")
        return 0

    print(f"\nSample (up to {_SAMPLE_SIZE} of {len(affected)}):")
    for doc in affected[:_SAMPLE_SIZE]:
        values = ", ".join(f"{field}={doc.get(field)!r}" for field in _FIELDS if field in doc)
        print(f"  {doc['id']}: {values}")
    if len(affected) > _SAMPLE_SIZE:
        print(f"  ... and {len(affected) - _SAMPLE_SIZE} more")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    print(f"\nExecuting update_many -- $unset the four dead fields only, {len(affected)} matching document(s)...")
    result = db.meetings.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.meetings.count_documents(_FILTER)
    total_after = db.meetings.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have one of the dead fields after the write")
    if total_after != total_before:
        failures.append(
            f"meetings document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with a dead field: {remaining}")
    print(f"Total meetings document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
