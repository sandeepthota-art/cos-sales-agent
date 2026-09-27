"""Read a bounded batch of REAL historical emails directly from a SOURCE MongoDB
database (e.g. cos_sales_production_v1) -- read-only, enforced in code, not just by
convention -- and replay them through the real, currently-configured pipeline (real
LLM provider included) into a SEPARATE TARGET database (e.g. cos_sales_dryrun), so
the sales-deal extraction prompt can be verified against genuine historical email
content without risking a single write to the source.

Usage (run from the repo root):
    python -m scripts.reingest_historical \\
        --source-uri "<production Atlas connection string -- ideally a database \\
                       user with the Atlas 'Read Only' role, so the guarantee also \\
                       holds at the credential level, not just in this script>" \\
        --source-db cos_sales_production_v1 \\
        --target-uri "mongodb://localhost:27017" \\
        --target-db cos_sales_dryrun \\
        --limit 25 \\
        --yes

Safety:
  - The source connection is wrapped in _ReadOnlyDatabaseProxy, which raises on any
    write-shaped method (insert_one/update_one/delete_one/drop/create_index/etc.)
    before it would ever reach pymongo -- even a bug in this script's own read logic
    cannot write to source, by construction. The raw MongoClient for the source is
    never passed anywhere beyond the point where it's immediately wrapped.
  - Refuses if --target-db looks like a production database name (shares the exact
    guard app/replay_sample.py already uses).
  - Refuses if --source-uri/--source-db and --target-uri/--target-db resolve to the
    identical pair -- source and target must be different databases.
  - Requires --limit (or --message-ids) to bound real LLM API cost, and --yes.
"""
import argparse
from typing import Any

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.replay_sample import _unwrap_extended_json, run_replay_and_report, target_database_refusal_reason

_COLLECTION_WRITE_METHODS = frozenset(
    {
        "insert_one", "insert_many", "update_one", "update_many", "replace_one",
        "delete_one", "delete_many", "drop", "create_index", "create_indexes",
        "drop_index", "drop_indexes", "bulk_write", "find_one_and_update",
        "find_one_and_delete", "find_one_and_replace", "rename",
    }
)
_DATABASE_WRITE_METHODS = frozenset({"drop_collection", "create_collection", "command", "dereference"})


class _ReadOnlyCollectionProxy:
    """Wraps a pymongo (or mongomock) Collection so any write-shaped method raises
    instead of executing. This is the actual enforcement mechanism for "never writes
    to source" -- not a comment, not a convention."""

    def __init__(self, collection: Any):
        self._collection = collection

    def __getattr__(self, name: str) -> Any:
        if name in _COLLECTION_WRITE_METHODS:
            raise RuntimeError(
                f"Refused: attempted to call a write method ({name!r}) on the read-only "
                "source connection. This tool never writes to the source database."
            )
        return getattr(self._collection, name)


class _ReadOnlyDatabaseProxy:
    """Wraps a pymongo (or mongomock) Database so every collection obtained through
    it -- via either db['name'] or db.name, both supported -- comes back wrapped in
    _ReadOnlyCollectionProxy, and any database-level write/admin method is blocked
    the same way. The underlying MongoClient is never exposed.

    Distinguishing "a real Database method/property" (e.g. list_collection_names,
    .name) from "a collection accessed as an attribute" (e.g. .emails) can't rely on
    isinstance against pymongo.collection.Collection -- mongomock's Collection is a
    separate class that doesn't subclass it, and this proxy must work identically
    against both a real MongoClient and mongomock in tests. Real pymongo exposes
    .name as a class-level @property; mongomock instead sets it as a plain instance
    attribute in __init__ -- checking both the class AND the wrapped instance's own
    __dict__ covers both conventions. Neither pymongo nor mongomock caches an
    attribute-style collection access (e.g. a first `db.emails`) into the instance's
    own __dict__ (confirmed directly against mongomock), so this can't be fooled by
    a collection masquerading as a "real" attribute after first access.
    """

    def __init__(self, database: Any):
        self._database = database

    def __getitem__(self, name: str) -> _ReadOnlyCollectionProxy:
        return _ReadOnlyCollectionProxy(self._database[name])

    def __getattr__(self, name: str) -> Any:
        if name in _DATABASE_WRITE_METHODS:
            raise RuntimeError(
                f"Refused: attempted to call a write/admin method ({name!r}) on the "
                "read-only source database."
            )
        if name in vars(self._database) or hasattr(type(self._database), name):
            return getattr(self._database, name)
        return self[name]


