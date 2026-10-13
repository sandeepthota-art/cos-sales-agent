"""Tests for app.entities.profile_eligibility (Milestone 1: reliable profile
generation). check_profile_eligibility is a deterministic, zero-LLM-call gate
-- these tests construct exact evidence scenarios directly against the
repositories, rather than running the full pipeline, to pin the eligibility
rule precisely.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    KnowledgeRepository,
    MeetingRepository,
    PersonContextSnapshotRepository,
    PersonRepository,
)
from app.entities.profile_eligibility import check_profile_eligibility, mark_profile_generation_failed
from app.mcp import tools


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _make_person(db, person_id="PER-001", **overrides):
    person = {
        "id": person_id, "name": "Khyati Mehra", "email": "mehrak@google.com",
        "aliases": [], "org": "Google", "org_id": "ORG-001", "status": "active",
        "profile_summary": None, "recent_context": None, "key_topics": [],
        "profile_updated_at": None,
    }
    person.update(overrides)
    PersonRepository(db).upsert_by_key({"id": person_id}, person)
    return person


def _add_snapshot(db, person_id, source_email_id, thread_id="THR-001"):
    PersonContextSnapshotRepository(db).upsert_by_key(
        {"person_id": person_id, "source_email_id": source_email_id},
        {
            "id": f"PCS-{source_email_id}", "person_id": person_id, "thread_id": thread_id,
            "source_email_id": source_email_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": [],
        },
    )


def test_ineligible_with_single_bare_interaction(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is False
    assert result["evidence"]["email_interaction_count"] == 1
    assert "insufficient" in result["reason"].lower()


def test_eligible_via_knowledge_fact(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "KNOW-001"},
        {
            "knowledge_id": "KNOW-001", "thread_id": "THR-001", "subject_key": "khyati",
            "predicate": "role", "fact_key": "role", "current_value": "Community Manager",
            "person_id": "PER-001", "history": [], "source_emails": ["EML-001"],
            "basis": "stated", "first_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_confirmed_at": datetime.now(timezone.utc).isoformat(), "confidence": 0.9,
        },
    )

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is True
    assert result["evidence"]["knowledge_fact_count"] == 1
    assert result["evidence"]["source_email_ids"] == ["EML-001"]


def test_eligible_via_commitment(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    CommitmentRepository(db).upsert_by_key(
        {"id": "CMT-001"},
        {
            "id": "CMT-001", "what": "Send the deck", "class": "theirs", "person_id": "PER-001",
            "source_record": "EML-001", "made_on": datetime.now(timezone.utc).isoformat(), "status": "open",
        },
    )

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is True
    assert result["evidence"]["commitment_count"] == 1


def test_eligible_via_meeting(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"},
        {"id": "MTG-001", "attendees": ["Khyati Mehra"], "person_ids": ["PER-001"]},
    )

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is True
    assert result["evidence"]["meeting_count"] == 1


def test_eligible_via_multiple_threads_with_no_other_signal(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001", thread_id="THR-001")
    _add_snapshot(db, "PER-001", "EML-002", thread_id="THR-002")

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is True
    assert result["evidence"]["distinct_threads"] == 2
    assert result["evidence"]["knowledge_fact_count"] == 0
    assert result["evidence"]["commitment_count"] == 0
    assert result["evidence"]["meeting_count"] == 0


def test_ineligible_result_never_clears_existing_profile(db):
    _make_person(db, profile_summary="Existing profile text.", recent_context="Existing context.")
    _add_snapshot(db, "PER-001", "EML-001")

    result = check_profile_eligibility(db, "PER-001")

    assert result["eligible"] is False
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Existing profile text."
    assert stored["recent_context"] == "Existing context."


def test_reason_distinguishes_existing_profile_from_never_had_one(db):
    _make_person(db, person_id="PER-001", profile_summary="Already has a profile.")
    _add_snapshot(db, "PER-001", "EML-001")
    _make_person(db, person_id="PER-002", email="other@example.com", profile_summary=None)
    _add_snapshot(db, "PER-002", "EML-002")

    with_profile = check_profile_eligibility(db, "PER-001")
    without_profile = check_profile_eligibility(db, "PER-002")

    assert "existing profile preserved" in with_profile["reason"].lower()
    assert "no profile yet" in without_profile["reason"].lower()


def test_check_stamps_pending_generation_status(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "KNOW-001"},
        {
            "knowledge_id": "KNOW-001", "thread_id": "THR-001", "subject_key": "khyati",
            "predicate": "role", "fact_key": "role", "current_value": "Community Manager",
            "person_id": "PER-001", "history": [], "source_emails": ["EML-001"],
            "basis": "stated", "first_seen_at": datetime.now(timezone.utc).isoformat(),
            "last_confirmed_at": datetime.now(timezone.utc).isoformat(), "confidence": 0.9,
        },
    )

    check_profile_eligibility(db, "PER-001")

    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_status"] == "pending_generation"
    assert stored["profile_checked_at"] is not None


def test_check_stamps_insufficient_evidence_status(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    check_profile_eligibility(db, "PER-001")

    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_status"] == "insufficient_evidence"


def test_repeated_checks_are_idempotent(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    first = check_profile_eligibility(db, "PER-001")
    second = check_profile_eligibility(db, "PER-001")

    assert first["eligible"] == second["eligible"]
    assert first["evidence"] == second["evidence"]


def test_duplicate_evidence_is_not_double_counted(db):
    _make_person(db)
    # Same source_email_id upserted twice -- must not inflate the count.
    _add_snapshot(db, "PER-001", "EML-001")
    _add_snapshot(db, "PER-001", "EML-001")

    result = check_profile_eligibility(db, "PER-001")

    assert result["evidence"]["email_interaction_count"] == 1


def test_check_profile_eligibility_raises_for_unknown_person(db):
    with pytest.raises(ValueError):
        check_profile_eligibility(db, "PER-999")


def test_persist_person_profile_sets_generated_status_and_source_email_ids(db):
    _make_person(db)

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Khyati leads the Google Developer Ecosystem community.",
        source_email_ids=["EML-001", "EML-002"],
    )

    assert result["profile_status"] == "generated"
    assert result["profile_source_email_ids"] == ["EML-001", "EML-002"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_status"] == "generated"


def test_persist_person_profile_without_source_email_ids_is_backward_compatible(db):
    _make_person(db)

    result = tools.persist_person_profile(db, "PER-001", profile_summary="A profile with no evidence list.")

    assert result["profile_summary"] == "A profile with no evidence list."
    assert result["profile_source_email_ids"] == []
    assert result["profile_status"] == "generated"


def test_mark_profile_generation_failed_records_retryable_status(db):
    _make_person(db)

    result = mark_profile_generation_failed(db, "PER-001", "Conflicting role evidence across two emails.")

    assert result["profile_status"] == "generation_failed"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_status"] == "generation_failed"
    assert stored["profile_status_reason"] == "Conflicting role evidence across two emails."
    assert stored["profile_summary"] is None


def test_mark_profile_generation_failed_raises_for_unknown_person(db):
    with pytest.raises(ValueError):
        mark_profile_generation_failed(db, "PER-999", "irrelevant")


def test_tools_check_profile_eligibility_wrapper(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = tools.check_profile_eligibility(db, "PER-001")

    assert result["person_id"] == "PER-001"
    assert "eligible" in result
