from datetime import datetime, timedelta, timezone

from pymongo.database import Database

from app.config import Settings
from app.db import JsonDoc, RawEmailDumpRepository
from app.models import Email, EmailLabel

# _CURRENT_LABEL_CLASSIFICATION_VERSION -- bump this when the six-label
# taxonomy or classification instructions change meaningfully. Every
# raw-dumped email whose own classification_version is lower becomes
# eligible for re-evaluation again, automatically -- "label once" is never
# "label forever."
_CURRENT_LABEL_CLASSIFICATION_VERSION = 1
# A claim (label_claimed_at) older than this is treated as abandoned -- a
# crashed or interrupted skill run never permanently locks an email out of
# future classification.
_LABEL_CLAIM_TTL_MINUTES = 10
# Circuit breaker: an email that has failed classification this many times
# (mark_raw_email_label_failed) stops being surfaced by
# get_next_unprocessed_raw_email until a human intervenes.
_MAX_LABEL_ATTEMPTS = 3
# The three labels that mean "something is pending from us" -- a reply we
# send into the thread closes any of these out deterministically (see
# _close_open_needs_reply_in_thread). "1. Read only", "1. Delete",
# "1. Undecided" are never reopened this way -- there was nothing pending on
# them to begin with.
_NEEDS_REPLY_LABELS = ["1. Needs reply: ASAP", "1. Needs reply", "1. Needs reply: mention"]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_existing(repo: RawEmailDumpRepository, message_id: str) -> JsonDoc:
    """Every write in this module that targets an EXISTING document (as
    opposed to ingest_raw_email_only, which may create one) needs this exact
    check first -- raise, don't silently no-op, so a caller never mistakes
    "nothing happened" for "it worked"."""
    existing = repo.find_one({"message_id": message_id})
    if existing is None:
        raise ValueError(f"no raw-dumped email found for message_id={message_id!r}")
    return existing


def _close_open_needs_reply_in_thread(
    repo: RawEmailDumpRepository, thread_id: str, exclude_message_id: str, now_iso: str
) -> list[str]:
    """Deterministically closes every OTHER email in this thread whose
    current label is one of _NEEDS_REPLY_LABELS -- "a reply from us just
    landed in this thread, so whatever was pending from us is no longer
    pending" is a structural fact, not a judgment call, so this never
    invokes Claude/an LLM, same principle as every other deterministic write
    in this module.

    Sets label_applied="1. Read only", appends a label_history entry (never
    overwrites history), and does NOT touch classification_version -- this
    is a correction to an existing classification, not a fresh Claude
    re-evaluation, so it never makes the document eligible for
    get_next_unprocessed_raw_email's claim query again on this account
    alone.

    Returns the message_ids actually closed (for the caller's report).
    """
    candidates = repo.find_many(
        {"thread_id": thread_id, "message_id": {"$ne": exclude_message_id}, "label_applied": {"$in": _NEEDS_REPLY_LABELS}}
    )
    closed_ids = []
    for doc in candidates:
        history_entry = {
            "label_applied": "1. Read only",
            "labelled_at": now_iso,
            "classification_version": doc.get("classification_version", 0),
            "basis": f"agent reply {exclude_message_id} landed in this thread",
        }
        history = list(doc.get("label_history") or []) + [history_entry]
        repo.upsert_by_key(
            {"message_id": doc["message_id"]},
            {
                "label_applied": "1. Read only",
                "labelled_at": now_iso,
                "label_history": history,
                # The real Gmail label hasn't been touched by THIS write --
                # only the MongoDB side changed, so this document now
                # genuinely disagrees with Gmail until the caller confirms
                # the Gmail update too (see mark_gmail_label_synced).
                "gmail_label_synced": False,
            },
        )
        closed_ids.append(doc["message_id"])
    return closed_ids


