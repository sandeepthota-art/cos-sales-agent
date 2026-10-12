"""Regression tests for the core engine mechanisms that were previously
verified only once, by hand, live against Atlas, and never captured as a
test: claim atomicity, TTL crash recovery, the 3-attempt circuit breaker,
classification_version eligibility, and the thread close/reopen lifecycle.
Flagged as the single biggest robustness gap in the last review -- this
file closes it.
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


# --- claim atomicity / basic eligibility -------------------------------------------


def test_get_next_unprocessed_raw_email_returns_none_when_nothing_eligible(db):
    assert tools.get_next_unprocessed_raw_email(db) is None


def test_get_next_unprocessed_raw_email_claims_oldest_by_sort_key(db):
    settings = _settings()
    _ingest(db, settings, "MSG-NEW", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))

    claimed = tools.get_next_unprocessed_raw_email(db)

    assert claimed["message_id"] == "MSG-OLD"
    assert claimed["label_claimed_at"] != ""


def test_claimed_email_is_not_returned_again_by_a_second_claim_call(db):
    settings = _settings()
    _ingest(db, settings, "MSG-ONLY")

    first = tools.get_next_unprocessed_raw_email(db)
    second = tools.get_next_unprocessed_raw_email(db)

    assert first["message_id"] == "MSG-ONLY"
    assert second is None  # already claimed, not handed out twice


def test_thread_context_excludes_self_and_is_sorted_oldest_first(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-2", thread_id="T-1", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-3", thread_id="T-1", timestamp=datetime(2026, 8, 3, 9, 0, tzinfo=timezone.utc))

    claimed = tools.get_next_unprocessed_raw_email(db)  # oldest = MSG-1

    assert claimed["message_id"] == "MSG-1"
    assert [d["message_id"] for d in claimed["thread_context"]] == ["MSG-2", "MSG-3"]


# --- batch claim --------------------------------------------------------------------


def test_batch_claim_returns_empty_list_when_nothing_eligible(db):
    assert tools.get_next_unprocessed_raw_email_batch(db) == []


def test_batch_claim_claims_oldest_first_up_to_batch_size(db):
    settings = _settings()
    for i in range(5):
        _ingest(db, settings, f"MSG-{i}", timestamp=datetime(2026, 8, 1 + i, 9, 0, tzinfo=timezone.utc))

    batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=3)

    assert [d["message_id"] for d in batch] == ["MSG-0", "MSG-1", "MSG-2"]


def test_batch_claim_does_not_return_already_claimed_docs(db):
    settings = _settings()
    for i in range(3):
        _ingest(db, settings, f"MSG-{i}", timestamp=datetime(2026, 8, 1 + i, 9, 0, tzinfo=timezone.utc))

    first_batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=2)
    second_batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=2)

    assert [d["message_id"] for d in first_batch] == ["MSG-0", "MSG-1"]
    assert [d["message_id"] for d in second_batch] == ["MSG-2"]  # not MSG-0/MSG-1 again


def test_batch_claim_attaches_thread_context_per_document(db):
    settings = _settings()
    _ingest(db, settings, "MSG-A", thread_id="T-A", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-A2", thread_id="T-A", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-B", thread_id="T-B", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))

    batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=10)
    by_id = {d["message_id"]: d for d in batch}

    assert [d["message_id"] for d in by_id["MSG-A"]["thread_context"]] == ["MSG-A2"]
    assert by_id["MSG-B"]["thread_context"] == []


# --- TTL crash recovery ---------------------------------------------------------------


def test_abandoned_claim_is_not_immediately_reclaimable(db):
    settings = _settings()
    _ingest(db, settings, "MSG-CRASHED")
    tools.get_next_unprocessed_raw_email(db)  # claimed, then "crashes" -- never released

    assert tools.get_next_unprocessed_raw_email(db) is None


def test_abandoned_claim_becomes_reclaimable_after_ttl_expires(db):
    settings = _settings()
    _ingest(db, settings, "MSG-CRASHED")
    tools.get_next_unprocessed_raw_email(db)

    # Simulate the claim having expired: backdate label_claimed_at past the TTL.
    repo = RawEmailDumpRepository(db)
    stale_claim = (datetime.now(timezone.utc) - timedelta(minutes=tools._LABEL_CLAIM_TTL_MINUTES + 1)).isoformat()
    repo.upsert_by_key({"message_id": "MSG-CRASHED"}, {"label_claimed_at": stale_claim})

    recovered = tools.get_next_unprocessed_raw_email(db)

    assert recovered["message_id"] == "MSG-CRASHED"


# --- circuit breaker --------------------------------------------------------------------


def test_mark_failed_increments_attempts_and_releases_claim(db):
    settings = _settings()
    _ingest(db, settings, "MSG-FAIL")
    claimed = tools.get_next_unprocessed_raw_email(db)  # claim it first

    result = tools.mark_raw_email_label_failed(db, "MSG-FAIL", "garbled body", claim_id=claimed["claim_id"])

    assert result["label_attempts"] == 1
    assert result["label_error"] == "garbled body"
    assert result["label_claimed_at"] == ""  # released, so it can be retried
    assert tools.get_next_unprocessed_raw_email(db)["message_id"] == "MSG-FAIL"  # immediately re-claimable


def test_email_stops_being_claimable_after_max_attempts(db):
    settings = _settings()
    _ingest(db, settings, "MSG-STUCK")

    for _ in range(tools._MAX_LABEL_ATTEMPTS):
        claimed = tools.get_next_unprocessed_raw_email(db)
        assert claimed is not None
        tools.mark_raw_email_label_failed(db, "MSG-STUCK", "still garbled", claim_id=claimed["claim_id"])

    assert tools.get_next_unprocessed_raw_email(db) is None  # circuit breaker tripped


def test_persist_label_resets_the_attempts_counter(db):
    settings = _settings()
    _ingest(db, settings, "MSG-RECOVERED")
    first_claim = tools.get_next_unprocessed_raw_email(db)
    tools.mark_raw_email_label_failed(db, "MSG-RECOVERED", "first attempt failed", claim_id=first_claim["claim_id"])

    claimed = tools.get_next_unprocessed_raw_email(db)
    result = tools.persist_raw_email_label(db, claimed["message_id"], "1. Read only", claim_id=claimed["claim_id"])

    assert result["label_attempts"] == 0


# --- classification_version eligibility --------------------------------------------


def test_persist_label_makes_the_email_no_longer_claimable(db):
    settings = _settings()
    _ingest(db, settings, "MSG-DONE")
    claimed = tools.get_next_unprocessed_raw_email(db)
    tools.persist_raw_email_label(db, claimed["message_id"], "1. Read only", claim_id=claimed["claim_id"])

    assert tools.get_next_unprocessed_raw_email(db) is None


# --- thread lifecycle: agent reply closes open "Needs reply*" siblings --------------


def test_agent_reply_closes_open_needs_reply_siblings_to_read_only(db):
    settings = _settings()
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply")

    result = _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == ["MSG-ASK"]
    closed = RawEmailDumpRepository(db).find_one({"message_id": "MSG-ASK"})
    assert closed["label_applied"] == "1. Read only"
    assert closed["gmail_label_synced"] is False
    assert len(closed["label_history"]) == 2  # original decision + the auto-close entry


def test_closed_sibling_classification_version_is_untouched_so_it_is_not_reclaimed(db):
    settings = _settings()
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply")
    version_before = RawEmailDumpRepository(db).find_one({"message_id": "MSG-ASK"})["classification_version"]

    _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    version_after = RawEmailDumpRepository(db).find_one({"message_id": "MSG-ASK"})["classification_version"]
    assert version_after == version_before  # auto-close is a correction, not a re-evaluation
    # MSG-ASK (Aug 1, closed) must never be claimed again, even though it's
    # chronologically older than MSG-AGENT-REPLY (Aug 2) -- the only thing
    # legitimately claimable here is the agent's own reply itself, since
    # THAT document is new and unclassified (classification_version=0).
    claimed = tools.get_next_unprocessed_raw_email(db)
    assert claimed["message_id"] == "MSG-AGENT-REPLY"


def test_empty_agent_email_takes_reopen_path_never_auto_closes(db):
    """Documents the behavior app/server.py's startup warning flags: with
    AGENT_EMAIL unset, bool("") is False, so is_from_agent is always False
    -- even a message from the exact address that WOULD be the agent if
    configured goes through the reopen path, never the auto-close path."""
    settings = _settings(agent_email="")
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply")

    result = _ingest(
        db,
        settings,
        "MSG-WOULD-BE-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    assert result["closed_in_thread"] == []  # never auto-closed, no AGENT_EMAIL to match against
    assert result["reopened_in_thread"] == ["MSG-ASK"]  # took the reopen path instead
    sibling = RawEmailDumpRepository(db).find_one({"message_id": "MSG-ASK"})
    assert sibling["label_applied"] == "1. Needs reply"  # untouched, not force-closed to Read only
    assert sibling["classification_version"] == 0  # reset, eligible for re-evaluation


def test_inbound_reply_reopens_already_classified_siblings_for_reevaluation(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-OLD", "1. Read only")

    result = _ingest(
        db, settings, "MSG-NEW-QUESTION", thread_id="T-1", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    )

    assert result["reopened_in_thread"] == ["MSG-OLD"]
    reopened = RawEmailDumpRepository(db).find_one({"message_id": "MSG-OLD"})
    assert reopened["classification_version"] == 0
    assert reopened["label_applied"] == "1. Read only"  # untouched -- Claude re-decides on next claim


def test_reopened_sibling_becomes_claimable_again(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-OLD", "1. Read only")
    assert tools.get_next_unprocessed_raw_email(db) is None  # confirm it's NOT claimable yet

    _ingest(
        db, settings, "MSG-NEW-QUESTION", thread_id="T-1", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)
    )

    claimed = tools.get_next_unprocessed_raw_email(db)
    assert claimed["message_id"] == "MSG-OLD"  # oldest, and newly eligible again


def test_close_and_reopen_never_touch_emails_outside_the_thread(db):
    settings = _settings()
    _ingest(
        db,
        settings,
        "MSG-OTHER-THREAD",
        thread_id="T-UNRELATED",
        timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    )
    tools.persist_raw_email_label(db, "MSG-OTHER-THREAD", "1. Needs reply")
    _ingest(db, settings, "MSG-ASK", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-ASK", "1. Needs reply")

    _ingest(
        db,
        settings,
        "MSG-AGENT-REPLY",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc),
        **{"from": {"name": "Agent", "email": "agent@ourcompany.example"}},
    )

    untouched = RawEmailDumpRepository(db).find_one({"message_id": "MSG-OTHER-THREAD"})
    assert untouched["label_applied"] == "1. Needs reply"  # never closed by a different thread's reply
    assert len(untouched["label_history"]) == 1


def test_no_thread_id_skips_close_and_reopen_logic(db):
    settings = _settings()
    result = _ingest(db, settings, "MSG-NO-THREAD", thread_id=None)

    assert result["closed_in_thread"] == []
    assert result["reopened_in_thread"] == []


# --- ingestion correctness -----------------------------------------------------------


def test_ingest_is_idempotent_and_never_resets_existing_classification_progress(db):
    settings = _settings()
    _ingest(db, settings, "MSG-DUP")
    claimed = tools.get_next_unprocessed_raw_email(db)
    tools.persist_raw_email_label(db, claimed["message_id"], "1. Delete", claim_id=claimed["claim_id"])

    # Re-ingesting the SAME Gmail message (e.g. a sliding-window overlap re-read).
    result = _ingest(db, settings, "MSG-DUP")

    assert result["label_applied"] == "1. Delete"  # untouched by the re-ingest
    assert result["classification_version"] == tools._CURRENT_LABEL_CLASSIFICATION_VERSION


def test_sort_key_normalizes_differing_utc_offsets_to_correct_chronological_order(db):
    settings = _settings()
    # 09:00 IST (UTC+5:30) on Aug 2 is 03:30 UTC Aug 2 -- earlier than 23:00 UTC Aug 1.
    ist = timezone(timedelta(hours=5, minutes=30))
    _ingest(db, settings, "MSG-LATE-UTC-STRING", timestamp=datetime(2026, 8, 1, 23, 0, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-EARLY-IST", timestamp=datetime(2026, 8, 2, 9, 0, tzinfo=ist))

    claimed = tools.get_next_unprocessed_raw_email(db)

    assert claimed["message_id"] == "MSG-LATE-UTC-STRING"  # chronologically first despite the later calendar date
