"""One-off diagnostic tool: replay a small, exported sample of REAL email documents
through the real, currently-configured pipeline (real LLM provider included) into a
throwaway/local database, so a prompt change can be verified against genuine email
content without ever writing to the production database.

Usage:
    python -m app.replay_sample --input data/inbox/sample_replay.json --yes

Input format: a JSON array of email documents, e.g. exported from MongoDB Compass's
`emails` collection (Extended JSON `$oid`/`$date` wrappers are unwrapped
automatically; unrelated fields like `processing_status`/`entities_referenced` are
ignored -- only the fields app.email.models.Email needs are used).

Safety: refuses to run if the CONFIGURED database name looks like a production
database (contains "production"), regardless of --yes. There is no override for
this -- if you actually intend to touch production, this is deliberately the wrong
tool; see main.py's existing entry points instead, with an explicit decision to do
so.
"""
import argparse
import json
from pathlib import Path
from typing import Any

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.database.repositories import ProjectRepository
from app.pipeline import run_pipeline
from app.providers.email.mock import MockEmailProvider
from app.providers.factory import ProviderFactory


def _unwrap_extended_json(value: Any) -> Any:
    """Recursively strips MongoDB Extended JSON wrappers ({"$oid": ...}, {"$date":
    ...}) that a Compass export produces by default, and drops _id entirely -- so a
    straight Compass export can be fed in without the user having to change Compass's
    export settings."""
    if isinstance(value, dict):
        if set(value.keys()) == {"$oid"}:
            return value["$oid"]
        if set(value.keys()) == {"$date"}:
            date_value = value["$date"]
            return date_value if isinstance(date_value, str) else date_value
        return {k: _unwrap_extended_json(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [_unwrap_extended_json(v) for v in value]
    return value


def load_sample_emails(path: str) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError(f"{path}: expected a top-level JSON array of email documents, got {type(raw).__name__}")
    return [_unwrap_extended_json(doc) for doc in raw]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay a real email sample through the pipeline (dry-run verification)")
    parser.add_argument("--input", required=True, help="Path to a JSON array of exported email documents")
    parser.add_argument("--yes", action="store_true", help="Required: confirms you've reviewed the target database and LLM call count below")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, structured=(settings.log_format == "json"))

    if "production" in settings.mongodb_database.lower():
        print(
            f"Refused: MONGODB_DATABASE='{settings.mongodb_database}' looks like a production "
            "database. This tool is only for a throwaway/local/staging database -- point "
            "MONGODB_URI/MONGODB_DATABASE at a non-production target before running this."
        )
        return 1

    try:
        emails = load_sample_emails(args.input)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"Could not load {args.input}: {exc}")
        return 1

    if not emails:
        print(f"{args.input} contained no email documents -- nothing to replay.")
        return 0

    print(
        f"About to replay {len(emails)} email(s) through the REAL, configured "
        f"LLM_PROVIDER='{settings.llm_provider}' (LLM_MODEL='{settings.llm_model}') -- this makes "
        f"{len(emails)} real API call(s) and will write into database "
        f"'{settings.mongodb_database}' at '{settings.mongodb_uri}'."
    )
    if not args.yes:
        print("Re-run with --yes once you've confirmed the target database and call count above.")
        return 1

    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    email_provider = MockEmailProvider(payloads=emails)
    llm_provider = ProviderFactory.create_llm_provider(settings)
    calendar_provider = ProviderFactory.create_calendar_provider(settings)
    replay_settings = settings.model_copy(update={"email_limit": len(emails)})

    summary = run_pipeline(db, email_provider, llm_provider, calendar_provider, replay_settings)
    print(
        f"Replay complete: processed={summary.processed} completed={summary.completed} "
        f"failed={summary.failed} skipped={summary.skipped}"
    )
    for result in summary.results:
        if result.final_stage == "FAILED":
            print(f"  FAILED {result.message_id}: {result.error}")

    projects = ProjectRepository(db).find_many({})
    print(f"\nprojects collection now has {len(projects)} document(s):")
    for project in projects:
        print(f"  {project['id']}: project={project['project']!r} entity={project.get('entity')!r} goal_pillar={project.get('goal_pillar')!r}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
