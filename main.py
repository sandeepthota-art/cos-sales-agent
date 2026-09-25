import argparse
import sys

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.pipeline import run_pipeline
from app.providers.factory import ProviderFactory


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent")
    parser.add_argument("--healthcheck", action="store_true")
    parser.add_argument("--mode", choices=["demo", "file", "raw-file"], default=None)
    parser.add_argument("--reset-demo", action="store_true")
    parser.add_argument(
        "--input",
        default=None,
        help="Path to a JSON email export (required for --mode=file or --mode=raw-file)",
    )
    return parser


def run_healthcheck(settings) -> tuple[bool, list[str]]:
    lines = ["CoS Sales Agent Health Check", ""]
    ok = True
    lines.append("✓ Configuration loaded")

    try:
        client = get_client(settings.mongodb_uri)
        db = initialize_database(client, settings.mongodb_database)
        client.admin.command("ping") if hasattr(client, "admin") else None
        lines.append("✓ MongoDB connected")
        lines.append("✓ MongoDB indexes ready")
    except Exception as exc:  # pragma: no cover - exercised via integration only
        ok = False
        lines.append(f"✗ MongoDB connection failed: {exc}")

    lines.append(f"✓ Email provider: {settings.email_provider.upper()}")
    lines.append(f"✓ Calendar provider: {settings.calendar_provider.upper()}")
    lines.append(f"✓ LLM provider: {settings.llm_provider.upper()}")

    mcp_email_marker = "✓" if settings.mcp_email_enabled else "○"
    mcp_calendar_marker = "✓" if settings.mcp_calendar_enabled else "○"
    lines.append(f"{mcp_email_marker} MCP Email: {'enabled' if settings.mcp_email_enabled else 'disabled'}")
    lines.append(f"{mcp_calendar_marker} MCP Calendar: {'enabled' if settings.mcp_calendar_enabled else 'disabled'}")

    lines.append("")
    lines.append("System ready." if ok else "System not ready.")
    return ok, lines


def run_reset_demo(db, settings) -> None:
    if settings.app_env != "development" and not settings.simulation_mode:
        raise PermissionError("--reset-demo is only permitted when APP_ENV=development or SIMULATION_MODE=true")

    for collection_name in [
        "emails",
        "threads",
        "context_snapshots",
        "knowledge_items",
        "reply_drafts",
        "calendar_actions",
        "processing_runs",
        "entities",
        "opportunities",
        "activities",
        "people",
        "projects",
        "commitments",
        "follow_ups",
        "meetings",
        "personal_items",
        "counters",
        "ingested_files",
    ]:
        db[collection_name].delete_many({})


