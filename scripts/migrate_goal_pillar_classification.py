"""Targeted, one-time migration: reclassifies the exact 14 email documents
audited in the Sales/Not-Sales classification review, updating ONLY their
goal_pillar field. Nothing else on any document is touched -- no body,
subject, sender, message_id, timestamps, labels, entities_referenced, or any
other field.

This is NOT a general-purpose tool -- MIGRATION_PLAN below is the exact,
already-reviewed classification result from that chat-based audit (all 14
existing "Sales"/"Finance"/"Operations"/"Product Development" emails were
independently read and classified as Not Sales under the new criteria; none
showed a specific prospect/customer negotiation, pricing/quote, proposal,
contract, pilot/POC, renewal, expansion, or purchase/order). It intentionally
does not call process_email, does not call any LLM/API, and does not touch
projects_mentioned or create any Project document.

Usage (run from the repo root):
    # Dry run (default) -- verifies the plan against live data, prints it,
    # writes nothing:
    python -m scripts.migrate_goal_pillar_classification --uri "..." --db cos_sales_production_v1

    # Actually perform the write, after you've reviewed the dry-run output:
    python -m scripts.migrate_goal_pillar_classification --uri "..." --db cos_sales_production_v1 --confirm

Safety:
  - Targets exactly 14 explicit message_id values via bulk_write(UpdateOne),
    never update_many/a blanket filter/regex.
  - Each UpdateOne's update document is exactly {"$set": {"goal_pillar": ...}}
    -- asserted in code, not just by convention.
  - Refuses to write unless: exactly 14 of the 14 expected message_ids are
    found, none are missing, none are duplicated in the plan, AND each
    document's CURRENT goal_pillar matches what this plan was reviewed
    against (protects against writing over a document that changed under us
    between the audit and this run).
  - Re-reads all 14 after writing and verifies goal_pillar matches the plan
    AND every other field is byte-for-byte unchanged from the pre-write
    snapshot.
"""
import argparse
from typing import Any

from pymongo import UpdateOne

from app.database.mongodb import get_client

# The exact, already-reviewed classification result -- message_id -> (expected
# CURRENT goal_pillar, NEW goal_pillar). Every one of these 14 was independently
# read (subject + body) and classified as Not Sales: no specific prospect/
# customer negotiation, pricing/quote, proposal, contract, pilot/POC, renewal,
# expansion, or purchase/order evidence in any of them -- see the chat audit
# this script was generated from for the per-email reasoning.
MIGRATION_PLAN: dict[str, dict[str, str]] = {
    "1a0cf65bb5178a67": {"expected_old": "Product Development", "new": ""},
    "1a0d953d5b99c2d8": {"expected_old": "Finance", "new": ""},
    "1a0d95b2be88fa4f": {"expected_old": "Sales", "new": ""},
    "1a0d96f3e1fb57ca": {"expected_old": "Sales", "new": ""},
    "1a0d97e3661710a6": {"expected_old": "Sales", "new": ""},
    "1a0d97e3661710a6-dup": {"expected_old": "Sales", "new": ""},
    "1a0d9816c1a470c8": {"expected_old": "Sales", "new": ""},
    "1a0d981d4c7b06eb": {"expected_old": "Sales", "new": ""},
    "1a0d9864ea87c0c6": {"expected_old": "Operations", "new": ""},
    "1a0d9fdc092eb383": {"expected_old": "Sales", "new": ""},
    "1a0dab88c16086ab": {"expected_old": "Sales", "new": ""},
    "1a0dba7055fcd482": {"expected_old": "Sales", "new": ""},
    "1a0dc9d3abc751cc": {"expected_old": "Sales", "new": ""},
    "1a0dcc3114e18caa": {"expected_old": "Sales", "new": ""},
}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Targeted goal_pillar reclassification for the 14 audited emails")
    parser.add_argument("--uri", required=True, help="MongoDB connection string (needs write access for this run)")
    parser.add_argument("--db", required=True, help="Database name (e.g. cos_sales_production_v1)")
    parser.add_argument("--confirm", action="store_true", help="Actually perform the write. Without this flag, this is a dry run only.")
    return parser


