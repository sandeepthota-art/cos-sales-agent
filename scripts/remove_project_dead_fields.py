"""One-time cleanup (NOT YET RUN WITH --confirm): removes seven legacy fields
from existing `projects` documents in MongoDB: `cluster`, `objective`,
`target`, `collaborators`, `last_movement`, `note_link`, `source`.

Found to be genuinely unused by reading the actual code: confirmed directly
in `app.entities.resolution`'s `Project(...)` construction that NONE of
these seven are ever set to anything beyond their Pydantic defaults (None,
an empty list, or the constant string "gmail" for `source`). Unlike this
collection's `status`/`owner`/`health`/`next_milestone`/`due` -- which WERE
named in an explicit product-gap ask and given a real write mechanism via
`update_project_fields` -- none of these seven was ever requested as a
feature to build, so they are dead schema weight, not a missing mechanism.
See app/ui/column_descriptions.py's PROJECTS_COLUMN_ORDER comment, which
already reasoned through this distinction field-by-field.

A full-codebase re-check (not just the construction/write code) found two of
these seven ARE read, with safe fallbacks, so the picture is "never written,
read with a harmless default" rather than "never referenced at all":
  - `source`: `app.knowledge_projector.render_project` reads
    `project.get('source', 'unknown')` for a "Provenance" line in generated
    knowledge markdown (served through the `lookup_knowledge` MCP tool).
    Since `source` is only ever `"gmail"` today, removing it only changes
    that one rendered line from "Source: gmail" to "Source: unknown" --
    same zero-information value, no exception, no behavior change beyond
    that cosmetic text.
  - `collaborators`: `app.entities.context.get_person_context` reads it via
    `_legacy_name_match(..., ["collaborators"])` as a last-resort fallback
    when resolving a person's projects. `_legacy_name_match` uses
    `doc.get(field)` (safe on a missing key) and `collaborators` is always
    `[]` today, so this fallback already matches nothing via collaborators
    -- removing the field changes nothing observable.
`cluster`, `objective`, `target`, `last_movement`, `note_link` remain
confirmed zero-reference fields with no caveat.

None of these fields were removed from `app.entities.models.Project` yet --
this script is prepared but intentionally not yet wired to a model change,
per explicit instruction to implement/verify first and only clean up after
approval. Run dry-run only until told otherwise.

Only ever touches these seven keys via `$unset` -- never any other field
(especially never `status`/`owner`/`health`/`next_milestone`/`due`, `entity`,
`org_id`, `goal_pillar`, or `person_ids`, all of which are real and actively
used). Never inserts, never deletes a document. Never touches `emails`,
`threads`, `people`, or any other collection.

Usage (run from the repo root):
    # Dry run (default):
    python -m scripts.remove_project_dead_fields --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after reviewing the dry-run output AND
    # receiving explicit approval:
    python -m scripts.remove_project_dead_fields --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - The filter only ever matches documents that actually have at least one
    of these seven fields set -- a document without any of them is never
    touched.
  - The update is exactly `{"$unset": {<these seven keys>: ""}}`, asserted
    in code, not just by convention.
  - Refuses to write when `--confirm` is omitted (dry run only).
  - After writing, re-counts documents still carrying any of the seven
    fields (must be 0) and re-counts the total `projects` document count
    (must be unchanged from before the write).
"""
import argparse

from app.database.mongodb import get_client

_FIELDS = ("cluster", "objective", "target", "collaborators", "last_movement", "note_link", "source")
_FILTER = {"$or": [{field: {"$exists": True}} for field in _FIELDS]}
_UNSET = {"$unset": {field: "" for field in _FIELDS}}
_SAMPLE_SIZE = 10


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Remove legacy cluster/objective/target/collaborators/last_movement/note_link/source fields from projects documents"
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
        "Refusing to build an update touching anything other than the seven named dead fields"
    )

    client = get_client(args.uri)
    db = client[args.db]

    total_before = db.projects.count_documents({})
    projection = {"_id": 0, "id": 1, **{field: 1 for field in _FIELDS}}
    affected = list(db.projects.find(_FILTER, projection))

    print(
        f"=== project dead-field removal: db='{args.db}' "
        f"({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n"
    )
    print(f"Total documents in projects: {total_before}")
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

    print(f"\nExecuting update_many -- $unset the seven dead fields only, {len(affected)} matching document(s)...")
    result = db.projects.update_many(_FILTER, _UNSET)
    print(f"update_many result: matched={result.matched_count} modified={result.modified_count}")

    print("\nVerifying...")
    remaining = db.projects.count_documents(_FILTER)
    total_after = db.projects.count_documents({})

    failures: list[str] = []
    if remaining != 0:
        failures.append(f"{remaining} document(s) still have one of the dead fields after the write")
    if total_after != total_before:
        failures.append(
            f"projects document count changed: before={total_before} after={total_after} "
            "(should be identical -- this script must never insert/delete documents)"
        )

    print("\n=== Report ===")
    print(f"Documents modified: {result.modified_count}")
    print(f"Documents remaining with a dead field: {remaining}")
    print(f"Total projects document count unchanged: {total_after == total_before} (before={total_before}, after={total_after})")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
