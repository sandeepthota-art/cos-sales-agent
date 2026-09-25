# tests/test_query_email_activity.py
"""Phase 22B.3: bounded, read-only email activity retrieval. Uses only fields the
live pipeline actually populates -- entities_referenced.people for person scoping,
Thread.message_ids for thread scoping (never a thread_id field on the Email
document itself, which the live pipeline never sets)."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository, PersonRepository, ThreadRepository
from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.schemas import DateRangeKind, QueryFilters, QueryIntentType, QueryRequest, QueryResultStatus
from app.query.service import execute_query

_REF = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _email(message_id, **overrides):
    doc = {
        "message_id": message_id, "thread_id": None, "from": {"name": "X", "email": "x@example.com"},
        "to": [], "cc": [], "subject": "Subject", "body": "body", "timestamp": "2026-03-01T00:00:00Z",
        "in_reply_to": None, "references": [], "attachments": [], "labels": [],
        "entities_referenced": {"people": []}, "label_applied": None,
    }
    doc.update(overrides)
    return doc


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


# --- retrieve_email_activity -----------------------------------------------------------------


def test_retrieve_email_activity_filters_by_person(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", entities_referenced={"people": ["PER-1"]}))
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2", entities_referenced={"people": ["PER-2"]}))

    result = retrieval.retrieve_email_activity(db, person_id="PER-1")

    assert [e["message_id"] for e in result] == ["m1"]


def test_retrieve_email_activity_filters_by_org_via_org_persons(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", org_id="ORG-1"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", org_id="ORG-2"))
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", entities_referenced={"people": ["PER-1"]}))
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2", entities_referenced={"people": ["PER-2"]}))

    result = retrieval.retrieve_email_activity(db, org_id="ORG-1")

    assert [e["message_id"] for e in result] == ["m1"]


def test_retrieve_email_activity_filters_by_thread_via_thread_message_ids(db):
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "normalized_subject": "x", "message_ids": ["m1"], "last_message_at": "2026-01-01T00:00:00Z", "person_ids": [], "org_ids": []})
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1"))
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2"))

    result = retrieval.retrieve_email_activity(db, thread_id="t1")

    assert [e["message_id"] for e in result] == ["m1"]


def test_retrieve_email_activity_date_scoped(db):
    EmailRepository(db).upsert_by_key({"message_id": "m_old"}, _email("m_old", timestamp="2020-01-01T00:00:00Z"))
    EmailRepository(db).upsert_by_key({"message_id": "m_new"}, _email("m_new", timestamp="2026-03-12T09:00:00Z"))
    today = resolve_date_range(DateRangeKind.TODAY, _REF, "UTC")

    result = retrieval.retrieve_email_activity(db, date_range=today)

    assert [e["message_id"] for e in result] == ["m_new"]


def test_retrieve_email_activity_never_fabricates_sentiment_or_priority(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", label_applied="Needs reply: ASAP"))

    result = retrieval.retrieve_email_activity(db)

    assert result[0]["label_applied"] == "Needs reply: ASAP"  # passed through, real field
    assert "sentiment" not in result[0]
    assert "priority" not in result[0]
    assert "sales_stage" not in result[0]


def test_retrieve_email_activity_ordered_most_recent_first(db):
    EmailRepository(db).upsert_by_key({"message_id": "m_old"}, _email("m_old", timestamp="2026-01-01T00:00:00Z"))
    EmailRepository(db).upsert_by_key({"message_id": "m_new"}, _email("m_new", timestamp="2026-03-01T00:00:00Z"))

    result = retrieval.retrieve_email_activity(db)

    assert [e["message_id"] for e in result] == ["m_new", "m_old"]


# --- service integration --------------------------------------------------------------------


def test_execute_query_email_activity_intent(db):
    # "recent" triggers DateRangeKind.RECENT (last 7 days from _REF) -- the fixture
    # email must fall inside that window.
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", timestamp="2026-03-10T00:00:00Z"))

    request = QueryRequest(text="What is the recent email activity?", reference_datetime=_REF, timezone="UTC")
    result = execute_query(db, request)

    assert result.intent == QueryIntentType.EMAIL_ACTIVITY
    assert result.status == QueryResultStatus.OK
    assert result.evidence[0].collection == "emails"


def test_execute_query_email_activity_never_writes(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1"))
    before = list(db.emails.find({}, {"_id": 0}))

    execute_query(db, QueryRequest(text="What is the recent email activity?", reference_datetime=_REF, timezone="UTC"))

    after = list(db.emails.find({}, {"_id": 0}))
    assert before == after
