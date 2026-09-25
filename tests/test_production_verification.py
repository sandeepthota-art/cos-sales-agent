# tests/test_production_verification.py
"""Phase 20: the reusable production-invariant verification layer. All synthetic
mongomock fixtures -- no hardcoded current-Atlas ids, names, or counts anywhere,
per Section 15/16's explicit requirement that this hold for future data too.
"""
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
    ProjectRepository,
    ThreadRepository,
)
from app.production_verification import (
    verify_calendar_safety,
    verify_cross_collection_referential_integrity,
    verify_knowledge_safety,
    verify_organization_invariants,
    verify_person_lifecycle,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _person(**overrides):
    person_id = overrides.get("id", "PER-1")
    doc = {
        "id": person_id, "name": "Someone", "email": f"{person_id.lower()}@example.com", "aliases": [],
        "org": None, "org_id": None, "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": [], "note_link": None, "review_flag": False, "source": "gmail",
    }
    doc.update(overrides)
    return doc


# --- verify_person_lifecycle ---------------------------------------------------------------


def test_verify_person_lifecycle_counts_missing_status_as_active(db):
    p = _person(id="PER-1")
    p.pop("status", None)
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, p)

    report = verify_person_lifecycle(db)

    assert report["total_persons"] == 1
    assert report["active_persons"] == 1
    assert report["merged_persons"] == 0
    assert report["active_resolving_to_self"] == 1


def test_verify_person_lifecycle_clean_merge_reports_zero_defects(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))

    report = verify_person_lifecycle(db)

    assert report["merged_persons"] == 1
    assert report["merged_resolving_to_active"] == 1
    assert report["merged_with_missing_merged_into"] == []
    assert report["merged_with_invalid_merged_into_target"] == []
    assert report["self_references"] == []
    assert report["circular_chains"] == []
    assert report["depth_violations"] == []


def test_verify_person_lifecycle_flags_invalid_status_value(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="deleted"))

    report = verify_person_lifecycle(db)

    assert report["invalid_status_values"] == [{"person_id": "PER-1", "status": "deleted"}]


def test_verify_person_lifecycle_flags_missing_merged_into(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into=None))

    report = verify_person_lifecycle(db)

    assert report["merged_with_missing_merged_into"] == ["PER-1"]


def test_verify_person_lifecycle_flags_invalid_merged_into_target(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-999"))

    report = verify_person_lifecycle(db)

    assert report["merged_with_invalid_merged_into_target"] == ["PER-1"]


def test_verify_person_lifecycle_flags_self_reference(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-1"))

    report = verify_person_lifecycle(db)

    assert report["self_references"] == ["PER-1"]


def test_verify_person_lifecycle_flags_circular_chain(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", status="merged", merged_into="PER-1"))

    report = verify_person_lifecycle(db)

    assert set(report["circular_chains"]) == {"PER-1", "PER-2"}


# --- verify_organization_invariants ---------------------------------------------------------


def test_verify_organization_invariants_clean_state(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "Acme", "domain": "acme.example", "aliases": [], "source": "gmail"})
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", org_id="ORG-1"))

    report = verify_organization_invariants(db)

    assert report["total_organizations"] == 1
    assert report["people_with_org_id"] == 1
    assert report["dangling_org_id_person_ids"] == []
    assert report["duplicate_domains"] == {}


def test_verify_organization_invariants_flags_dangling_org_id(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", org_id="ORG-MISSING"))

    report = verify_organization_invariants(db)

    assert report["dangling_org_id_person_ids"] == ["PER-1"]


def test_verify_organization_invariants_flags_duplicate_domain(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "Acme A", "domain": "acme.example", "aliases": [], "source": "gmail"})
    OrganizationRepository(db).upsert_by_key({"id": "ORG-2"}, {"id": "ORG-2", "name": "Acme B", "domain": "acme.example", "aliases": [], "source": "gmail"})

    report = verify_organization_invariants(db)

    assert report["duplicate_domains"] == {"acme.example": ["ORG-1", "ORG-2"]}


# --- verify_cross_collection_referential_integrity -------------------------------------------


def test_referential_integrity_clean_state(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "person_ids": ["PER-1"], "org_ids": []})

    report = verify_cross_collection_referential_integrity(db)

    assert report["threads"]["dangling_references"] == []
    assert report["threads"]["merged_person_references"] == []


def test_referential_integrity_flags_dangling_person_reference(db):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
         "person_id": "PER-MISSING", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )

    report = verify_cross_collection_referential_integrity(db)

    assert len(report["commitments"]["dangling_references"]) == 1
    assert report["commitments"]["dangling_references"][0]["value"] == "PER-MISSING"


