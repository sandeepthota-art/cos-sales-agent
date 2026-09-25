# tests/test_duplicate_impact_analysis.py
"""All tests use isolated mongomock fixtures only -- this is a READ-ONLY analysis
phase, never run against production Atlas here."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
)
from app.duplicate_impact_analysis import (
    build_high_confidence_report,
    categorize_knowledge_items,
    check_calendar_safety,
    classify_all_high_confidence_candidates,
    compute_downstream_impact,
)

_NOW = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _person(**overrides):
    doc = {
        "id": "PER-391", "name": "Ashok Ganapam", "email": "ashok@databeat.io", "aliases": [],
        "org": "DataBeat", "org_id": "ORG-001", "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": ["thread_anchor"], "note_link": None,
        "review_flag": False, "source": "gmail",
    }
    doc.update(overrides)
    return doc


def _seed_org(db, org_id="ORG-001", name="DataBeat", domain="databeat.io"):
    OrganizationRepository(db).upsert_by_key(
        {"id": org_id}, {"id": org_id, "name": name, "domain": domain, "aliases": [], "source": "gmail"}
    )


# --- Deterministic HIGH classification ------------------------------------------------


def test_deterministic_high_duplicate_is_safe_to_review(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"}, _person(id="PER-390", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_anchor"])
    )

    results = classify_all_high_confidence_candidates(db)

    match = next(r for r in results if r["duplicate_person_id"] == "PER-390")
    assert match["classification"] == "SAFE_TO_REVIEW"
    assert match["mergeable"] is True
    assert match["blocking_reason"] is None
    assert match["canonical_person_id"] == "PER-391"


# --- Ambiguous classification ----------------------------------------------------------


def test_ambiguous_duplicate_is_blocked_ambiguous(db):
    # Three anchors sharing the same name+org -- PER-003 is proposed as a duplicate of
    # BOTH PER-001 and PER-002 (the anchor-anchor detection axis), a genuine "more than
    # one plausible canonical target" case.
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(id="PER-001", email="lowell1@outfront.com", name="Lowell Simpson"))
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person(id="PER-002", email="lowell2@outfront.com", name="Lowell Simpson"))
    PersonRepository(db).upsert_by_key({"id": "PER-003"}, _person(id="PER-003", email="lowell3@outfront.com", name="Lowell Simpson"))

    results = classify_all_high_confidence_candidates(db)

    per_003_rows = [r for r in results if r["duplicate_person_id"] == "PER-003"]
    assert len(per_003_rows) >= 1
    assert all(r["classification"] == "BLOCKED_AMBIGUOUS" for r in per_003_rows)
    assert all(r["mergeable"] is False for r in per_003_rows)


# --- Conflicting email classification ---------------------------------------------------


def test_two_anchors_with_different_real_emails_is_blocked_data_conflict(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-441"}, _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", org_id="ORG-002")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-443"}, _person(id="PER-443", name="Lowell Simpson", email="simpsonlowell@gmail.com", org="Outfront Media", org_id="ORG-002")
    )

    results = classify_all_high_confidence_candidates(db)

    match = next(r for r in results if {r["duplicate_person_id"], r["canonical_person_id"]} == {"PER-441", "PER-443"})
    assert match["classification"] == "BLOCKED_DATA_CONFLICT"
    assert match["mergeable"] is False
    assert "distinct real email" in match["blocking_reason"]


# --- Malformed multi-email blocking ------------------------------------------------------


def test_malformed_combined_email_is_blocked_data_conflict_not_approved(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-441"}, _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", org_id="ORG-002")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-444"},
        _person(
            id="PER-444", name="Lowell Simpson",
            email="simpsonlowell@gmail.com; lowell@outfront.com", org="Outfront Media", org_id=None,
        ),
    )

    results = classify_all_high_confidence_candidates(db)

    match = next(r for r in results if r["duplicate_person_id"] == "PER-444")
    assert match["classification"] == "BLOCKED_DATA_CONFLICT"
    assert match["mergeable"] is False
    assert "malformed" in match["blocking_reason"]


# --- PER-608/PER-513-style short nickname ------------------------------------------------


def test_short_nickname_match_is_needs_manual_review_not_safe(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key(
        {"id": "PER-513"}, _person(id="PER-513", name="Ash Allen", email="ash.allen@databeat.io", open_threads=["thread_shared"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-608"}, _person(id="PER-608", name="Ash", email=None, org_id=None, open_threads=["thread_shared"])
    )

    results = classify_all_high_confidence_candidates(db)

    match = next(r for r in results if r["duplicate_person_id"] == "PER-608")
    assert match["classification"] == "NEEDS_MANUAL_REVIEW"
    assert match["mergeable"] is False
    assert "nickname" in match["blocking_reason"]
    assert match["canonical_person_id"] == "PER-513"  # correctly identified the target, just not auto-approved


def test_full_first_name_match_is_not_penalized_as_a_short_nickname(db):
    # "Ashok" (5 letters) is a complete real given name, not a stub -- must NOT be
    # treated the same as "Ash" (3 letters). This proves the rule is principled (name
    # length), not a hardcoded special case for PER-608 alone.
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-386"}, _person(id="PER-386", name="Ashok", email=None, org_id=None, open_threads=["thread_anchor"])
    )

    results = classify_all_high_confidence_candidates(db)

    match = next(r for r in results if r["duplicate_person_id"] == "PER-386")
    assert match["classification"] == "SAFE_TO_REVIEW"


# --- Downstream impact calculation -------------------------------------------------------


def test_downstream_impact_reports_correct_per_collection_counts(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["thread_anchor"])
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m1", "made_on": _NOW, "status": "open", "thread_id": "thread_anchor",
            "person_id": "PER-391", "org_id": "ORG-001",
        },
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-002"},
        {
            "id": "COM-002", "what": "y", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m2", "made_on": _NOW, "status": "open", "thread_id": "thread_anchor",
            "person_id": "PER-390", "org_id": None,
        },
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-003"},
        {
            "id": "COM-003", "what": "z", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m3", "made_on": _NOW, "status": "open", "thread_id": "thread_anchor",
            "person_id": None, "org_id": None,
        },
    )

    impact = compute_downstream_impact(db, "PER-390", "PER-391")

    commitments = impact["commitments"]
    assert commitments["already_pointing_to_canonical"] == 1
    assert commitments["pointing_to_duplicate"] == 1
    assert commitments["no_person_id_but_matching_duplicate_threads"] == 1
    assert commitments["would_require_deterministic_repointing"] == 1
    assert commitments["would_require_no_change"] == 1

    before = CommitmentRepository(db).find_many({})
    compute_downstream_impact(db, "PER-390", "PER-391")
    after = CommitmentRepository(db).find_many({})
    assert before == after  # strictly read-only


# --- Calendar attendee safety -------------------------------------------------------------


def test_calendar_safety_flags_external_attendee_conflict(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": ["intruder@example.com"]},
            "actor_type": "authenticated_user", "reason": None, "person_id": "PER-391", "org_id": "ORG-001", "meeting_id": None,
        },
    )

    result = check_calendar_safety(db, "PER-390", "PER-391")

    assert result["safe"] is False
    assert result["external_attendee_conflicts"] == 1


def test_calendar_safety_passes_when_no_attendees_present(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": []},
            "actor_type": "authenticated_user", "reason": None, "person_id": "PER-391", "org_id": "ORG-001", "meeting_id": None,
        },
    )

    result = check_calendar_safety(db, "PER-390", "PER-391")

    assert result["safe"] is True
    assert result["external_attendee_conflicts"] == 0


# --- Knowledge thread-level safety ---------------------------------------------------------


def test_knowledge_categorization_never_treats_thread_level_facts_as_person_knowledge(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["thread_anchor"])
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k_person"},
        {
            "knowledge_id": "k_person", "thread_id": "thread_anchor", "subject_key": "ashok_ganapam",
            "predicate": "prefers", "fact_key": "mornings", "current_value": "mornings", "history": [],
            "source_emails": ["m1"], "basis": "stated", "first_seen_at": _NOW, "last_confirmed_at": _NOW,
            "confidence": 0.8, "status": "active", "person_id": "PER-390", "org_id": None,
        },
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k_org"},
        {
            "knowledge_id": "k_org", "thread_id": "thread_anchor", "subject_key": "databeat",
            "predicate": "has", "fact_key": "employee_count", "current_value": "500", "history": [],
            "source_emails": ["m1"], "basis": "stated", "first_seen_at": _NOW, "last_confirmed_at": _NOW,
            "confidence": 0.8, "status": "active", "person_id": None, "org_id": "ORG-001",
        },
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k_thread"},
        {
            "knowledge_id": "k_thread", "thread_id": "thread_anchor", "subject_key": "thread_anchor",
            "predicate": "requires", "fact_key": "seat_count", "current_value": "25 seats", "history": [],
            "source_emails": ["m1"], "basis": "stated", "first_seen_at": _NOW, "last_confirmed_at": _NOW,
            "confidence": 0.8, "status": "active", "person_id": None, "org_id": None,
        },
    )

    result = categorize_knowledge_items(db, "PER-390", "PER-391")

    assert result["canonical_person_knowledge"] == 1
    assert result["canonical_org_knowledge"] == 1
    assert result["thread_level_knowledge"] == 1


# --- Idempotent read-only analysis ----------------------------------------------------------


def test_full_report_is_idempotent_and_strictly_read_only(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["thread_anchor"])
    )

    people_before = PersonRepository(db).find_many({})
    orgs_before = OrganizationRepository(db).find_many({})

    first = build_high_confidence_report(db)
    second = build_high_confidence_report(db)

    people_after = PersonRepository(db).find_many({})
    orgs_after = OrganizationRepository(db).find_many({})

    assert first == second
    assert people_before == people_after
    assert orgs_before == orgs_after
