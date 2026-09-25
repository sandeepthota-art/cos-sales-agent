"""Live Gmail ingestion source -- implements the same Source abstraction as
FolderSource (app/providers/source/folder.py), so Scheduler can poll either without
any Gmail-specific branching in the scheduler itself.

IMPORTANT -- read before setting EMAIL_SOURCE=gmail in any real environment:

This class requires an injected `client` capable of listing/searching live Gmail
messages. NO such client exists anywhere in this codebase today. Confirmed by
inspection: there is no MCP *client* import anywhere in app/ (only
mcp.server.fastmcp.FastMCP, which makes this repo an MCP *server*); the existing
app/providers/email/mcp.py has never had a real client wired into it; and every tool
this project's own MCP server exposes either requires an already-fetched Email as
input (process_email) or only reads the already-ingested MongoDB copy
(search_emails, get_thread, list_processed_emails, ...), never live Gmail. The only
working Gmail path in this system remains entirely external: a Cowork/Claude Desktop
Gmail connector fetches a message and calls this project's process_email MCP tool --
there is no way for a standalone background process to invoke that connector itself.

Until a real client is supplied, get_new_batches() raises RuntimeError -- the exact
same fail-loud convention app/providers/email/mcp.py already established, rather than
silently returning "no new emails" (which would misrepresent "not configured" as
"nothing to do") or fabricating fake data.

client contract: client.list_messages(limit: int) -> list[dict], each dict shaped
exactly like a Gmail-export message (id, threadId, subject, sender, toRecipients,
ccRecipients, date, labelIds, plaintextBody) -- the same shape
app.providers.email.file.FileEmailProvider already normalizes. Normalization is
reused via app.providers.email.file.convert_gmail_message, not reimplemented.

Duplicate protection: this source does NOT introduce a second identity/tracking
system. It pre-filters against the exact same signal app.pipeline.run_pipeline
already uses to skip an email -- emails.processing_status.stage == "COMPLETED" in the
SAME emails collection -- which is also what protects the 662-email historical
baseline from ever being reprocessed if a Gmail search happens to return them again.
This is a pure efficiency pre-filter; run_pipeline's own check remains the real
correctness guarantee even if this pre-filter were skipped entirely.
"""

import logging
from typing import Any

from pymongo.database import Database

from app.database.repositories import EmailRepository
from app.interfaces.source import Source
from app.providers.email.file import convert_gmail_message

logger = logging.getLogger(__name__)


class LiveGmailSource(Source):
    def __init__(self, db: Database, client: Any = None, batch_limit: int = 50):
        self._email_repo = EmailRepository(db)
        self._client = client
        self._batch_limit = batch_limit

    def get_new_batches(self) -> list[dict[str, Any]]:
        if self._client is None:
            raise RuntimeError(
                "Live Gmail source is not configured -- no client capable of listing/"
                "searching Gmail messages is available in this environment. The "
                "current Cowork/MCP interface exposes process_email (a single "
                "already-fetched email) but no list/search/fetch operation a "
                "background scheduler can call on its own. Set EMAIL_SOURCE=folder "
                "(the default) until a real client is wired here."
            )

        raw_messages = self._client.list_messages(limit=self._batch_limit)
        new_emails: list[dict[str, Any]] = []
        skipped_malformed = 0
        skipped_already_completed = 0

        for message in raw_messages:
            converted = convert_gmail_message(message)
            if converted is None:
                skipped_malformed += 1
                continue
            existing = self._email_repo.find_one({"message_id": converted["message_id"]})
            if existing and (existing.get("processing_status") or {}).get("stage") == "COMPLETED":
                skipped_already_completed += 1
                continue
            new_emails.append(converted)

        if skipped_malformed:
            logger.warning("live gmail source: skipped %d malformed message(s)", skipped_malformed)
        if skipped_already_completed:
            logger.info(
                "live gmail source: skipped %d already-completed message(s) "
                "(includes historical baseline overlap, if any)",
                skipped_already_completed,
            )

        if not new_emails:
            return []
        return [
            {
                "source_ref": {"kind": "live_gmail", "message_ids": [e["message_id"] for e in new_emails]},
                "emails": new_emails,
            }
        ]

    def mark_batch_processed(self, source_ref: Any, failed_count: int) -> None:
        # Deliberate no-op: unlike FolderSource (which tracks per-*file* completion
        # because a file has no completion marker of its own), a Gmail message's
        # completion state already lives entirely in emails.processing_status -- there
        # is nothing additional to record without creating a second, competing
        # identity/tracking system.
        pass
