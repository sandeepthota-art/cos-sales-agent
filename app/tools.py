import re
import uuid
from datetime import datetime, timedelta, timezone

from pymongo.database import Database

from app.config import Settings
from app.db import JsonDoc, RawEmailDumpRepository
from app.models import Email, EmailLabel
from app.text_clean import strip_quotes_and_signature
from app.threading import build_thread_candidates, resolve_thread_id_fallback

_EVIDENCE_MAX_LENGTH = 300

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


def _cleared_claim_fields() -> JsonDoc:
    """Shared by _close_open_needs_reply_in_thread and
    _reopen_thread_siblings_for_reevaluation -- any ingestion-driven write
    to a document invalidates whatever claim it currently holds, since the
    context a claimant is classifying from (thread_context snapshotted at
    claim time) just became stale. Merge this into every write either of
    those two make, unconditionally -- clearing an already-empty claim is a
    harmless no-op."""
    return {"label_claimed_at": "", "claim_id": None}


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
                **_cleared_claim_fields(),
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
        repo.update_many_by_key(
            {"message_id": {"$in": reopened_ids}}, {"classification_version": 0, **_cleared_claim_fields()}
        )
    return reopened_ids


def _clear_active_claims_in_thread(repo: RawEmailDumpRepository, thread_id: str, exclude_message_id: str) -> None:
    """Any new message landing in a thread makes every OTHER actively-claimed
    sibling's in-flight classification stale -- its thread_context snapshot
    (taken at claim time) doesn't include this new message. Clears claims
    UNCONDITIONALLY on every sibling currently claimed (claim_id set),
    regardless of label_applied/classification_version -- broader than
    _close_open_needs_reply_in_thread/_reopen_thread_siblings_for_reevaluation,
    which only clear claims on the subset of siblings THEIR OWN matching
    criteria touch. Without this, a sibling claimed for its first-ever
    classification (classification_version=0, label_applied=None) matches
    neither of those two functions' criteria, so nothing invalidated its
    claim even though a new thread message just made its context stale."""
    repo.update_many_by_key(
        {"thread_id": thread_id, "message_id": {"$ne": exclude_message_id}, "claim_id": {"$ne": None}},
        _cleared_claim_fields(),
    )


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

    If Gmail gave no threadId at all (rare), a NEW document attempts
    fallback resolution against existing raw_emails_dump documents
    (in_reply_to -> references -> subject+participants -> 14-day window,
    see app/threading.py) and reuses an existing thread_id if one matches --
    otherwise thread_id stays None, never synthesized. Once set (from Gmail
    or via fallback) at creation time, thread_id/source_thread_id are never
    touched again by a re-ingest of the same message -- recomputing them on
    every call could drift the match as more documents accumulate.

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
            "claim_id": None,
            "label_attempts": 0,
            "label_error": None,
            # No label exists yet, so there's nothing for Gmail to be out of
            # sync with -- True (in sync) until persist_raw_email_label/the
            # auto-close path below sets it False.
            "gmail_label_synced": True,
        }
        if not document.get("thread_id"):
            candidates = build_thread_candidates(repo.find_many({}))
            resolved = resolve_thread_id_fallback(email, candidates)
            if resolved:
                document["thread_id"] = resolved
                document["source_thread_id"] = resolved
    else:
        # Already decided at creation time (from Gmail or via fallback) --
        # never let a re-ingest of the same message recompute or overwrite it.
        document.pop("thread_id", None)
        document.pop("source_thread_id", None)
    result = repo.upsert_by_key({"source_message_id": email.source_message_id}, document)

    agent_email = (settings.agent_email or "").strip().lower()
    is_from_agent = bool(agent_email) and email.from_.email.strip().lower() == agent_email
    closed_ids: list[str] = []
    reopened_ids: list[str] = []
    thread_id = result.get("thread_id")
    if thread_id:
        _clear_active_claims_in_thread(repo, thread_id, result["message_id"])
        if is_from_agent:
            closed_ids = _close_open_needs_reply_in_thread(repo, thread_id, result["message_id"], _now_iso())
        else:
            reopened_ids = _reopen_thread_siblings_for_reevaluation(repo, thread_id, result["message_id"])
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
        claim_id=uuid.uuid4().hex,
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
        claim_id=uuid.uuid4().hex,
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
    the two can never drift on what "context" means.

    Also adds body_clean (claimed and every thread_context entry) --
    quote/signature-stripped, computed fresh here, NEVER written back to
    raw_emails_dump (Mode A's raw-preservation rule: stored subject/body
    are never altered). Purely a derived read-time view for the classifier."""
    claimed["body_clean"] = strip_quotes_and_signature(claimed.get("body") or "")
    siblings = (
        repo.siblings_in_thread(claimed["thread_id"], exclude_message_id=claimed["message_id"])
        if claimed.get("thread_id")
        else []
    )
    for sibling in siblings:
        sibling["body_clean"] = strip_quotes_and_signature(sibling.get("body") or "")
    claimed["thread_context"] = siblings


def _normalize_for_match(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _thread_searchable_text(repo: RawEmailDumpRepository, doc: JsonDoc) -> str:
    """Subject + body_clean of doc itself, plus subject + body_clean of
    every thread sibling -- the full text evidence is allowed to be quoted
    from. Recomputes body_clean fresh (never stored) rather than trusting
    anything the caller might have cached."""
    parts = [doc.get("subject") or "", strip_quotes_and_signature(doc.get("body") or "")]
    if doc.get("thread_id"):
        for sibling in repo.siblings_in_thread(doc["thread_id"], exclude_message_id=doc["message_id"]):
            parts.append(sibling.get("subject") or "")
            parts.append(strip_quotes_and_signature(sibling.get("body") or ""))
    return "\n".join(parts)


def persist_raw_email_label(
    db: Database, message_id: str, label_applied: EmailLabel, claim_id: str | None, evidence: str
) -> JsonDoc:
    """Records the classification label YOU already decided for one raw-dumped
    email. Overwrites label_applied/labelled_at outright but APPENDS to
    label_history (now including evidence), so a relabel is never lossy.
    Advances classification_version, releases the claim, resets the failure
    counter.

    evidence is REQUIRED: a short (<= 300 char) exact quote from text you
    actually read -- the email's own subject/body_clean, or any thread
    sibling's subject/body_clean. Checked case/whitespace-insensitively
    against that combined text; if it's not found anywhere in it, returns
    {"ok": False, "reason": "evidence_not_found"} instead of writing --
    checked BEFORE the claim_id compare-and-swap, so a bad quote never
    consumes or risks the claim. Raises ValueError if evidence is empty or
    over 300 characters -- a caller contract violation, not a normal
    rejection.

    claim_id is REQUIRED and MUST be the exact value returned by whichever
    claim call handed you this message_id (get_next_unprocessed_raw_email(
    _batch)'s claim_id field) -- pass claim_id=None explicitly only for a
    document you know was never claimed at all (its own claim_id is also
    None by default). This is a compare-and-swap: if the document's current
    claim_id no longer matches (your lease expired and someone else
    reclaimed it, or any new message landing in its thread invalidated your
    claim because the context you read is now stale), the write is
    REJECTED -- you get back {"ok": False, "reason": "claim_lost"} instead
    of silently overwriting newer or more-correct work.

    Sets gmail_label_synced=False -- this call only ever touches MongoDB;
    the caller must confirm the real Gmail label was also updated and call
    mark_gmail_label_synced, or this stays visibly out of sync.

    Raises ValueError if message_id doesn't exist in raw_emails_dump at all
    (a genuinely different failure mode from a lost claim).
    """
    if not evidence or not evidence.strip():
        raise ValueError("evidence is required and must be non-empty")
    if len(evidence) > _EVIDENCE_MAX_LENGTH:
        raise ValueError(f"evidence must be <= {_EVIDENCE_MAX_LENGTH} characters")

    repo = RawEmailDumpRepository(db)
    existing = repo.find_one({"message_id": message_id})
    if existing is None:
        raise ValueError(f"no raw-dumped email found for message_id={message_id!r}")

    searchable = _thread_searchable_text(repo, existing)
    if _normalize_for_match(evidence) not in _normalize_for_match(searchable):
        return {"ok": False, "reason": "evidence_not_found"}

    now_iso = _now_iso()
    history_entry = {
        "label_applied": label_applied,
        "labelled_at": now_iso,
        "classification_version": _CURRENT_LABEL_CLASSIFICATION_VERSION,
        "evidence": evidence,
    }
    set_fields = {
        "label_applied": label_applied,
        "labelled_at": now_iso,
        "classification_version": _CURRENT_LABEL_CLASSIFICATION_VERSION,
        "label_claimed_at": "",
        "claim_id": None,
        "label_attempts": 0,
        "label_error": None,
        "gmail_label_synced": False,
    }
    updated = repo.update_if_claim_matches(message_id, claim_id, set_fields, push_fields={"label_history": history_entry})
    if updated is not None:
        return updated
    return {"ok": False, "reason": "claim_lost"}


def mark_gmail_label_synced(db: Database, message_id: str, label_applied: str | None) -> JsonDoc:
    """Call this ONLY after confirming the real Gmail label call actually
    succeeded for this message -- a successful MongoDB write
    (persist_raw_email_label, or the deterministic auto-close path) is never
    proof the Gmail side also changed; those are two separate writes to two
    separate systems.

    label_applied MUST be the exact value you just applied to the real Gmail
    message (read it from whatever call handed you this message_id --
    persist_raw_email_label's return value, or a get_unsynced_labels/
    get_next_unprocessed_raw_email(_batch) document's label_applied field).
    This is a compare-and-swap, not an unconditional write: if the
    document's CURRENT label_applied no longer matches what you pass (a
    relabel raced ahead of you -- e.g. a catch-up retry overlapping a fresh
    Mode B pass on the same message), gmail_label_synced is deliberately
    left False and the CURRENT document is returned instead. That's correct,
    not a bug -- Gmail now holds a stale label against the newer decision,
    so it genuinely isn't synced; stamping it True anyway would hide a real
    gap. Re-read label_applied from what's returned and retry against the
    (now current) real Gmail label if you need to close it out.

    Raises ValueError if message_id doesn't exist in raw_emails_dump.
    """
    repo = RawEmailDumpRepository(db)
    _require_existing(repo, message_id)
    updated = repo.mark_synced_if_label_unchanged(message_id, label_applied)
    return updated if updated is not None else repo.find_one({"message_id": message_id})


def mark_raw_email_label_failed(db: Database, message_id: str, reason: str, claim_id: str | None) -> JsonDoc:
    """Records that classification was attempted but could not be completed.
    Increments label_attempts and releases the claim so a later run can
    retry it, up to _MAX_LABEL_ATTEMPTS. Never sets label_applied -- a
    failure is not a label.

    claim_id compare-and-swap, same contract as persist_raw_email_label --
    returns {"ok": False, "reason": "claim_lost"} if claim_id no longer
    matches, instead of silently recording a failure count against a claim
    you no longer actually hold.

    Raises ValueError if message_id doesn't exist in raw_emails_dump at all.
    """
    repo = RawEmailDumpRepository(db)
    existing = repo.find_one({"message_id": message_id})
    if existing is None:
        raise ValueError(f"no raw-dumped email found for message_id={message_id!r}")
    set_fields = {
        "label_error": reason,
        "label_attempts": existing.get("label_attempts", 0) + 1,
        "label_claimed_at": "",
        "claim_id": None,
    }
    updated = repo.update_if_claim_matches(message_id, claim_id, set_fields)
    if updated is not None:
        return updated
    return {"ok": False, "reason": "claim_lost"}


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


def get_stuck_emails(db: Database, limit: int = 20) -> list[JsonDoc]:
    """Every raw-dumped email that tripped the circuit breaker
    (label_attempts >= _MAX_LABEL_ATTEMPTS) -- SKILL.md promises these stop
    being returned by either claim tool "until a human reviews it
    directly", but until this tool existed there was no way to actually
    FIND them through the MCP surface (only direct DB access could). Purely
    read-only -- recovery (resetting label_attempts) is deliberately NOT an
    MCP tool, see scripts/reset_stuck.py, a local-only dry-run-by-default
    script, never reachable from a scheduled task.
    """
    repo = RawEmailDumpRepository(db)
    return repo.find_many(
        {"label_attempts": {"$gte": _MAX_LABEL_ATTEMPTS}}, limit=limit, sort=[("message_id", 1)]
    )
