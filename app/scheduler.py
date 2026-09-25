"""Source-agnostic polling scheduler. Contains zero knowledge of *where* items come
from -- it only asks a Source (app/interfaces/source.py) for newly-discovered email
batches, runs each batch through the SAME app.pipeline.run_pipeline that --mode=demo
and --mode=file already use (full LLM analysis + entity extraction, not the LLM-free
app.raw_ingestion path), tells the Source the batch is done, and logs per-cycle
counts using PipelineRunSummary's already-computed fields.

Gmail ingestion is NOT handled here and is out of scope for this module. The only
working Gmail path today is external to this repo: a Cowork/Claude Desktop Gmail
connector fetches a message and calls this project's existing process_email MCP
tool (app/mcp/server.py), which is untouched by this module. This scheduler adds a
second, independent way to reach run_pipeline (via a local folder for now) -- it
does not replace or modify the Gmail/MCP path.

Markdown projection is explicitly out of scope for this phase. A future Markdown
projector's natural hook is right after `summary = run_pipeline(...)` below -- once
one exists, it can read summary.results for that batch's newly-COMPLETED emails,
with no change to the poll loop itself.

CLI usage:
    python -m app.scheduler            # poll forever, INGESTION_INTERVAL_MINUTES apart
    python -m app.scheduler --once     # run exactly one poll cycle and exit
"""

import argparse
import logging
import sys
import time

from pymongo.database import Database

from app.config.logging import configure_logging
from app.config.settings import Settings, get_settings
from app.database.mongodb import get_client, initialize_database
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.llm_provider import LLMProvider
from app.interfaces.source import Source
from app.pipeline import run_pipeline
from app.processing.models import PipelineRunSummary
from app.providers.email.mock import MockEmailProvider
from app.providers.factory import ProviderFactory
from app.providers.source.folder import FolderSource
from app.providers.source.live_gmail import LiveGmailSource

logger = logging.getLogger(__name__)


class Scheduler:
    def __init__(
        self,
        db: Database,
        source: Source,
        llm_provider: LLMProvider,
        calendar_provider: CalendarProvider,
        settings: Settings,
    ):
        self._db = db
        self._source = source
        self._llm_provider = llm_provider
        self._calendar_provider = calendar_provider
        self._settings = settings

    def run_once(self) -> list[PipelineRunSummary]:
        """Runs exactly one poll cycle. Never raises: discovery failures and
        per-batch failures are logged and the cycle ends/continues without
        propagating. Returns the PipelineRunSummary for each batch actually run
        through run_pipeline this cycle (possibly empty)."""
        logger.info("poll cycle starting")
        summaries: list[PipelineRunSummary] = []
        try:
            batches = self._source.get_new_batches()
        except Exception:
            logger.exception("poll cycle failed during discovery; will retry next cycle")
            return summaries

        for batch in batches:
            emails = batch["emails"]
            source_ref = batch["source_ref"]
            try:
                provider = MockEmailProvider(payloads=emails)
                # Every discovered email in this batch must be processed -- override
                # email_limit (default 50) the same way main.py's run_file_ingestion
                # already does for --mode=file, so a batch larger than the default
                # cap is never silently truncated.
                batch_settings = self._settings.model_copy(update={"email_limit": len(emails)})
                summary = run_pipeline(
                    self._db, provider, self._llm_provider, self._calendar_provider, batch_settings
                )
                # Extension point for a future Markdown projector: read
                # summary.results here for this batch's newly-COMPLETED emails.
                self._source.mark_batch_processed(source_ref, failed_count=summary.failed)
                summaries.append(summary)
                logger.info(
                    "batch ingested source_ref=%s discovered=%d processed=%d completed=%d "
                    "failed=%d skipped=%d",
                    source_ref, len(emails), summary.processed, summary.completed,
                    summary.failed, summary.skipped,
                )
            except Exception:
                logger.exception("batch failed source_ref=%s; will retry next cycle", source_ref)
                continue

        logger.info(
            "poll cycle complete: batches=%d total_processed=%d total_completed=%d "
            "total_failed=%d total_skipped=%d",
            len(summaries), sum(s.processed for s in summaries), sum(s.completed for s in summaries),
            sum(s.failed for s in summaries), sum(s.skipped for s in summaries),
        )
        return summaries

    def run_forever(self, interval_seconds: int) -> None:
        """Plain time.sleep()-based loop. No overlapping executions is guaranteed
        structurally: this is a single sequential loop with no threading/async, so
        iteration N+1 can never begin before iteration N's run_once() call has
        returned. An unexpected exception from run_once() itself is logged and the
        loop continues; Ctrl+C (KeyboardInterrupt) is a BaseException, not caught
        here, and propagates to the caller."""
        while True:
            try:
                self.run_once()
            except Exception:
                logger.exception("unexpected error in poll cycle; continuing")
            time.sleep(interval_seconds)


def build_source(db: Database, settings: Settings) -> Source:
    """Selects the Source implementation per settings.email_source. "gmail" is
    accepted and constructs a real LiveGmailSource -- but with no client (none exists
    to inject anywhere in this codebase today), so it will raise a clear
    RuntimeError the moment the scheduler actually polls, rather than silently doing
    nothing or pretending to work. See app/providers/source/live_gmail.py."""
    source_kind = settings.email_source.lower()
    if source_kind == "folder":
        return FolderSource(db, settings.ingestion_folder)
    if source_kind == "gmail":
        return LiveGmailSource(db, client=None)
    raise ValueError(f"Unknown EMAIL_SOURCE: {settings.email_source!r}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent -- folder ingestion scheduler")
    parser.add_argument(
        "--once", action="store_true",
        help="Run exactly one poll cycle and exit, instead of polling forever",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)

    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    llm_provider = ProviderFactory.create_llm_provider(settings)
    calendar_provider = ProviderFactory.create_calendar_provider(settings)
    source = build_source(db, settings)
    scheduler = Scheduler(db, source, llm_provider, calendar_provider, settings)

    if args.once:
        scheduler.run_once()
        return 0

    interval_seconds = settings.ingestion_interval_minutes * 60
    logger.info(
        "scheduler starting: folder=%s interval_minutes=%d",
        settings.ingestion_folder, settings.ingestion_interval_minutes,
    )
    try:
        scheduler.run_forever(interval_seconds)
    except KeyboardInterrupt:
        logger.info("scheduler stopped (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