def load_source_emails(
    source_db: _ReadOnlyDatabaseProxy, limit: int | None, message_ids: list[str] | None
) -> list[dict[str, Any]]:
    """Read-only: find() and sort()/limit() on a cursor never write anything, and
    source_db is always the _ReadOnlyDatabaseProxy from main() below, which would
    raise before any write-shaped call could reach pymongo regardless."""
    query: dict[str, Any] = {"message_id": {"$in": message_ids}} if message_ids else {}
    cursor = source_db["emails"].find(query, {"_id": 0})
    if not message_ids:
        cursor = cursor.sort("timestamp", -1).limit(limit)
    return [_unwrap_extended_json(doc) for doc in cursor]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only reingest of real historical emails from a source database into a separate target database"
    )
    parser.add_argument("--source-uri", required=True, help="Connection string for the READ-ONLY source (e.g. production Atlas cluster) -- ideally a read-only database user")
    parser.add_argument("--source-db", required=True, help="Source database name (e.g. cos_sales_production_v1) -- never written to")
    parser.add_argument("--target-uri", required=True, help="Connection string for the WRITE target (e.g. mongodb://localhost:27017)")
    parser.add_argument("--target-db", required=True, help="Target database name -- must not contain 'production'")
    parser.add_argument("--limit", type=int, default=None, help="Max number of most-recent emails to reingest (required unless --message-ids is given)")
    parser.add_argument("--message-ids", default=None, help="Comma-separated exact message_ids to reingest instead of --limit")
    parser.add_argument("--yes", action="store_true", help="Required: confirms you've reviewed the source/target and call count below")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, structured=(settings.log_format == "json"))

    refusal = target_database_refusal_reason(args.target_db)
    if refusal:
        print(refusal)
        return 1

    if (args.source_uri, args.source_db) == (args.target_uri, args.target_db):
        print(
            "Refused: --source-uri/--source-db and --target-uri/--target-db are identical -- "
            "source and target must be different databases."
        )
        return 1

    message_ids = [m.strip() for m in args.message_ids.split(",") if m.strip()] if args.message_ids else None
    if not message_ids and not args.limit:
        print("Provide either --limit N or --message-ids id1,id2,... to bound how many emails are reingested.")
        return 1
    if args.limit is not None and args.limit <= 0:
        print("--limit must be a positive integer.")
        return 1

    source_client = get_client(args.source_uri)
    source_db = _ReadOnlyDatabaseProxy(source_client[args.source_db])

    try:
        emails = load_source_emails(source_db, args.limit, message_ids)
    except Exception as exc:  # noqa: BLE001 - report any connection/query failure, don't crash
        print(f"Could not read from source '{args.source_db}': {exc}")
        return 1

    if not emails:
        print(f"No matching emails found in source '{args.source_db}' -- nothing to reingest.")
        return 0

    print(
        f"About to reingest {len(emails)} email(s) READ-ONLY from '{args.source_db}' into "
        f"'{args.target_db}', through the REAL, configured LLM_PROVIDER='{settings.llm_provider}' "
        f"(LLM_MODEL='{settings.llm_model}') -- this makes {len(emails)} real API call(s)."
    )
    if not args.yes:
        print("Re-run with --yes once you've confirmed the source, target, and call count above.")
        return 1

    target_client = get_client(args.target_uri)
    target_db = initialize_database(target_client, args.target_db)

    run_replay_and_report(target_db, emails, settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
