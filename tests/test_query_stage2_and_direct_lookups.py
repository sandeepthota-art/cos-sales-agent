# tests/test_query_stage2_and_direct_lookups.py
"""Phase 22B.2 (Stage-2 LLM intent fallback) and 22B.4 (direct meeting_id/thread_id
lookups). No live LLM call anywhere -- llm_classify is always a plain injected
callable."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import MeetingRepository, PersonRepository, ThreadRepository
from app.query.intent import IntentParseError, classify_intent_with_fallback
from app.query.schemas import QueryFilters, QueryIntentType, QueryRequest, QueryResultStatus
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


# --- Stage 2 fallback --------------------------------------------------------------------


def test_stage2_not_invoked_when_stage1_already_classifies():
    calls = []

    def fake_llm(text):
        calls.append(text)
        return {"intent": "meetings"}

    parsed = classify_intent_with_fallback("What meetings do I have today?", llm_classify=fake_llm)

    assert parsed.intent == QueryIntentType.MEETINGS
    assert calls == []  # Stage 1 already succeeded -- Stage 2 must not run


def test_stage2_invoked_only_when_stage1_is_unsupported():
    def fake_llm(text):
        return {"intent": "commitments", "person_text": "Ashok"}

    parsed = classify_intent_with_fallback("asdkjfh nonsense zzz", llm_classify=fake_llm)

    assert parsed.intent == QueryIntentType.COMMITMENTS
    assert parsed.person_text == "Ashok"


def test_stage2_malformed_output_falls_back_to_unsupported_not_a_crash():
    def fake_llm(text):
        return {"not_a_real_field": True}

    parsed = classify_intent_with_fallback("asdkjfh nonsense zzz", llm_classify=fake_llm)

    assert parsed.intent == QueryIntentType.UNSUPPORTED


def test_stage2_invalid_intent_value_falls_back_to_unsupported():
    def fake_llm(text):
        return {"intent": "delete_everything"}

    parsed = classify_intent_with_fallback("asdkjfh nonsense zzz", llm_classify=fake_llm)

    assert parsed.intent == QueryIntentType.UNSUPPORTED


def test_stage2_provider_exception_falls_back_safely():
    def fake_llm(text):
        raise TimeoutError("simulated provider timeout")

    parsed = classify_intent_with_fallback("asdkjfh nonsense zzz", llm_classify=fake_llm)

    assert parsed.intent == QueryIntentType.UNSUPPORTED


def test_stage2_cannot_select_an_ambiguous_person_itself(db):
    # Even if Stage 2 names a person, ENTITY RESOLUTION -- not the LLM -- decides
    # ambiguity; the LLM output is only ever a name hint, never a resolved id.
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Jason Greene"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Jason Smith"))

    def fake_llm(text):
        return {"intent": "person_context", "person_text": "Jason"}

    request = QueryRequest(text="asdkjfh nonsense zzz", reference_datetime=_REF, timezone="UTC")
    result = execute_query(db, request, llm_classify=fake_llm)

    assert result.status == QueryResultStatus.AMBIGUOUS
    assert len(result.ambiguities[0].candidates) == 2


# --- Direct ID lookups (22B.4) --------------------------------------------------------------


def test_direct_meeting_id_unblocks_meeting_preparation(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})

    request = QueryRequest(
        text="Prepare me for my meeting with this person.", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(meeting_id="MTG-1"),
    )
    result = execute_query(db, request)

    assert result.status == QueryResultStatus.OK
    assert result.intent == QueryIntentType.MEETING_PREPARATION


def test_direct_meeting_id_unknown_returns_no_match(db):
    request = QueryRequest(
        text="Prepare me for my meeting with this person.", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(meeting_id="MTG-DOES-NOT-EXIST"),
    )
    result = execute_query(db, request)
    assert result.status == QueryResultStatus.NO_MATCH


def test_meeting_preparation_still_data_incomplete_without_a_meeting_id(db):
    request = QueryRequest(text="Prepare me for my meeting with this person.", reference_datetime=_REF, timezone="UTC")
    result = execute_query(db, request)
    assert result.status == QueryResultStatus.DATA_INCOMPLETE


def test_direct_thread_id_unblocks_specific_thread_context(db):
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "normalized_subject": "x", "message_ids": ["m1"], "last_message_at": "2026-01-01T00:00:00Z", "person_ids": [], "org_ids": []})

    request = QueryRequest(
        text="What happened in the latest thread?", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(thread_id="t1"),
    )
    result = execute_query(db, request)

    assert result.status == QueryResultStatus.OK
    assert result.records[0]["thread"]["thread_id"] == "t1"


def test_direct_thread_id_takes_priority_over_latest_thread_fallback(db):
    ThreadRepository(db).upsert_by_key({"thread_id": "t_old"}, {"thread_id": "t_old", "normalized_subject": "x", "message_ids": [], "last_message_at": "2020-01-01T00:00:00Z", "person_ids": [], "org_ids": []})
    ThreadRepository(db).upsert_by_key({"thread_id": "t_new"}, {"thread_id": "t_new", "normalized_subject": "x", "message_ids": [], "last_message_at": "2026-01-01T00:00:00Z", "person_ids": [], "org_ids": []})

    request = QueryRequest(
        text="What happened in the latest thread?", reference_datetime=_REF, timezone="UTC",
        filters=QueryFilters(thread_id="t_old"),
    )
    result = execute_query(db, request)

    assert result.records[0]["thread"]["thread_id"] == "t_old"  # explicit id wins, not "latest"