def _reopen_thread_siblings_for_reevaluation(
    repo: RawEmailDumpRepository, thread_id: str, exclude_message_id: str
) -> list[str]:
    """A new INBOUND message (not from the agent) landing in a known thread
    means the conversation changed -- every OTHER already-classified email in
    that thread (any label, not just "Needs reply*") needs a fresh look,
    since an inbound "Thanks" can close a loop just as easily as an inbound
    question can reopen one. Reuses the exact same eligibility mechanism
    classification_version already provides for a global taxonomy-version
    bump -- resetting it to 0 here makes these documents reappear in
    claim_next_unclassified's normal query, with no separate queue/staging
    state needed.

    Never touches label_applied itself -- Claude decides the new label the
    next time it's claimed, same as any other eligible document. Returns the
    message_ids reopened (for the caller's report).
    """
    candidates = repo.find_many(
        {"thread_id": thread_id, "message_id": {"$ne": exclude_message_id}, "classification_version": {"$gte": 1}}
    )
    reopened_ids = [doc["message_id"] for doc in candidates]
    if reopened_ids:
        repo.update_many_by_key({"message_id": {"$in": reopened_ids}}, {"classification_version": 0})
    return reopened_ids


def ingest_raw_email_only(db: Database, email: Email, settings: Settings) -> JsonDoc:
    """Pure persistence, zero analysis. Stores the raw Gmail email exactly as
    received into its own `raw_emails_dump` collection. Idempotent: upserts by
    source_message_id, so ingesting the same Gmail message twice updates the
    same document rather than creating a duplicate.

    Writes sort_key -- email.timestamp (already a parsed, possibly
    non-UTC-offset datetime) normalized to UTC ONCE here at write time, so
    get_next_unprocessed_raw_email can sort natively in MongoDB without a
    chronological-correctness trade-off (two emails with different UTC
    offsets would sort WRONG on the raw timestamp string otherwise).

    Classification state is initialized to explicit defaults ONLY the first
    time a document is created -- re-ingesting an already-dumped message must
    never reset classification progress already made on it.

    If this email is FROM settings.agent_email and belongs to a thread, every
    OTHER email in that thread currently on a "Needs reply*" label is closed
    out deterministically (see _close_open_needs_reply_in_thread) -- a
    structural fact, not Claude's judgment. If this email is INBOUND (not
    from the agent) and belongs to a thread, every OTHER already-classified
    email in that thread becomes eligible for re-evaluation again (see
    _reopen_thread_siblings_for_reevaluation) -- the conversation changed,
    so a stale label shouldn't stay frozen.
    """
    repo = RawEmailDumpRepository(db)
    document = email.model_dump(mode="json", by_alias=True)
    document["source"] = "gmail"
    document["ingested_at"] = _now_iso()
    document["sort_key"] = email.timestamp.astimezone(timezone.utc).isoformat()

    is_new = repo.find_one({"source_message_id": email.source_message_id}) is None
    if is_new:
        document |= {
            "classification_version": 0,
            "label_applied": None,
            "labelled_at": None,
            "label_history": [],
            # "" (never None) is the "not currently claimed" sentinel -- see
            # RawEmailDumpRepository.claim_next_unclassified's own docstring.
            "label_claimed_at": "",
            "label_attempts": 0,
            "label_error": None,
            # No label exists yet, so there's nothing for Gmail to be out of
            # sync with -- True (in sync) until persist_raw_email_label/the
            # auto-close path below sets it False.
            "gmail_label_synced": True,
        }
    result = repo.upsert_by_key({"source_message_id": email.source_message_id}, document)

    agent_email = (settings.agent_email or "").strip().lower()
    is_from_agent = bool(agent_email) and email.from_.email.strip().lower() == agent_email
    closed_ids: list[str] = []
    reopened_ids: list[str] = []
    if email.thread_id:
        if is_from_agent:
            closed_ids = _close_open_needs_reply_in_thread(repo, email.thread_id, result["message_id"], _now_iso())
        else:
            reopened_ids = _reopen_thread_siblings_for_reevaluation(repo, email.thread_id, result["message_id"])
    # Surfaced so the caller (the skill) knows what else needs attention:
    # closed_in_thread needs its real Gmail label updated to match;
    # reopened_in_thread will simply resurface via
    # get_next_unprocessed_raw_email on the next Mode B pass.
    result["closed_in_thread"] = closed_ids
    result["reopened_in_thread"] = reopened_ids
    return result


