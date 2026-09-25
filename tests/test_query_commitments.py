# tests/test_query_commitments.py
"""Phase 24: Commitment + Follow-Up Intelligence. Synthetic mongomock fixtures only.
Ownership maps directly onto Commitment.commitment_class -- no new state machine."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import CommitmentRepository, FollowUpRepository, MeetingRepository, PersonRepository
from app.query import commitments as cq
from app.query.schemas import OwnershipDirection, QueryFilters, QueryIntentType, QueryRequest, QueryResultStatus
from app.query.service import execute_query

_REF = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)


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
        "status": "active", "merged_into": None,
    }
    doc.update(overrides)
    return doc


def _commitment(cid, **overrides):
    doc = {
        "id": cid, "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
        "person_id": None, "org_id": None, "source_record": "e:1", "made_on": "2026-03-01T00:00:00Z",
        "committed_date": None, "status": "open", "importance": None, "project_id": None,
    }
    doc.update(overrides)
    return doc


def _follow_up(fid, **overrides):
    doc = {
        "id": fid, "commitment_id": None, "thread_id": "t1", "person_id": None, "org_id": None,
        "escalation_level": 1, "surfaced": False, "status": "active",
        "audience": None, "follow_up_earliest_at": None, "follow_up_latest_at": None,
    }
    doc.update(overrides)
    return doc


# --- Ownership boundaries ------------------------------------------------------------------


def test_get_user_commitments_only_class_mine(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-MINE"}, _commitment("COM-MINE", **{"class": "mine"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-THEIRS"}, _commitment("COM-THEIRS", **{"class": "owed_to_me"}))

    result = cq.get_user_commitments(db)

    assert [c["id"] for c in result] == ["COM-MINE"]


def test_get_commitments_owed_to_user_only_class_owed_to_me(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-MINE"}, _commitment("COM-MINE", **{"class": "mine"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-OWED"}, _commitment("COM-OWED", **{"class": "owed_to_me"}))

    result = cq.get_commitments_owed_to_user(db)

    assert [c["id"] for c in result] == ["COM-OWED"]


def test_ownership_excludes_theirs_and_recap_from_both_directions(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-THEIRS"}, _commitment("COM-THEIRS", **{"class": "theirs"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-RECAP"}, _commitment("COM-RECAP", **{"class": "recap"}))

    user_owes = cq.get_user_commitments(db)
    owed_to_user = cq.get_commitments_owed_to_user(db)

    assert user_owes == []
    assert owed_to_user == []


def test_execute_query_who_am_i_waiting_on_resolves_ownership_deterministically(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-MINE"}, _commitment("COM-MINE", person_id="PER-1", **{"class": "mine"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-OWED"}, _commitment("COM-OWED", person_id="PER-1", **{"class": "owed_to_me"}))

    request = QueryRequest(
        text="Who am I waiting on?", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(person_ref=None),
    )
    result = execute_query(db, request)

    assert result.status == QueryResultStatus.OK
    assert [r["id"] for r in result.records] == ["COM-OWED"]


def test_execute_query_what_do_i_owe_resolves_ownership_deterministically(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-MINE"}, _commitment("COM-MINE", **{"class": "mine"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-OWED"}, _commitment("COM-OWED", **{"class": "owed_to_me"}))

    result = execute_query(db, QueryRequest(text="What do I owe?", reference_datetime=_REF, timezone="UTC"))

    assert [r["id"] for r in result.records] == ["COM-MINE"]


def test_filters_ownership_overrides_text_extracted_hint(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-MINE"}, _commitment("COM-MINE", **{"class": "mine"}))
    CommitmentRepository(db).upsert_by_key({"id": "COM-OWED"}, _commitment("COM-OWED", **{"class": "owed_to_me"}))

    # Text says "waiting on" (-> other_owes hint), but the caller explicitly overrides.
    request = QueryRequest(
        text="Who am I waiting on?", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(ownership=OwnershipDirection.USER_OWES),
    )
    result = execute_query(db, request)

    assert [r["id"] for r in result.records] == ["COM-MINE"]


# --- Person / org / project scoping ----------------------------------------------------------


def test_get_commitments_for_person(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1", person_id="PER-1"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-2"}, _commitment("COM-2", person_id="PER-2"))

    result = cq.get_commitments_for_person(db, "PER-1")

    assert [c["id"] for c in result] == ["COM-1"]


def test_get_commitments_for_org(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1", org_id="ORG-1"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-2"}, _commitment("COM-2", org_id="ORG-2"))

    result = cq.get_commitments_for_org(db, "ORG-1")

    assert [c["id"] for c in result] == ["COM-1"]


def test_get_commitments_for_project_honestly_empty_when_unpopulated(db):
    # Confirms the documented Phase 22A/24 finding: project_id is never populated by
    # the live pipeline -- this query is real but returns nothing on realistic data.
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1"))  # project_id stays None

    result = cq.get_commitments_for_project(db, "PRJ-1")

    assert result == []


def test_get_commitments_for_project_returns_when_actually_populated(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1", project_id="PRJ-1"))

    result = cq.get_commitments_for_project(db, "PRJ-1")

    assert [c["id"] for c in result] == ["COM-1"]


# --- Open / overdue ----------------------------------------------------------------------------


def test_get_open_commitments_uses_real_stored_status(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-OPEN"}, _commitment("COM-OPEN", status="open"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-CLOSED"}, _commitment("COM-CLOSED", status="closed"))

    result = cq.get_open_commitments(db)

    assert [c["id"] for c in result] == ["COM-OPEN"]


def test_get_overdue_commitments_uses_committed_date(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-PAST"}, _commitment("COM-PAST", committed_date="2026-01-01T00:00:00Z"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-FUTURE"}, _commitment("COM-FUTURE", committed_date="2026-06-01T00:00:00Z"))

    result = cq.get_overdue_commitments(db, reference_datetime=_REF, timezone="UTC")

    assert [c["id"] for c in result] == ["COM-PAST"]


def test_get_overdue_commitments_no_match_query(db):
    result = cq.get_overdue_commitments(db, reference_datetime=_REF, timezone="UTC", person_id="PER-NOBODY")
    assert result == []


# --- Follow-ups ------------------------------------------------------------------------------


def test_get_followups_for_person(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-1"}, {"id": "FU-1", "commitment_id": None, "thread_id": "t1", "person_id": "PER-1", "org_id": None})
    FollowUpRepository(db).upsert_by_key({"id": "FU-2"}, {"id": "FU-2", "commitment_id": None, "thread_id": "t2", "person_id": "PER-2", "org_id": None})

    result = cq.get_followups_for_person(db, "PER-1")

    assert [f["id"] for f in result] == ["FU-1"]


def test_get_open_followups_treats_a_legacy_document_with_no_status_field_as_active(db):
    # A FollowUp written before BRD 6.4's status field existed has no "status" key at
    # all -- missing is treated the same as "active" (mirrors Person.status's own
    # missing-means-active convention), so a pre-existing record is never silently
    # excluded just because it predates the field.
    FollowUpRepository(db).upsert_by_key({"id": "FU-1"}, {"id": "FU-1", "commitment_id": None, "thread_id": "t1", "person_id": None, "org_id": None})
    assert "status" not in FollowUpRepository(db).find_one({"id": "FU-1"})

    result = cq.get_open_followups(db)

    assert len(result) == 1


def test_get_open_followups_excludes_resolved_and_dropped(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-A"}, _follow_up("FU-A", status="active"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-B"}, _follow_up("FU-B", status="resolved"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-C"}, _follow_up("FU-C", status="dropped"))

    result = cq.get_open_followups(db)

    assert [f["id"] for f in result] == ["FU-A"]


def test_get_followups_by_escalation_level_filters_exactly(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-L1"}, _follow_up("FU-L1", escalation_level=1))
    FollowUpRepository(db).upsert_by_key({"id": "FU-L2"}, _follow_up("FU-L2", escalation_level=2))

    result = cq.get_followups_by_escalation_level(db, 2)

    assert [f["id"] for f in result] == ["FU-L2"]


def test_get_followups_by_audience_filters_exactly(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-INT"}, _follow_up("FU-INT", audience="internal"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-CLI"}, _follow_up("FU-CLI", audience="client_fixed_date"))

    result = cq.get_followups_by_audience(db, "internal")

    assert [f["id"] for f in result] == ["FU-INT"]


def test_describe_escalation_level_uses_the_brd_wording():
    assert "surfaced" in cq.describe_escalation_level(1).lower()
    assert "top of the queue" in cq.describe_escalation_level(2).lower()
    assert "15-minute hold" in cq.describe_escalation_level(3).lower()
    assert "stopped" in cq.describe_escalation_level(4).lower()


def test_describe_escalation_level_rejects_out_of_range():
    with pytest.raises(ValueError):
        cq.describe_escalation_level(5)


def test_next_escalation_level_advances_and_caps_at_four():
    assert cq.next_escalation_level(1) == 2
    assert cq.next_escalation_level(2) == 3
    assert cq.next_escalation_level(3) == 4
    assert cq.next_escalation_level(4) == 4  # terminal -- never advances past 4


def test_get_overdue_followups_via_parent_commitment(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-PAST"}, _commitment("COM-PAST", committed_date="2026-01-01T00:00:00Z"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-PAST"}, {"id": "FU-PAST", "commitment_id": "COM-PAST", "thread_id": "t1", "person_id": None, "org_id": None})

    result = cq.get_overdue_followups(db, reference_datetime=_REF, timezone="UTC")

    assert [f["id"] for f in result] == ["FU-PAST"]


def test_get_overdue_followups_excludes_undated_parent(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-UNDATED"}, _commitment("COM-UNDATED", committed_date=None))
    FollowUpRepository(db).upsert_by_key({"id": "FU-UNDATED"}, {"id": "FU-UNDATED", "commitment_id": "COM-UNDATED", "thread_id": "t1", "person_id": None, "org_id": None})

    result = cq.get_overdue_followups(db, reference_datetime=_REF, timezone="UTC")

    assert result == []


def test_get_followups_for_commitment(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-1"}, {"id": "FU-1", "commitment_id": "COM-1", "thread_id": None, "person_id": None, "org_id": None})
    FollowUpRepository(db).upsert_by_key({"id": "FU-2"}, {"id": "FU-2", "commitment_id": "COM-2", "thread_id": None, "person_id": None, "org_id": None})

    result = cq.get_followups_for_commitment(db, "COM-1")

    assert [f["id"] for f in result] == ["FU-1"]


# --- Meeting <-> Commitment relationship (24.6) -----------------------------------------------


def test_meeting_related_commitments_is_always_indirect(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-01-01T00:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True, "thread_id": "t1"})
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, {**_commitment("COM-1"), "thread_id": "t1"})

    result = cq.get_meeting_related_commitments(db, "MTG-1")

    assert result["relationship"] == "indirect_via_thread"
    assert [c["id"] for c in result["commitments"]] == ["COM-1"]


def test_meeting_related_commitments_missing_meeting(db):
    result = cq.get_meeting_related_commitments(db, "MTG-DOES-NOT-EXIST")
    assert result["commitments"] == []
    assert result["relationship"] == "indirect_via_thread"


# --- Cross-cutting: merged/ambiguous person via service, lifecycle, read-only ------------------


def test_execute_query_commitments_merged_person_resolves_to_canonical_evidence(db):
    from app.query.schemas import EntityReference, ResolutionStatus

    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1", person_id="PER-2"))
    ref = EntityReference(kind="person", resolution_status=ResolutionStatus.RESOLVED, resolved_id="PER-2")

    result = execute_query(db, QueryRequest(text="What are my open commitments?", reference_datetime=_REF, timezone="UTC", filters=QueryFilters(person_ref=ref)))

    assert result.status == QueryResultStatus.OK
    assert result.records[0]["person_id"] == "PER-2"


def test_commitment_and_followup_functions_never_write(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-1"}, {"id": "FU-1", "commitment_id": "COM-1", "thread_id": "t1", "person_id": None, "org_id": None})
    before = {c: list(db[c].find({}, {"_id": 0})) for c in ["commitments", "follow_ups"]}

    cq.get_user_commitments(db)
    cq.get_commitments_owed_to_user(db)
    cq.get_open_commitments(db)
    cq.get_overdue_commitments(db, reference_datetime=_REF, timezone="UTC")
    cq.get_followups_for_person(db, "PER-1")
    cq.get_overdue_followups(db, reference_datetime=_REF, timezone="UTC")
    cq.get_followups_for_commitment(db, "COM-1")

    after = {c: list(db[c].find({}, {"_id": 0})) for c in before}
    assert before == after
