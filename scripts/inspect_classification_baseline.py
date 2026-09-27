"""Read-only data-quality report on the EXISTING emails/projects dataset, to
establish a goal_pillar classification baseline before testing the new Claude
Desktop Sales/Not-Sales workflow against it.

This script performs ZERO writes -- it never calls process_email, never
ingests anything, never touches an LLM. It only counts and samples what's
already stored. The connection is wrapped in the same _ReadOnlyDatabaseProxy
scripts/reingest_historical.py uses, so a write-shaped call would raise before
it ever reached pymongo, even if this script's own logic had a bug.

Usage (run from the repo root):
    python -m scripts.inspect_classification_baseline \\
        --uri "<your MongoDB connection string -- ideally a read-only Atlas \\
               database user>" \\
        --db cos_sales_production_v1 \\
        --sample-size 3

    # Or, to get full (untruncated) subject/body for every email -- e.g. to
    # paste the output to a reasoning caller for a classification audit:
    python -m scripts.inspect_classification_baseline \\
        --uri "..." --db cos_sales_production_v1 --dump-all
"""
import argparse
import json
from typing import Any

from app.database.mongodb import get_client
from scripts.reingest_historical import _ReadOnlyDatabaseProxy

_BODY_PREVIEW_CHARS = 400


def _truncate(text: str | None, limit: int = _BODY_PREVIEW_CHARS) -> str:
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only goal_pillar classification baseline report")
    parser.add_argument("--uri", required=True, help="MongoDB connection string -- ideally a read-only database user")
    parser.add_argument("--db", required=True, help="Database name to inspect (e.g. cos_sales_production_v1)")
    parser.add_argument("--sample-size", type=int, default=3, help="How many example emails to show per bucket (default 3)")
    parser.add_argument(
        "--dump-all", action="store_true",
        help="Print full (untruncated) message_id/subject/goal_pillar/body for every email as JSON lines, "
             "instead of the bucketed report -- for handing off to a reasoning caller to classify.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)

    client = get_client(args.uri)
    db = _ReadOnlyDatabaseProxy(client[args.db])

    projection = {"_id": 0, "message_id": 1, "subject": 1, "goal_pillar": 1, "body": 1}

    if args.dump_all:
        docs = list(db["emails"].find({}, projection).sort("message_id", 1))
        print(f"=== Full email dump: db='{args.db}', {len(docs)} document(s) -- read-only, nothing modified ===")
        for doc in docs:
            print(json.dumps(doc, default=str))
        return 0

    total_emails = db["emails"].count_documents({})
    sales_count = db["emails"].count_documents({"goal_pillar": "Sales"})
    empty_count = db["emails"].count_documents({"goal_pillar": ""})
    missing_or_null_count = db["emails"].count_documents(
        {"$or": [{"goal_pillar": {"$exists": False}}, {"goal_pillar": None}]}
    )
    other_values = list(
        db["emails"].aggregate(
            [
                {"$match": {"goal_pillar": {"$exists": True, "$ne": None, "$nin": ["Sales", ""]}}},
                {"$group": {"_id": "$goal_pillar", "count": {"$sum": 1}}},
                {"$sort": {"count": -1}},
            ]
        )
    )

    total_projects = db["projects"].count_documents({})
    sales_projects = db["projects"].count_documents({"goal_pillar": "Sales"})

    print(f"=== Email goal_pillar baseline: db='{args.db}' ===")
    print(f"1. Total email documents: {total_emails}")
    print(f"2. goal_pillar == 'Sales': {sales_count}")
    print(f"3. goal_pillar == '': {empty_count}")
    print(f"4. goal_pillar missing/null: {missing_or_null_count}")
    print("5. Other goal_pillar values:")
    if other_values:
        for entry in other_values:
            print(f"   {entry['_id']!r}: {entry['count']}")
    else:
        print("   (none)")
    accounted_for = sales_count + empty_count + missing_or_null_count + sum(e["count"] for e in other_values)
    if accounted_for != total_emails:
        print(f"   WARNING: bucket counts sum to {accounted_for}, expected {total_emails} -- investigate before trusting this report.")

    print(f"\n6. Total Project documents: {total_projects}")
    print(f"7. Projects with goal_pillar == 'Sales': {sales_projects}")

    print(f"\n8-9. Sample emails (up to {args.sample_size} per bucket):")

    def show_bucket(label: str, query: dict[str, Any]) -> None:
        print(f"\n--- {label} ---")
        docs = list(db["emails"].find(query, projection).limit(args.sample_size))
        if not docs:
            print("  (no matching emails)")
            return
        for doc in docs:
            print(f"  message_id: {doc.get('message_id')}")
            print(f"  subject: {doc.get('subject')!r}")
            print(f"  goal_pillar: {doc.get('goal_pillar')!r}")
            print(f"  body preview: {_truncate(doc.get('body'))!r}")
            print()

    show_bucket("Currently classified Sales", {"goal_pillar": "Sales"})
    show_bucket("Currently classified Not Sales (goal_pillar == '')", {"goal_pillar": ""})
    show_bucket(
        "Missing/other classification",
        {"$or": [{"goal_pillar": {"$exists": False}}, {"goal_pillar": None}, {"goal_pillar": {"$nin": ["Sales", ""]}}]},
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
