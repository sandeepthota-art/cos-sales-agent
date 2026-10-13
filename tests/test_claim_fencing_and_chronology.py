"""Phase 2: claim fencing (STEP 3, implemented) and chronology (implemented).

Implemented: claim_id returned by claim tools, persist_raw_email_label /
mark_raw_email_label_failed compare-and-swap on it (returns
{"ok": False, "reason": "claim_lost"} on mismatch instead of silently
overwriting), reopen/auto-close clear a document's claim via the shared
_cleared_claim_fields helper, batch claim is a loop of atomic
single-document claims (no more find()-then-update_many TOCTOU window).

Chronology: auto-close only closes a "Needs reply*" sibling strictly
EARLIER than the closing agent reply by (sort_key, source_message_id) --
a sibling sent AFTER the reply (by that same ordering) is never touched,
regardless of ingestion call order. See docs/DECISIONS.md.
"""

from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app import tools
from app.config import Settings
from app.db import RawEmailDumpRepository
from app.models import parse_email


@pytest.fixture
def db():
    return mongomock.MongoClient().db


def _settings(**overrides):
    defaults = {"agent_email": "agent@ourcompany.example"}
    defaults.update(overrides)
    return Settings(**defaults)


def _email_doc(message_id: str, **overrides):
    doc = {
        "message_id": message_id,
        "from": {"name": "Jane", "email": "jane@customerco.example"},
        "to": [{"name": "Agent", "email": "agent@ourcompany.example"}],
        "subject": "Renewal pricing",
        "body": "Can we discuss renewal pricing for next quarter?",
        "timestamp": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _ingest(db, settings, message_id: str, **overrides) -> dict:
    return tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id, **overrides)), settings)


# --- 1. stale-writer persist after lease expiry -------------------------------------


def test_stale_writer_persist_after_lease_expiry_is_rejected(db):
    """Worker A claims MSG-1, its lease expires, Worker B reclaims and
    persists. Worker A -- still alive, unaware -- must NOT be able to
    overwrite Worker B's fresher decision with its stale one."""
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    claimed_by_a = tools.get_next_unprocessed_raw_email(db)
    claim_id_a = claimed_by_a["claim_id"]

    repo = RawEmailDumpRepository(db)
    stale_claim_time = (datetime.now(timezone.utc) - timedelta(minutes=tools._LABEL_CLAIM_TTL_MINUTES + 1)).isoformat()
    repo.upsert_by_key({"message_id": "MSG-1"}, {"label_claimed_at": stale_claim_time})

    claimed_by_b = tools.get_next_unprocessed_raw_email(db)
    claim_id_b = claimed_by_b["claim_id"]
    tools.persist_raw_email_label(db, "MSG-1", "1. Read only", claim_id=claim_id_b, evidence="renewal pricing")

    # Worker A, unaware it was reclaimed, tries to persist its own stale decision.
    result = tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply: ASAP", claim_id=claim_id_a, evidence="renewal pricing")

    assert result == {"ok": False, "reason": "claim_lost"}
    final = RawEmailDumpRepository(db).find_one({"message_id": "MSG-1"})
    assert final["label_applied"] == "1. Read only"  # Worker B's decision survives


# --- 2. claim-token mismatch (wrong/unknown token, no expiry involved) --------------


