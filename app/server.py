import logging
from functools import lru_cache

from mcp.server.fastmcp import FastMCP
from pymongo.database import Database

from app.config import get_settings
from app.db import JsonDoc, get_client, initialize_database
from app.models import Email, EmailLabel
from app import tools

mcp = FastMCP("raw-dump-labeling")
logger = logging.getLogger(__name__)


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
def persist_raw_email_label(message_id: str, label_applied: EmailLabel, claim_id: str | None = None) -> JsonDoc:
    """Records the classification label YOU already decided for one
    raw-dumped email. Overwrites the current label but appends to
    label_history for audit. Sets gmail_label_synced=False -- after updating
    the real Gmail label to match, call mark_gmail_label_synced to confirm
    it.

    claim_id must be the claim_id field from whichever claim call (
    get_next_unprocessed_raw_email(_batch)) handed you this message_id --
    compare-and-swap, not an unconditional write. Returns
    {"ok": false, "reason": "claim_lost"} instead of writing if claim_id no
    longer matches (lease expired and reclaimed, or invalidated by a
    concurrent reopen/auto-close). Raises only if message_id doesn't exist
    at all.
    """
    return tools.persist_raw_email_label(_get_db(), message_id, label_applied, claim_id)


@mcp.tool()
def mark_raw_email_label_failed(message_id: str, reason: str, claim_id: str | None = None) -> JsonDoc:
    """Records that classification was attempted but could not be completed.
    Releases the claim for retry, up to 3 attempts.

    claim_id compare-and-swap, same contract as persist_raw_email_label --
    returns {"ok": false, "reason": "claim_lost"} if claim_id no longer
    matches. Raises only if message_id doesn't exist at all.
    """
    return tools.mark_raw_email_label_failed(_get_db(), message_id, reason, claim_id)


@mcp.tool()
def mark_gmail_label_synced(message_id: str, label_applied: str) -> JsonDoc:
    """Call ONLY after confirming the real Gmail label call actually
    succeeded for this message. A successful MongoDB label write is never
    proof the Gmail side changed too -- these are two separate writes.

    label_applied must be the exact label you just applied to the real
    Gmail message. This is a compare-and-swap: if the document's current
    label_applied has since changed (a relabel raced ahead of this call),
    gmail_label_synced is deliberately left False instead of being stamped
    True -- Gmail now holds a stale value against the newer decision, so it
    genuinely isn't synced. The returned document's label_applied tells you
    what to actually apply and retry.

    Raises if message_id doesn't exist.
    """
    return tools.mark_gmail_label_synced(_get_db(), message_id, label_applied)


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


@mcp.tool()
def get_stuck_emails(limit: int = 20) -> list[JsonDoc]:
    """Every raw-dumped email that tripped the circuit breaker
    (3 failed classification attempts) -- these stop being returned by
    get_next_unprocessed_raw_email(_batch) until reviewed. Read-only; does
    NOT reset anything. Recovery is deliberately not an MCP tool -- see
    scripts/reset_stuck.py (local-only, dry-run by default).
    """
    return tools.get_stuck_emails(_get_db(), limit)


def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if not settings.agent_email:
        logger.warning(
            "AGENT_EMAIL is not set -- auto-close of 'Needs reply*' siblings "
            "when the agent's own reply lands in a thread is DISABLED. Every "
            "threaded ingest, including the agent's own future replies, will "
            "take the reopen-for-reevaluation path instead. Set AGENT_EMAIL "
            "in .env to enable auto-close (see .env.example)."
        )
    mcp.run()


if __name__ == "__main__":
    main()