def get_next_unprocessed_raw_email(db: Database) -> JsonDoc | None:
    """The oldest (by sort_key) raw-dumped Gmail email eligible for
    classification -- never-classified, stale-classified, whose prior claim
    lease expired, or just reopened by a new inbound message in its thread --
    ATOMICALLY claimed in the same operation. Returns None when nothing is
    eligible. Never touches Gmail, never calls an LLM.

    Also attaches thread_context -- every OTHER raw-dumped email sharing this
    one's thread_id, oldest first -- so a caller re-evaluating this message
    can read the WHOLE conversation, not just this one message in isolation.
    Empty list when thread_id is unset or this is the only email on record
    for that thread.
    """
    repo = RawEmailDumpRepository(db)
    now = datetime.now(timezone.utc)
    claim_cutoff_iso = (now - timedelta(minutes=_LABEL_CLAIM_TTL_MINUTES)).isoformat()
    claimed = repo.claim_next_unclassified(
        max_classification_version=_CURRENT_LABEL_CLASSIFICATION_VERSION,
        claim_cutoff_iso=claim_cutoff_iso,
        max_attempts=_MAX_LABEL_ATTEMPTS,
        now_iso=now.isoformat(),
    )
    if claimed is None:
        return None
    _attach_thread_context(repo, claimed)
    return claimed


def get_next_unprocessed_raw_email_batch(db: Database, batch_size: int = 10) -> list[JsonDoc]:
    """Same eligibility and atomicity guarantees as get_next_unprocessed_raw_email,
    but claims up to batch_size emails in ONE round trip instead of one call
    per email -- for a 100-email run, calling this in batches of 10 (the
    default) means ~10 claim calls instead of ~100. Each returned document
    still carries its own thread_context, same as the single-claim version;
    Claude still reads and decides each one individually -- only the CLAIM
    step batches, never the classification decision itself.

    Returns [] when nothing is eligible (same meaning as None from the
    single-claim version -- stop the labeling loop).
    """
    repo = RawEmailDumpRepository(db)
    now = datetime.now(timezone.utc)
    claim_cutoff_iso = (now - timedelta(minutes=_LABEL_CLAIM_TTL_MINUTES)).isoformat()
    claimed_batch = repo.claim_batch_unclassified(
        max_classification_version=_CURRENT_LABEL_CLASSIFICATION_VERSION,
        claim_cutoff_iso=claim_cutoff_iso,
        max_attempts=_MAX_LABEL_ATTEMPTS,
        now_iso=now.isoformat(),
        batch_size=batch_size,
    )
    for claimed in claimed_batch:
        _attach_thread_context(repo, claimed)
    return claimed_batch


def _attach_thread_context(repo: RawEmailDumpRepository, claimed: JsonDoc) -> None:
    """Mutates `claimed` in place, adding thread_context -- every OTHER
    raw-dumped email sharing its thread_id, oldest first, or [] when
    thread_id is unset. Shared by both the single and batch claim paths so
    the two can never drift on what "context" means."""
    claimed["thread_context"] = (
        repo.siblings_in_thread(claimed["thread_id"], exclude_message_id=claimed["message_id"])
        if claimed.get("thread_id")
        else []
    )