def test_persist_rejects_an_unknown_claim_id(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")
    tools.get_next_unprocessed_raw_email(db)

    result = tools.persist_raw_email_label(db, "MSG-1", "1. Read only", claim_id="bogus-claim-id", evidence="renewal pricing")

    assert result == {"ok": False, "reason": "claim_lost"}


def test_mark_failed_rejects_an_unknown_claim_id(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")
    tools.get_next_unprocessed_raw_email(db)

    result = tools.mark_raw_email_label_failed(db, "MSG-1", "garbled", claim_id="bogus-claim-id")

    assert result == {"ok": False, "reason": "claim_lost"}


# --- 3. ANY new thread message clears every actively-claimed sibling's claim --------
# Corrected from the original STEP 1 finding (b): reopen specifically can't
# race a claimed doc (claim requires classification_version=0, reopen only
# matches version>=1 -- mutually exclusive by construction). The REAL gap
# was narrower than first described: a sibling claimed for its FIRST-EVER
# classification (version=0, label_applied=None) matches neither
# _close_open_needs_reply_in_thread's nor _reopen_thread_siblings_for_reevaluation's
# own criteria, so nothing invalidated its claim when a new thread message
# arrived. _clear_active_claims_in_thread (app/tools.py) now runs
# unconditionally on every threaded ingest to close that gap.


def test_new_thread_message_clears_the_claim_of_any_actively_claimed_sibling(db):
    settings = _settings()
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    claimed = tools.get_next_unprocessed_raw_email(db)  # first-ever classification, version=0
    assert claimed["message_id"] == "MSG-ASK"
    claim_id = claimed["claim_id"]

    # A new inbound message arrives in the same thread while MSG-ASK is claimed.
    _ingest(db, settings, "MSG-NEW", thread_id="T-1", timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc))

    result = tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply", claim_id=claim_id, evidence="renewal pricing")
    assert result == {"ok": False, "reason": "claim_lost"}

    # Immediately reclaimable (claim was cleared, not just expired). Claim
    # order is newest-first, so MSG-NEW (the newer sibling) comes first;
    # draining a second time reclaims MSG-ASK, with fresh thread_context
    # that now includes the new sibling.
    first_reclaim = tools.get_next_unprocessed_raw_email(db)
    assert first_reclaim["message_id"] == "MSG-NEW"
    reclaimed = tools.get_next_unprocessed_raw_email(db)
    assert reclaimed["message_id"] == "MSG-ASK"
    assert [s["message_id"] for s in reclaimed["thread_context"]] == ["MSG-NEW"]


# --- 4. auto-close invalidates the claim of a claimed sibling ------------------------


def test_auto_close_clears_the_claim_of_a_concurrently_claimed_sibling(db):
    """MSG-ASK is claimed for re-evaluation. While claimed, the agent's own
    reply lands in the thread and deterministically auto-closes MSG-ASK
    (still label_applied='1. Needs reply' at that point). That auto-close
    must invalidate MSG-ASK's claim -- the claimant's eventual persist
    would otherwise clobber the auto-close with stale judgment."""
    settings = _settings()
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply", claim_id=None, evidence="renewal pricing")
    _ingest(db, settings, "MSG-REOPEN", thread_id="T-1", timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc))

    # Claim order is newest-first: MSG-REOPEN (Aug 2) is claimed before the
    # reopened MSG-ASK (Aug 1).
    first_claim = tools.get_next_unprocessed_raw_email(db)
    assert first_claim["message_id"] == "MSG-REOPEN"
    claimed = tools.get_next_unprocessed_raw_email(db)
    assert claimed["message_id"] == "MSG-ASK"
    claim_id = claimed["claim_id"]

    _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    result = tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply: ASAP", claim_id=claim_id, evidence="renewal pricing")

    assert result == {"ok": False, "reason": "claim_lost"}
    final = RawEmailDumpRepository(db).find_one({"message_id": "MSG-ASK"})
    assert final["label_applied"] == "1. Read only"  # auto-close's decision stands


# --- 5. batch claim: shared claim_id, no TOCTOU window --------------------------------


def test_batch_claimed_documents_carry_a_shared_claim_id(db):
    """Batch claim shares ONE claim_id across the whole batch, with each
    slot still claimed via its own atomic find_one_and_update underneath
    (see RawEmailDumpRepository.claim_batch_unclassified)."""
    settings = _settings()
    for i in range(3):
        _ingest(db, settings, f"MSG-{i}", timestamp=datetime(2026, 8, 1 + i, tzinfo=timezone.utc))

    batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=3)

    claim_ids = {doc["claim_id"] for doc in batch}
    assert len(claim_ids) == 1
    assert all(doc["claim_id"] is not None for doc in batch)


def test_batch_claim_never_reclaims_a_document_another_claim_already_holds(db):
    """Regression for the find()-then-update_many TOCTOU window: claiming a
    batch, then immediately claiming again, must never return a document
    from the first batch -- each slot's claim_next_unclassified call is
    atomic, so a document claimed by slot 1 is immediately ineligible for
    slot 2 within the SAME batch call, and for any later batch call."""
    settings = _settings()
    for i in range(5):
        _ingest(db, settings, f"MSG-{i}", timestamp=datetime(2026, 8, 1 + i, tzinfo=timezone.utc))

    first_batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=3)
    second_batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=3)

    first_ids = {d["message_id"] for d in first_batch}
    second_ids = {d["message_id"] for d in second_batch}
    assert first_ids.isdisjoint(second_ids)
    assert len(first_batch) == 3
    assert len(second_batch) == 2


# --- 6. retry-cap visibility ----------------------------------------------------------


def test_get_stuck_emails_surfaces_documents_at_the_retry_cap(db):
    """get_stuck_emails surfaces every document with label_attempts >=
    _MAX_LABEL_ATTEMPTS -- closing the gap where SKILL.md promised
    human-reviewability with no tool to actually find them."""
    settings = _settings()
    _ingest(db, settings, "MSG-STUCK")
    for _ in range(tools._MAX_LABEL_ATTEMPTS):
        claimed = tools.get_next_unprocessed_raw_email(db)
        tools.mark_raw_email_label_failed(db, "MSG-STUCK", "still garbled", claim_id=claimed["claim_id"])

    stuck = tools.get_stuck_emails(db)

    assert [d["message_id"] for d in stuck] == ["MSG-STUCK"]
    assert stuck[0]["label_attempts"] == tools._MAX_LABEL_ATTEMPTS


