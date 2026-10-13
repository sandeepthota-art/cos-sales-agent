"""app.context.persist_context_delta / mark_context_failed / get_stuck_context
-- claim safety and apply correctness, synthetic data only."""

from datetime import datetime, timezone

import mongomock
import pytest

from app import context, tools
from app.config import Settings
from app.context_config import MAX_CONTEXT_ATTEMPTS
from app.db import initialize_indexes
from app.models import parse_email

BODY = "Can we discuss renewal pricing for next quarter? Jane Doe is the account manager at CustomerCo."


@pytest.fixture
def db():
    """initialize_indexes is required here (unlike plain label tests) --
    ObservationRepository.append's idempotency depends on the real unique
    index existing; mongomock does not enforce uniqueness on an index that
    was never created."""
    database = mongomock.MongoClient().db
    initialize_indexes(database)
    return database


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
        "body": BODY,
        "timestamp": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _ingest(db, settings, message_id: str, **overrides) -> dict:
    return tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id, **overrides)), settings)


def _valid_observation(**overrides):
    obs = {
        "entity_type": "person",
        "entity_key": "jane@customerco.example",
        "attribute": "department",
        "value": "Sales",
        "evidence": "Can we discuss renewal pricing for next quarter?",
        "source_message_id": "MSG-1",
    }
    obs.update(overrides)
    return obs


def test_claim_with_unknown_claim_id_is_claim_lost_and_writes_nothing(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    context.claim_next_thread_for_context(db)
    delta = {"observations": [_valid_observation()]}

    result = context.persist_context_delta(db, "never-issued-claim-id", "T-1", delta)

    assert result == {"ok": False, "reason": "claim_lost"}
    assert db.observations.count_documents({}) == 0


def test_stale_claim_lost_after_reclaim(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    claimed = context.claim_next_thread_for_context(db)
    db.raw_emails_dump.update_many({"thread_id": "T-1"}, {"$set": {"context_claimed_at": "2000-01-01T00:00:00+00:00"}})
    reclaimed = context.claim_next_thread_for_context(db)
    assert reclaimed["claim_id"] != claimed["claim_id"]

    result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], {"observations": [_valid_observation()]})

    assert result == {"ok": False, "reason": "claim_lost"}
    assert db.observations.count_documents({}) == 0


def test_lease_expiry_then_reclaim_works(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    context.claim_next_thread_for_context(db)
    db.raw_emails_dump.update_many({"thread_id": "T-1"}, {"$set": {"context_claimed_at": "2000-01-01T00:00:00+00:00"}})
    reclaimed = context.claim_next_thread_for_context(db)

    result = context.persist_context_delta(
        db, reclaimed["claim_id"], reclaimed["thread_id"], {"observations": [_valid_observation()]}
    )

    assert result["ok"] is True


def test_retry_cap_terminal_and_visible_in_get_stuck_context(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    for _ in range(MAX_CONTEXT_ATTEMPTS):
        claimed = context.claim_next_thread_for_context(db)
        assert claimed["empty"] is False
        context.mark_context_failed(db, claimed["claim_id"], claimed["thread_id"], "boom")

    assert context.claim_next_thread_for_context(db) == {"empty": True}
    stuck = context.get_stuck_context(db)
    assert [d["message_id"] for d in stuck] == ["MSG-1"]


def test_idempotent_repersist_of_same_delta_creates_no_duplicates(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    delta = {"observations": [_valid_observation()]}
    claimed1 = context.claim_next_thread_for_context(db)
    context.persist_context_delta(db, claimed1["claim_id"], claimed1["thread_id"], delta)
    assert db.observations.count_documents({}) == 1

    db.raw_emails_dump.update_many(
        {"message_id": "MSG-1"}, {"$set": {"context_version": 0, "context_claim_id": None, "context_claimed_at": ""}}
    )
    claimed2 = context.claim_next_thread_for_context(db)
    result = context.persist_context_delta(db, claimed2["claim_id"], claimed2["thread_id"], delta)

    assert result["ok"] is True
    assert db.observations.count_documents({}) == 1


def test_crash_after_apply_before_release_then_reprocess_yields_identical_state(db):
    """Simulates a crash between steps (a)-(c) (applied) and step (d) (claim
    release) -- the lease expires, the thread is reclaimed with a NEW
    claim_id, and reprocessing the SAME delta must leave the derived state
    (profile, observation count, thread status/summary) unchanged. The
    threads.context_version reprocess counter and threads_history are
    expected to advance (each persist call is a real reprocessing event,
    not a duplicate write) -- those are excluded from this comparison."""
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    delta = {"observations": [_valid_observation()], "thread": {"status": "resolved", "summary": "Pricing agreed."}}

    claimed = context.claim_next_thread_for_context(db)
    context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)
    baseline_person = db.people.find_one({"_id": "jane@customerco.example"}, {"_id": 0})
    baseline_thread = db.threads.find_one({"_id": "T-1"})
    baseline_obs_count = db.observations.count_documents({})

    db.raw_emails_dump.update_many(
        {"message_id": "MSG-1"}, {"$set": {"context_version": 0, "context_claimed_at": "2000-01-01T00:00:00+00:00"}}
    )
    reclaimed = context.claim_next_thread_for_context(db)
    result = context.persist_context_delta(db, reclaimed["claim_id"], reclaimed["thread_id"], delta)

    assert result["ok"] is True
    assert db.people.find_one({"_id": "jane@customerco.example"}, {"_id": 0}) == baseline_person
    reprocessed_thread = db.threads.find_one({"_id": "T-1"})
    assert reprocessed_thread["status"] == baseline_thread["status"]
    assert reprocessed_thread["summary"] == baseline_thread["summary"]
    assert db.observations.count_documents({}) == baseline_obs_count


def test_concurrent_persist_race_second_caller_gets_claim_lost(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    claimed = context.claim_next_thread_for_context(db)
    delta = {"observations": [_valid_observation()]}

    first = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)
    second = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)

    assert first["ok"] is True
    assert second == {"ok": False, "reason": "claim_lost"}


def test_successful_persist_strips_control_chars_from_value(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1")
    claimed = context.claim_next_thread_for_context(db)
    delta = {"observations": [_valid_observation(value="Sales\x07Team")]}

    result = context.persist_context_delta(db, claimed["claim_id"], claimed["thread_id"], delta)

    assert result["ok"] is True
    obs = db.observations.find_one({"entity_key": "jane@customerco.example"})
    assert obs["value"] == "SalesTeam"
    person = db.people.find_one({"_id": "jane@customerco.example"})
    assert person["profile"]["department"] == "SalesTeam"
