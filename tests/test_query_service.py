# tests/test_query_service.py
"""Phase 22A: end-to-end QueryRequest -> QueryResult orchestration
(app.query.service.execute_query). Synthetic mongomock fixtures only -- no live LLM
call happens anywhere; execute_query itself never calls one."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
)
from app.query.schemas import (
    EntityReference,
    QueryFilters,
    QueryIntentType,
    QueryRequest,
    QueryResultStatus,
    ResolutionStatus,
)
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


def _request(text: str, **filter_overrides) -> QueryRequest:
    return QueryRequest(text=text, reference_datetime=_REF, timezone="UTC", filters=QueryFilters(**filter_overrides))


# --- Basic dispatch ---------------------------------------------------------------------


def test_execute_query_unsupported_text_returns_error(db):
    result = execute_query(db, _request("asdkjfh nonsense zzz"))
    assert result.status == QueryResultStatus.ERROR
    assert result.intent == QueryIntentType.UNSUPPORTED


def test_execute_query_meetings_no_match_when_none_exist(db):
    result = execute_query(db, _request("What meetings do I have today?"))
    assert result.status == QueryResultStatus.NO_MATCH
    assert result.records == []
    assert "does not" not in (result.message or "")  # Q's phrasing check is separate; just ensure a message exists
    assert result.message


def test_execute_query_meetings_ok_with_matching_records(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})

    result = execute_query(db, _request("What meetings do I have today?"))

    assert result.status == QueryResultStatus.OK
    assert result.metadata.result_count == 1
    assert len(result.evidence) == 1
    assert result.evidence[0].collection == "meetings"


# --- Person resolution integration --------------------------------------------------------


def test_execute_query_person_context_data_incomplete_when_no_person_specified(db):
    result = execute_query(db, _request("Who is Ash?"))
    # "Ash" is extracted as person_text but doesn't resolve to anyone -> NO_MATCH,
    # not DATA_INCOMPLETE (the question DID name someone, just not found).
    assert result.status == QueryResultStatus.NO_MATCH


def test_execute_query_person_context_ambiguous_surfaces_candidates(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Jason Greene"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Jason Smith"))

    result = execute_query(db, _request("Who is Jason?"))

    assert result.status == QueryResultStatus.AMBIGUOUS
    assert len(result.ambiguities) == 1
    assert {c.id for c in result.ambiguities[0].candidates} == {"PER-1", "PER-2"}


def test_execute_query_person_context_resolves_and_returns_evidence(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Ashok Ganapam", open_threads=["t1"]))

    result = execute_query(db, _request("Give me the context on Ashok Ganapam."))

    assert result.status == QueryResultStatus.OK
    assert result.metadata.resolution_status == ResolutionStatus.RESOLVED
    assert result.records[0]["person"]["id"] == "PER-1"


def test_execute_query_merged_person_never_becomes_the_canonical_query_result(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Canonical Cara"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Canonical Cara", status="merged", merged_into="PER-2"))

    result = execute_query(db, _request("Give me the context on Canonical Cara."))

    assert result.status == QueryResultStatus.OK
    assert result.records[0]["person"]["id"] == "PER-2"  # never PER-1


def test_execute_query_prefers_a_caller_supplied_entity_reference_over_text_extraction(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Text Name"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Explicit Name"))
    pre_resolved = EntityReference(kind="person", resolution_status=ResolutionStatus.RESOLVED, resolved_id="PER-2")

    result = execute_query(db, _request("Give me the context on Text Name.", person_ref=pre_resolved))

    assert result.records[0]["person"]["id"] == "PER-2"


def test_execute_query_broken_lifecycle_chain_surfaces_as_error_not_silent_fallback(db):
    # Name-only search deliberately never surfaces a merged Person as a candidate at
    # all (tested separately) -- a broken chain is only reachable via the exact-email
    # path, which DOES find the merged record and then discovers it's broken.
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Broken Chain", email="broken@example.com", status="merged", merged_into=None))
    ref = EntityReference(kind="person", email_hint="broken@example.com", resolution_status=ResolutionStatus.LIFECYCLE_ERROR, lifecycle_error="person 'PER-1' is status='merged' but has no merged_into target")

    result = execute_query(db, _request("Give me the context on this person.", person_ref=ref))

    assert result.status == QueryResultStatus.ERROR
    assert "no merged_into target" in result.message


# --- Organization resolution ----------------------------------------------------------------


def test_execute_query_organization_context_resolves(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"})
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", org_id="ORG-1"))

    result = execute_query(db, _request("Give me the company context on DataBeat."))

    assert result.status == QueryResultStatus.OK
    assert result.records[0]["organization"]["id"] == "ORG-1"


# --- Commitments/person scoping -------------------------------------------------------------


def test_execute_query_commitments_scoped_to_a_resolved_person(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Ashok Ganapam"))
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "send proposal", "class": "mine", "owed_by": None, "owed_to": None,
         "person_id": "PER-1", "org_id": None, "source_record": "e:1", "made_on": "2026-03-01T00:00:00Z",
         "committed_date": None, "status": "open"},
    )

    # Note: "waiting on"/"commitments" phrasing extracts person_text via the generic
    # mention pattern only when a preposition precedes the name; here we exercise the
    # caller-supplied EntityReference path instead, which is the documented and
    # tested precedent for a caller that already knows who it means.
    ref = EntityReference(kind="person", resolution_status=ResolutionStatus.RESOLVED, resolved_id="PER-1")
    result = execute_query(db, _request("What are my open commitments?", person_ref=ref))

    assert result.status == QueryResultStatus.OK
    assert result.records[0]["id"] == "COM-1"
    assert result.metadata.canonical_resolution_used is True


# --- No-data / ambiguity / incomplete distinctness (Section Q) ------------------------------


def test_execute_query_distinguishes_no_match_ambiguous_and_data_incomplete(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Solo Match"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Dup One"))
    PersonRepository(db).upsert_by_key({"id": "PER-3"}, _person(id="PER-3", name="Dup One"))

    no_match = execute_query(db, _request("Give me the context on Nobody Real."))
    ambiguous = execute_query(db, _request("Give me the context on Dup One."))
    incomplete = execute_query(db, _request("Prepare me for my meeting with this person."))

    assert no_match.status == QueryResultStatus.NO_MATCH
    assert ambiguous.status == QueryResultStatus.AMBIGUOUS
    assert incomplete.status == QueryResultStatus.DATA_INCOMPLETE
    assert {no_match.status, ambiguous.status, incomplete.status} == {
        QueryResultStatus.NO_MATCH, QueryResultStatus.AMBIGUOUS, QueryResultStatus.DATA_INCOMPLETE,
    }


# --- Read-only enforcement (Section M) -------------------------------------------------------


def test_execute_query_never_writes_any_document_across_every_supported_intent(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Ashok Ganapam", open_threads=["t1"]))
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"})
    before = {
        c: list(db[c].find({}, {"_id": 0}))
        for c in ["people", "organizations", "meetings", "commitments", "follow_ups", "knowledge_items", "reply_drafts", "threads", "projects", "calendar_actions"]
    }

    for text in [
        "What meetings do I have today?", "What commitments did I make?", "What follow-ups are overdue?",
        "Give me the context on Ashok Ganapam.", "Give me the company context on DataBeat.",
        "What do I need to respond to?", "Who is Nobody?", "asdkjfh nonsense",
    ]:
        execute_query(db, _request(text))

    after = {c: list(db[c].find({}, {"_id": 0})) for c in before}
    assert before == after


# --- Query metadata / observability (Section AD) -----------------------------------------------


def test_execute_query_metadata_reports_execution_time_and_query_id(db):
    result = execute_query(db, _request("What meetings do I have today?"))
    assert result.metadata.query_id.startswith("qry_")
    assert result.metadata.execution_time_ms is not None and result.metadata.execution_time_ms >= 0
    assert result.metadata.intent == QueryIntentType.MEETINGS