def run_file_ingestion(db, settings, input_path: str) -> list[str]:
    from app.providers.email.file import FileEmailProvider

    email_provider = FileEmailProvider(path=input_path)
    llm_provider = ProviderFactory.create_llm_provider(settings)
    calendar_provider = ProviderFactory.create_calendar_provider(settings)

    # settings.email_limit (default 50) exists to cap a live Gmail fetch -- a file
    # export is finite and already on disk, so file-mode always ingests every message
    # the file actually contained, not a truncated slice of it. This is a local copy
    # used only for this call; the cached get_settings() singleton is untouched.
    available = email_provider.total_found - email_provider.skipped_malformed
    file_settings = settings.model_copy(update={"email_limit": available})

    summary = run_pipeline(db, email_provider, llm_provider, calendar_provider, file_settings)

    completed_ids = [r.message_id for r in summary.results if r.final_stage == "COMPLETED"]
    thread_ids = {
        doc["thread_id"]
        for doc in db.emails.find({"message_id": {"$in": completed_ids}}, {"thread_id": 1})
        if doc.get("thread_id")
    }
    entities_extracted = sum(
        sum(len(v) for v in doc.get("entities_referenced", {}).values())
        for doc in db.emails.find({"message_id": {"$in": completed_ids}}, {"entities_referenced": 1})
    )
    knowledge_items_touched = db.knowledge_items.count_documents(
        {"source_emails": {"$in": completed_ids}}
    )
    reply_drafts_generated = db.reply_drafts.count_documents(
        {"source_email_id": {"$in": completed_ids}}
    )
    meetings_detected = db.meetings.count_documents({"thread_id": {"$in": list(thread_ids)}})
    calendar_actions_proposed = db.calendar_actions.count_documents(
        {"thread_id": {"$in": list(thread_ids)}}
    )

    return [
        f"Total messages found in file: {email_provider.total_found}",
        f"Messages skipped (malformed, never sent to pipeline): {email_provider.skipped_malformed}",
        f"Messages processed this run: {summary.processed}",
        f"Messages successfully completed: {summary.completed}",
        f"Messages already processed before (idempotent skip): {summary.skipped}",
        f"Messages failed: {summary.failed}",
        f"Threads touched: {len(thread_ids)}",
        f"Entities referenced (people/projects/commitments/follow_ups/meetings/personal): {entities_extracted}",
        f"Knowledge items created/updated: {knowledge_items_touched}",
        f"Reply drafts generated: {reply_drafts_generated}",
        f"Meetings detected (canonical entity layer): {meetings_detected}",
        f"Calendar actions proposed: {calendar_actions_proposed}",
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    if args.healthcheck:
        ok, lines = run_healthcheck(settings)
        print("\n".join(lines))
        return 0 if ok else 1

    if args.reset_demo:
        try:
            client = get_client(settings.mongodb_uri)
            db = initialize_database(client, settings.mongodb_database)
            run_reset_demo(db, settings)
            print("Demo data reset.")
            return 0
        except PermissionError as exc:
            print(f"Refused: {exc}")
            return 1

    if args.mode == "demo":
        client = get_client(settings.mongodb_uri)
        db = initialize_database(client, settings.mongodb_database)

        email_provider = ProviderFactory.create_email_provider(settings)
        llm_provider = ProviderFactory.create_llm_provider(settings)
        calendar_provider = ProviderFactory.create_calendar_provider(settings)

        summary = run_pipeline(db, email_provider, llm_provider, calendar_provider, settings)
        print(
            f"Demo run complete: processed={summary.processed} completed={summary.completed} "
            f"failed={summary.failed} skipped={summary.skipped}"
        )
        return 0

    if args.mode == "file":
        if not args.input:
            print("--mode=file requires --input <path to JSON export>")
            return 1
        client = get_client(settings.mongodb_uri)
        db = initialize_database(client, settings.mongodb_database)
        try:
            stats_lines = run_file_ingestion(db, settings, args.input)
        except (OSError, ValueError) as exc:
            print(f"File ingestion failed: {exc}")
            return 1
        print("File ingestion complete.")
        print("\n".join(stats_lines))
        return 0

    if args.mode == "raw-file":
        if not args.input:
            print("--mode=raw-file requires --input <path to JSON export>")
            return 1
        # Deliberately no ProviderFactory call anywhere on this path -- raw-file mode
        # never touches an LLM or calendar provider, so it never requires LLM_API_KEY.
        from app.raw_ingestion import run_raw_file_ingestion

        client = get_client(settings.mongodb_uri)
        db = initialize_database(client, settings.mongodb_database)
        try:
            summary = run_raw_file_ingestion(db, args.input)
        except (OSError, ValueError) as exc:
            print(f"Raw ingestion failed: {exc}")
            return 1
        print("## RAW INGESTION COMPLETE")
        print()
        print(f"Input messages:       {summary.input_messages}")
        print(f"Inserted:             {summary.inserted}")
        print(f"Already existed:      {summary.already_existed}")
        print(f"Skipped:              {summary.skipped}")
        print(f"Failed:               {summary.failed}")
        print(f"Unique threads:       {summary.unique_threads}")
        print("Claude API calls:     0")
        print("LLM calls:            0")
        if summary.failed_message_ids:
            print()
            print("Failed message IDs:")
            for message_id in summary.failed_message_ids:
                print(f"  {message_id}")
        return 0

    build_arg_parser().print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