def _print_plan() -> None:
    print(f"Migration plan -- {len(MIGRATION_PLAN)} document(s), goal_pillar only:")
    for message_id, entry in MIGRATION_PLAN.items():
        print(f"  {message_id}: {entry['expected_old']!r} -> {entry['new']!r}")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    assert len(MIGRATION_PLAN) == len(set(MIGRATION_PLAN)) == 14, "MIGRATION_PLAN must have exactly 14 unique message_ids"

    client = get_client(args.uri)
    db = client[args.db]

    print(f"=== goal_pillar migration: db='{args.db}' ({'CONFIRM: will write' if args.confirm else 'DRY RUN: no write'}) ===\n")
    _print_plan()

    message_ids = list(MIGRATION_PLAN.keys())
    live_docs = {
        doc["message_id"]: doc
        for doc in db.emails.find({"message_id": {"$in": message_ids}}, {"_id": 0})
    }

    missing = [mid for mid in message_ids if mid not in live_docs]
    if missing:
        print(f"\nRefused: {len(missing)} expected message_id(s) not found in the database: {missing}")
        return 1

    if len(live_docs) != 14:
        print(f"\nRefused: expected exactly 14 matching documents, found {len(live_docs)}.")
        return 1

    mismatched = [
        mid for mid, entry in MIGRATION_PLAN.items()
        if live_docs[mid].get("goal_pillar") != entry["expected_old"]
    ]
    if mismatched:
        print(
            f"\nRefused: {len(mismatched)} document(s) have a CURRENT goal_pillar that doesn't match what "
            f"this plan was reviewed against -- something changed since the audit. Re-run the audit before "
            f"proceeding: {mismatched}"
        )
        return 1

    print(f"\nVerified: all 14 expected message_ids found, no duplicates, no missing, all current goal_pillar values match the audited plan.")

    if not args.confirm:
        print("\nDry run only -- no write performed. Re-run with --confirm to execute this exact plan.")
        return 0

    before_snapshots = {mid: dict(doc) for mid, doc in live_docs.items()}

    operations = []
    for message_id, entry in MIGRATION_PLAN.items():
        update_doc = {"$set": {"goal_pillar": entry["new"]}}
        assert set(update_doc.keys()) == {"$set"} and set(update_doc["$set"].keys()) == {"goal_pillar"}, (
            "Refusing to build an update touching anything other than goal_pillar"
        )
        operations.append(UpdateOne({"message_id": message_id}, update_doc))

    print(f"\nExecuting bulk_write with {len(operations)} explicit UpdateOne operation(s), goal_pillar only...")
    result = db.emails.bulk_write(operations, ordered=True)
    print(f"bulk_write result: matched={result.matched_count} modified={result.modified_count}")

    print("\nRe-reading all 14 documents to verify...")
    after_docs = {
        doc["message_id"]: doc
        for doc in db.emails.find({"message_id": {"$in": message_ids}}, {"_id": 0})
    }

    failures: list[str] = []
    old_sales_to_new_sales = old_sales_to_new_not_sales = 0
    old_other_to_new_sales = old_other_to_new_not_sales = 0
    new_sales_count = new_not_sales_count = 0

    for message_id, entry in MIGRATION_PLAN.items():
        before = before_snapshots[message_id]
        after = after_docs.get(message_id)
        if after is None:
            failures.append(f"{message_id}: document disappeared after write")
            continue
        if after.get("goal_pillar") != entry["new"]:
            failures.append(f"{message_id}: goal_pillar is {after.get('goal_pillar')!r}, expected {entry['new']!r}")
            continue
        other_field_diffs = {
            k: (before.get(k), after.get(k))
            for k in set(before) | set(after)
            if k != "goal_pillar" and before.get(k) != after.get(k)
        }
        if other_field_diffs:
            failures.append(f"{message_id}: unexpected field changes: {other_field_diffs}")
            continue

        if entry["new"] == "Sales":
            new_sales_count += 1
        else:
            new_not_sales_count += 1
        if entry["expected_old"] == "Sales":
            if entry["new"] == "Sales":
                old_sales_to_new_sales += 1
            else:
                old_sales_to_new_not_sales += 1
        else:
            if entry["new"] == "Sales":
                old_other_to_new_sales += 1
            else:
                old_other_to_new_not_sales += 1

    print(f"\n=== Migration report ===")
    print(f"Total documents migrated: {14 - len(failures)} / 14")
    print(f"Classified Sales: {new_sales_count}")
    print(f"Classified Not Sales: {new_not_sales_count}")
    print(f"Old Sales -> New Sales: {old_sales_to_new_sales}")
    print(f"Old Sales -> New Not Sales: {old_sales_to_new_not_sales}")
    print(f"Old non-Sales -> New Sales: {old_other_to_new_sales}")
    print(f"Old non-Sales -> New Not Sales: {old_other_to_new_not_sales}")
    if failures:
        print(f"Failures ({len(failures)}):")
        for f in failures:
            print(f"  {f}")
    else:
        print("Failures: none")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
