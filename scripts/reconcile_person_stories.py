"""Finding #3 recovery: detects (and, within narrow verified limits, repairs)
person-story records left inconsistent by an interrupted
persist_person_profile write. See app.entities.person_story_reconciliation
for the full classification logic and its documented limitation: there is no
operation id anywhere in this schema proving a text match is caused by an
interrupted write rather than some other explanation -- "probable_
interrupted_write" is evidence, never proof.

Usage (run from the repo root):
    # Dry run (default) -- one person, reports only, writes nothing:
    python -m scripts.reconcile_person_stories --uri "..." --db cos_sales_production_v1 --person-id PER-001

    # Dry run across every person with at least one committed story version:
    python -m scripts.reconcile_person_stories --uri "..." --db cos_sales_production_v1 --all

    # Execute: repairs superseded-flag drift (always, when found) but NOT
    # profile_summary mismatches, which stay manual-review-required:
    python -m scripts.reconcile_person_stories --uri "..." --db cos_sales_production_v1 --all --execute

    # Execute, ALSO repairing probable_interrupted_write profile_summary (and
    # its matching profile_source_email_ids) mismatches -- only use this once
    # you've reviewed the dry-run output and accept the residual risk that
    # the classification is not proven, only consistent with an interrupted
    # write:
    python -m scripts.reconcile_person_stories --uri "..." --db cos_sales_production_v1 --all --execute --repair-profile-summary

Safety:
  - Never creates a person_story_versions document, never touches
    source_email_ids/source_snapshot_ids on any version document.
  - --execute alone repairs ONLY superseded-flag drift (unambiguous, purely
    internal bookkeeping). profile_summary (and profile_source_email_ids,
    restored together from the same selected version -- never fabricated if
    that version predates the source_email_ids field) are left exactly as-is
    unless --repair-profile-summary is ALSO explicitly passed.
  - ambiguous_mismatch is never repaired automatically, under any flag
    combination -- always reported as needing manual review.
  - Every repair this script performs is re-verified by
    app.entities.person_story_reconciliation itself before being reported as
    successful; a repair that doesn't verify raises instead of being
    silently reported as done.
  - No scheduling, no MCP tool -- this is a manually-invoked, one-shot script.
"""
import argparse
import sys

from app.database.mongodb import get_client
from app.database.repositories import PersonRepository, PersonStoryVersionRepository
from app.entities.person_story_reconciliation import reconcile_person_story


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect and (within narrow, verified limits) repair interrupted person-story writes"
    )
    parser.add_argument("--uri", required=True, help="MongoDB connection string")
    parser.add_argument("--db", required=True, help="Database name (e.g. cos_sales_production_v1)")
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--person-id", help="Inspect/repair exactly one person")
    target.add_argument("--all", action="store_true", help="Inspect/repair every person with committed story history")
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually repair superseded-flag drift. Without this flag, this is a dry run only.",
    )
    parser.add_argument(
        "--repair-profile-summary", action="store_true",
        help=(
            "Only meaningful with --execute. ALSO repairs profile_summary for "
            "probable_interrupted_write cases. This classification is NOT proven -- "
            "there is no operation id in this schema linking a text match to a specific "
            "interrupted call. Review the dry-run output before using this flag."
        ),
    )
    return parser


def _eligible_person_ids(db) -> list[str]:
    """Every person with at least one committed story version -- a person
    with none is classified no_history by reconcile_person_story itself, but
    skipping them here avoids a wasted read of the whole people collection."""
    return sorted({v["person_id"] for v in PersonStoryVersionRepository(db).find_many({"commit_status": "committed"})})


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    client = get_client(args.uri)
    db = client[args.db]

    if args.person_id:
        person_ids = [args.person_id]
        if PersonRepository(db).find_one({"id": args.person_id}) is None:
            print(f"No person found for person_id={args.person_id!r}.")
            return 1
    else:
        person_ids = _eligible_person_ids(db)

    print(
        f"=== person-story reconciliation: db='{args.db}' "
        f"({'EXECUTE' if args.execute else 'DRY RUN: no write'}"
        f"{', profile_summary repair ENABLED' if args.execute and args.repair_profile_summary else ''}) ===\n"
    )
    print(f"People to check: {len(person_ids)}")

    counts: dict[str, int] = {}
    manual_review: list[dict] = []
    failures: list[str] = []

    for person_id in person_ids:
        try:
            result = reconcile_person_story(
                db, person_id, dry_run=not args.execute, repair_profile_summary=args.repair_profile_summary
            )
        except Exception as exc:  # noqa: BLE001 -- one person's failure must not hide the rest
            failures.append(f"{person_id}: {type(exc).__name__}: {exc}")
            continue

        counts[result["classification"]] = counts.get(result["classification"], 0) + 1
        print(
            f"  {result['person_id']}: {result['classification']} -> {result['action']} "
            f"({result['detail']})"
        )
        if result["superseded_drift_versions"]:
            print(
                f"      superseded-flag drift: {result['superseded_drift_versions']} "
                f"(repaired: {result['superseded_repaired_versions']})"
            )
        if result["classification"] == "ambiguous_mismatch":
            manual_review.append(result)

    print("\n=== Summary ===")
    for classification, count in sorted(counts.items()):
        print(f"  {classification}: {count}")

    if manual_review:
        print(f"\nManual review required ({len(manual_review)} person(s)) -- never auto-repaired:")
        for result in manual_review:
            print(f"  {result['person_id']}: {result['detail']}")

    if failures:
        print(f"\nFailures ({len(failures)}):")
        for failure in failures:
            print(f"  {failure}")

    return 1 if (failures or manual_review) else 0


if __name__ == "__main__":
    sys.exit(main())