def test_get_stuck_emails_excludes_documents_below_the_cap(db):
    settings = _settings()
    _ingest(db, settings, "MSG-ONE-FAILURE")
    claimed = tools.get_next_unprocessed_raw_email(db)
    tools.mark_raw_email_label_failed(db, "MSG-ONE-FAILURE", "garbled", claim_id=claimed["claim_id"])

    assert tools.get_stuck_emails(db) == []


# --- 7. chronology ---------------------------------------------------------------------


def test_auto_close_closes_a_sibling_sent_before_the_agent_reply(db):
    """A sibling sent (by sort_key) BEFORE the agent's closing reply is
    closed, same as always."""
    settings = _settings()
    _ingest(db, settings, "MSG-SIBLING-EARLIER", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-SIBLING-EARLIER", "1. Needs reply", claim_id=None, evidence="renewal pricing")

    result = _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == ["MSG-SIBLING-EARLIER"]
    sibling = RawEmailDumpRepository(db).find_one({"message_id": "MSG-SIBLING-EARLIER"})
    assert sibling["label_applied"] == "1. Read only"


def test_auto_close_never_closes_a_sibling_sent_after_the_agent_reply(db):
    """A sibling sent (by sort_key) AFTER the agent's closing reply must
    NEVER be auto-closed by it -- label, gmail_label_synced, and claim
    fields all stay exactly as they were."""
    settings = _settings()
    _ingest(db, settings, "MSG-SIBLING-LATER", thread_id="T-1", timestamp=datetime(2026, 8, 5, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-SIBLING-LATER", "1. Needs reply", claim_id=None, evidence="renewal pricing")
    before = RawEmailDumpRepository(db).find_one({"message_id": "MSG-SIBLING-LATER"})

    result = _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == []
    sibling = RawEmailDumpRepository(db).find_one({"message_id": "MSG-SIBLING-LATER"})
    assert sibling["label_applied"] == "1. Needs reply"
    assert sibling["gmail_label_synced"] == before["gmail_label_synced"]
    assert sibling["claim_id"] == before["claim_id"]
    assert sibling["label_claimed_at"] == before["label_claimed_at"]


def test_auto_close_tie_on_sort_key_resolved_by_source_message_id(db):
    """Two siblings share the agent reply's exact sort_key -- the tie is
    resolved on source_message_id: strictly-less closes, strictly-greater-or-
    equal does not."""
    settings = _settings()
    tied_ts = datetime(2026, 8, 3, tzinfo=timezone.utc)
    _ingest(
        db, settings, "MSG-SIB-EARLY", thread_id="T-1", timestamp=tied_ts, source_message_id="AAA-EARLY"
    )
    tools.persist_raw_email_label(db, "MSG-SIB-EARLY", "1. Needs reply", claim_id=None, evidence="renewal pricing")
    _ingest(
        db, settings, "MSG-SIB-LATE", thread_id="T-1", timestamp=tied_ts, source_message_id="ZZZ-LATE"
    )
    tools.persist_raw_email_label(db, "MSG-SIB-LATE", "1. Needs reply", claim_id=None, evidence="renewal pricing")

    result = _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=tied_ts,
        source_message_id="MMM-AGENT",
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == ["MSG-SIB-EARLY"]
    early = RawEmailDumpRepository(db).find_one({"message_id": "MSG-SIB-EARLY"})
    late = RawEmailDumpRepository(db).find_one({"message_id": "MSG-SIB-LATE"})
    assert early["label_applied"] == "1. Read only"
    assert late["label_applied"] == "1. Needs reply"


def test_agent_reply_ingested_out_of_order_does_not_close_a_later_question(db):
    """An inbound question with a LATER sort_key is already labeled "Needs
    reply". The agent's reply, carrying an EARLIER sort_key, is only
    ingested afterward (e.g. a delayed webhook) -- ingestion call order
    must not matter, only sort_key. The later question stays untouched."""
    settings = _settings()
    _ingest(db, settings, "MSG-LATER-QUESTION", thread_id="T-1", timestamp=datetime(2026, 8, 10, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-LATER-QUESTION", "1. Needs reply", claim_id=None, evidence="renewal pricing")

    result = _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY-OUT-OF-ORDER",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 5, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == []
    question = RawEmailDumpRepository(db).find_one({"message_id": "MSG-LATER-QUESTION"})
    assert question["label_applied"] == "1. Needs reply"
