import logging
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from pymongo.database import Database

from app.config import get_settings
from app.db import JsonDoc, get_client, initialize_database
from app.models import Email, EmailLabel
from app import tools

mcp = FastMCP("raw-dump-labeling")


@lru_cache
def _get_db() -> Database:
    settings = get_settings()
    return initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)


@mcp.tool()
def ingest_raw_email_only(email: Email) -> JsonDoc:
    """Pure persistence, zero analysis. Stores a raw Gmail email into
    raw_emails_dump. Idempotent on source_message_id. Map Gmail fields:
    message_id, thread_id (omit if unknown), from/to/cc as {"name", "email"}
    objects, timestamp as ISO-8601, in_reply_to/references if available.

    If this email is from the configured agent_email and lands in a thread
    that already has an open "Needs reply*" item, that item is
    deterministically closed out (set to "1. Read only") automatically --
    see the returned closed_in_thread list, and update its real Gmail label
    to match.

    If this email is NOT from agent_email (an inbound message) and lands in
    a thread with already-classified siblings, those siblings become
    eligible for re-evaluation again -- see the returned reopened_in_thread
    list. Their label is untouched here; they'll resurface via
    get_next_unprocessed_raw_email for a fresh Claude read.
    """
    return tools.ingest_raw_email_only(_get_db(), email, get_settings())


@mcp.tool()
def get_next_unprocessed_raw_email() -> JsonDoc | None:
    """The oldest raw-dumped Gmail email in raw_emails_dump eligible for
    classification -- never-classified, classified under an older taxonomy
    version, whose prior claim lease expired, or just reopened by a new
    inbound message in the same thread. Atomically claimed, not just read --
    two concurrent callers can never be handed the same email. Returns None
    when nothing is eligible. Call this first in a labeling loop; None means
    stop.

    Also includes thread_context: every OTHER raw-dumped email sharing this
    one's thread_id, oldest first -- read it before deciding a label, so a
    re-evaluation reflects the WHOLE conversation (e.g. a later inbound
    "Thanks" closing what an earlier message opened), not just this one
    message read in isolation.
    """
    return tools.get_next_unprocessed_raw_email(_get_db())


@mcp.tool()
def get_next_unprocessed_raw_email_batch(batch_size: int = 10) -> list[JsonDoc]:
    """Same eligibility/atomicity as get_next_unprocessed_raw_email, but
    claims up to batch_size emails in ONE call instead of one call per
    email -- for a large run (e.g. 100 emails dumped via ingest_raw_email_only,
    then labelled in batches of 10), this cuts the claim round-trips from
    ~N to ~N/batch_size. Each returned document has its own thread_context.
    Returns [] when nothing is eligible -- stop the labeling loop. You still
    read and decide a label for each email individually; only the claim
    step batches, never the classification itself.
    """
    return tools.get_next_unprocessed_raw_email_batch(_get_db(), batch_size)


@mcp.tool()
def persist_raw_email_label(message_id: str, label_applied: EmailLabel) -> JsonDoc:
    """Records the classification label YOU already decided for one
    raw-dumped email. Overwrites the current label but appends to
    label_history for audit. Sets gmail_label_synced=False -- after updating
    the real Gmail label to match, call mark_gmail_label_synced to confirm
    it. Raises if message_id doesn't exist.
    """
    return tools.persist_raw_email_label(_get_db(), message_id, label_applied)


@mcp.tool()
def mark_raw_email_label_failed(message_id: str, reason: str) -> JsonDoc:
    """Records that classification was attempted but could not be completed.
    Releases the claim for retry, up to 3 attempts. Raises if message_id
    doesn't exist.
    """
    return tools.mark_raw_email_label_failed(_get_db(), message_id, reason)


@mcp.tool()
def mark_gmail_label_synced(message_id: str) -> JsonDoc:
    """Call ONLY after confirming the real Gmail label call actually
    succeeded for this message. A successful MongoDB label write is never
    proof the Gmail side changed too -- these are two separate writes.
    Raises if message_id doesn't exist.
    """
    return tools.mark_gmail_label_synced(_get_db(), message_id)


@mcp.tool()
def get_unsynced_labels(limit: int = 10) -> list[JsonDoc]:
    """Every raw-dumped email whose MongoDB label is already correct but
    whose real Gmail label is NOT confirmed to match (gmail_label_synced ==
    False). Closes a real gap: once a label is persisted, the claim tools
    (get_next_unprocessed_raw_email / _batch) never return that document
    again, even if the Gmail write that was supposed to follow it failed --
    this is the only way to find those stragglers again, from this run or a
    past one. Classification completion and Gmail-sync completion are
    separate states; this tool only ever reports on the second. Read-only,
    never reclassifies or touches classification_version; for each result,
    re-apply its already-decided label_applied in Gmail, then call
    mark_gmail_label_synced once that succeeds.
    """
    return tools.get_unsynced_labels(_get_db(), limit)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