def test_referential_integrity_flags_reference_to_merged_person(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
         "person_id": "PER-1", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )

    report = verify_cross_collection_referential_integrity(db)

    assert len(report["commitments"]["merged_person_references"]) == 1
    assert report["commitments"]["merged_person_references"][0]["value"] == "PER-1"
    assert report["commitments"]["dangling_references"] == []  # PER-1 exists -- just merged, not dangling


def test_referential_integrity_handles_none_list_field_safely(db):
    # Regression guard for the exact Phase 18 bug: an explicit None (not missing,
    # not []) in a list-valued field must never crash this audit.
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "attendees": [], "person_ids": None, "org_id": None})
    ProjectRepository(db).upsert_by_key({"id": "PRJ-1"}, {"id": "PRJ-1", "project": "x", "person_ids": None, "org_id": None})
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "person_ids": None, "org_ids": None})

    report = verify_cross_collection_referential_integrity(db)  # must not raise

    assert report["meetings"]["dangling_references"] == []
    assert report["projects"]["dangling_references"] == []
    assert report["threads"]["dangling_references"] == []


def test_referential_integrity_flags_dangling_meeting_id_on_calendar_action(db):
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "person_id": None, "org_id": None,
         "meeting_id": "MTG-MISSING", "event": {"attendees": []}},
    )

    report = verify_cross_collection_referential_integrity(db)

    assert len(report["calendar_actions"]["dangling_references"]) == 1
    assert report["calendar_actions"]["dangling_references"][0]["field"] == "meeting_id"


# --- verify_calendar_safety ------------------------------------------------------------------


def test_calendar_safety_clean_state(db):
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "person_id": None, "org_id": None, "meeting_id": None, "event": {"attendees": []}},
    )

    report = verify_calendar_safety(db)

    assert report["safe"] is True
    assert report["external_attendee_violations"] == []


def test_calendar_safety_flags_external_attendees(db):
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "person_id": None, "org_id": None, "meeting_id": None,
         "event": {"attendees": ["external@customer.com"]}},
    )

    report = verify_calendar_safety(db)

    assert report["safe"] is False
    assert report["external_attendee_violations"] == [{"thread_id": "t1", "meeting_fingerprint": "fp1"}]


# --- verify_knowledge_safety -----------------------------------------------------------------


def test_knowledge_safety_categorizes_all_four_buckets(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    KnowledgeRepository(db).upsert_by_key({"knowledge_id": "K-1"}, {"knowledge_id": "K-1", "thread_id": "t1", "person_id": "PER-2", "org_id": None, "subject_key": "x"})
    KnowledgeRepository(db).upsert_by_key({"knowledge_id": "K-2"}, {"knowledge_id": "K-2", "thread_id": "t1", "person_id": None, "org_id": "ORG-1", "subject_key": "y"})
    KnowledgeRepository(db).upsert_by_key({"knowledge_id": "K-3"}, {"knowledge_id": "K-3", "thread_id": "t1", "person_id": None, "org_id": None, "subject_key": "t1"})
    KnowledgeRepository(db).upsert_by_key({"knowledge_id": "K-4"}, {"knowledge_id": "K-4", "thread_id": "t1", "person_id": "PER-1", "org_id": None, "subject_key": "z"})

    report = verify_knowledge_safety(db)

    assert report["total_knowledge_items"] == 4
    assert report["explicit_person_links"] == 2
    assert report["explicit_person_links_pointing_at_merged_person"] == ["K-4"]
    assert report["org_only_links"] == 1
    assert report["thread_level_or_unresolved"] == 1