def persist_raw_email_label(db: Database, message_id: str, label_applied: EmailLabel) -> JsonDoc:
    """Records the classification label YOU already decided for one raw-dumped
    email. Overwrites label_applied/labelled_at outright but APPENDS to
    label_history, so a relabel is never lossy. Advances
    classification_version, releases the claim, resets the failure counter.

    Sets gmail_label_synced=False -- this call only ever touches MongoDB;
    the caller must confirm the real Gmail label was also updated and call
    mark_gmail_label_synced, or this stays visibly out of sync.

    Raises ValueError if message_id doesn't exist in raw_emails_dump.
    """
    repo = RawEmailDumpRepository(db)
    existing = _require_existing(repo, message_id)
    now_iso = _now_iso()
    history_entry = {
        "label_applied": label_applied,
        "labelled_at": now_iso,
        "classification_version": _CURRENT_LABEL_CLASSIFICATION_VERSION,
    }
    history = list(existing.get("label_history") or []) + [history_entry]
    return repo.upsert_by_key(
        {"message_id": message_id},
        {
            "label_applied": label_applied,
            "labelled_at": now_iso,
            "classification_version": _CURRENT_LABEL_CLASSIFICATION_VERSION,
            "label_history": history,
            "label_claimed_at": "",
            "label_attempts": 0,
            "label_error": None,
            "gmail_label_synced": False,
        },
    )


def mark_gmail_label_synced(db: Database, message_id: str) -> JsonDoc:
    """Call this ONLY after confirming the real Gmail label call actually
    succeeded for this message -- a successful MongoDB write
    (persist_raw_email_label, or the deterministic auto-close path) is never
    proof the Gmail side also changed; those are two separate writes to two
    separate systems. Sets gmail_label_synced=True.

    Raises ValueError if message_id doesn't exist in raw_emails_dump.
    """
    repo = RawEmailDumpRepository(db)
    _require_existing(repo, message_id)
    return repo.upsert_by_key({"message_id": message_id}, {"gmail_label_synced": True})


def mark_raw_email_label_failed(db: Database, message_id: str, reason: str) -> JsonDoc:
    """Records that classification was attempted but could not be completed.
    Increments label_attempts and releases the claim so a later run can
    retry it, up to _MAX_LABEL_ATTEMPTS. Never sets label_applied -- a
    failure is not a label.

    Raises ValueError if message_id doesn't exist in raw_emails_dump.
    """
    repo = RawEmailDumpRepository(db)
    existing = _require_existing(repo, message_id)
    return repo.upsert_by_key(
        {"message_id": message_id},
        {
            "label_error": reason,
            "label_attempts": existing.get("label_attempts", 0) + 1,
            "label_claimed_at": "",
        },
    )


def get_unsynced_labels(db: Database, limit: int = 10) -> list[JsonDoc]:
    """Every raw-dumped email whose MongoDB label_applied is correct but
    whose real Gmail label is NOT confirmed to match (gmail_label_synced ==
    False) -- a real gap get_next_unprocessed_raw_email/_batch cannot catch:
    once persist_raw_email_label succeeds, classification_version becomes
    current and the claim queries will NEVER return that document again,
    even if the Gmail write that was supposed to follow it failed. This is
    the only way to find those again -- classification completion
    (classification_version/label_applied) and Gmail-sync completion
    (gmail_label_synced) are separate states; this tool exposes outstanding
    work in the second, never the first.

    Read-only, no claim/lease semantics, never touches classification_version
    or label_applied -- a caller re-applies the Gmail label for each returned
    document (its label_applied is already the correct, decided value;
    nothing to reclassify) and calls mark_gmail_label_synced once the Gmail
    call actually succeeds. limit is applied server-side (via find_many), so
    a large backlog is never pulled into memory just to return `limit` of it.

    Ordered oldest-stuck-first by labelled_at (set by both
    persist_raw_email_label and the auto-close path whenever they set
    gmail_label_synced=False), message_id as a stable tie-breaker -- without
    this, repeated calls over a backlog larger than `limit` have no
    guaranteed progress (the same unordered page could repeat, or different
    arbitrary subsets could appear each call).
    """
    repo = RawEmailDumpRepository(db)
    return repo.find_many(
        {"gmail_label_synced": False}, limit=limit, sort=[("labelled_at", 1), ("message_id", 1)]
    )
