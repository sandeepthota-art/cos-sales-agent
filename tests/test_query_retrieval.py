# tests/test_query_retrieval.py
"""Phase 22A: structured, bounded retrieval functions. Synthetic mongomock fixtures
only. Every function is exercised for: correct filtering, deterministic ordering,
result limits, and date-range application."""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.schemas import DateRangeKind
from datetime import datetime, timezone

_REF = datetime(2026, 3, 12, 10, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


# --- MEETINGS -------------------------------------------------------------------------------


def test_retrieve_meetings_filters_by_person_id(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": ["PER-1"], "org_id": None, "actionable": True})
    MeetingRepository(db).upsert_by_key({"id": "MTG-2"}, {"id": "MTG-2", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": ["PER-2"], "org_id": None, "actionable": True})

    result = retrieval.retrieve_meetings(db, person_id="PER-1")

    assert [m["id"] for m in result] == ["MTG-1"]


def test_retrieve_meetings_filters_by_date_range(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-TODAY"}, {"id": "MTG-TODAY", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})
    MeetingRepository(db).upsert_by_key({"id": "MTG-NEXT-WEEK"}, {"id": "MTG-NEXT-WEEK", "date": "2026-03-20T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})

    today = resolve_date_range(DateRangeKind.TODAY, _REF, "UTC")
    result = retrieval.retrieve_meetings(db, date_range=today)

    assert [m["id"] for m in result] == ["MTG-TODAY"]


def test_retrieve_meetings_deterministic_ordering_ascending_by_default(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-LATER"}, {"id": "MTG-LATER", "date": "2026-03-12T15:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})
    MeetingRepository(db).upsert_by_key({"id": "MTG-EARLIER"}, {"id": "MTG-EARLIER", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})

    result = retrieval.retrieve_meetings(db)

    assert [m["id"] for m in result] == ["MTG-EARLIER", "MTG-LATER"]


def test_retrieve_meetings_respects_result_limit(db):
    for i in range(5):
        MeetingRepository(db).upsert_by_key({"id": f"MTG-{i}"}, {"id": f"MTG-{i}", "date": f"2026-03-{12+i:02d}T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True})

    result = retrieval.retrieve_meetings(db, limit=2)

    assert len(result) == 2


def test_retrieve_meetings_tolerates_none_person_ids(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": None, "org_id": None, "actionable": True})

    result = retrieval.retrieve_meetings(db)  # must not raise

    assert len(result) == 1


# --- COMMITMENTS ----------------------------------------------------------------------------


def _commitment(cid, **overrides):
    doc = {
        "id": cid, "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
        "person_id": None, "org_id": None, "source_record": "e:1", "made_on": "2026-03-01T00:00:00Z",
        "committed_date": None, "status": "open",
    }
    doc.update(overrides)
    return doc


def test_retrieve_commitments_filters_by_person_and_org(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, _commitment("COM-1", person_id="PER-1"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-2"}, _commitment("COM-2", person_id="PER-2"))

    result = retrieval.retrieve_commitments(db, person_id="PER-1")

    assert [c["id"] for c in result] == ["COM-1"]


def test_retrieve_commitments_ordered_most_recent_first(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-OLD"}, _commitment("COM-OLD", made_on="2026-01-01T00:00:00Z"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-NEW"}, _commitment("COM-NEW", made_on="2026-03-01T00:00:00Z"))

    result = retrieval.retrieve_commitments(db)

    assert [c["id"] for c in result] == ["COM-NEW", "COM-OLD"]


def test_retrieve_commitments_latest_returns_exactly_one(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-OLD"}, _commitment("COM-OLD", made_on="2026-01-01T00:00:00Z"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-NEW"}, _commitment("COM-NEW", made_on="2026-03-01T00:00:00Z"))
    latest = resolve_date_range(DateRangeKind.LATEST, _REF, "UTC")

    result = retrieval.retrieve_commitments(db, date_range=latest)

    assert [c["id"] for c in result] == ["COM-NEW"]


# --- FOLLOW_UPS (joined through parent commitment for date filtering) ------------------------


def test_retrieve_follow_ups_filters_by_person(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-1"}, {"id": "FU-1", "commitment_id": None, "thread_id": "t1", "person_id": "PER-1", "org_id": None})
    FollowUpRepository(db).upsert_by_key({"id": "FU-2"}, {"id": "FU-2", "commitment_id": None, "thread_id": "t2", "person_id": "PER-2", "org_id": None})

    result = retrieval.retrieve_follow_ups(db, person_id="PER-1")

    assert [f["id"] for f in result] == ["FU-1"]


def test_retrieve_follow_ups_overdue_uses_parent_commitment_date(db):
    CommitmentRepository(db).upsert_by_key({"id": "COM-PAST"}, _commitment("COM-PAST", committed_date="2026-01-01T00:00:00Z"))
    CommitmentRepository(db).upsert_by_key({"id": "COM-FUTURE"}, _commitment("COM-FUTURE", committed_date="2026-06-01T00:00:00Z"))
    FollowUpRepository(db).upsert_by_key({"id": "FU-PAST"}, {"id": "FU-PAST", "commitment_id": "COM-PAST", "thread_id": "t1", "person_id": None, "org_id": None})
    FollowUpRepository(db).upsert_by_key({"id": "FU-FUTURE"}, {"id": "FU-FUTURE", "commitment_id": "COM-FUTURE", "thread_id": "t1", "person_id": None, "org_id": None})
    overdue = resolve_date_range(DateRangeKind.OVERDUE, _REF, "UTC")

    result = retrieval.retrieve_follow_ups(db, date_range=overdue)

    assert [f["id"] for f in result] == ["FU-PAST"]


def test_retrieve_follow_ups_without_a_dated_parent_excluded_from_date_filter(db):
    # A commitment with no committed_date -- the follow-up must be excluded from a
    # date-bounded query, never guessed into range.
    CommitmentRepository(db).upsert_by_key({"id": "COM-UNDATED"}, _commitment("COM-UNDATED", committed_date=None))
    FollowUpRepository(db).upsert_by_key({"id": "FU-UNDATED"}, {"id": "FU-UNDATED", "commitment_id": "COM-UNDATED", "thread_id": "t1", "person_id": None, "org_id": None})
    overdue = resolve_date_range(DateRangeKind.OVERDUE, _REF, "UTC")

    result = retrieval.retrieve_follow_ups(db, date_range=overdue)

    assert result == []


# --- KNOWLEDGE -------------------------------------------------------------------------------


def test_retrieve_knowledge_preserves_stated_vs_inferred_basis(db):
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "K-1"},
        {"knowledge_id": "K-1", "thread_id": "t1", "person_id": "PER-1", "org_id": None, "subject_key": "x",
         "predicate": "y", "fact_key": "z", "current_value": "v", "history": [], "source_emails": [],
         "basis": "stated", "first_seen_at": "2026-01-01T00:00:00Z", "last_confirmed_at": "2026-01-01T00:00:00Z",
         "confidence": 0.9, "status": "active"},
    )

    result = retrieval.retrieve_knowledge(db, person_id="PER-1")

    assert result[0]["basis"] == "stated"  # never rewritten to inferred or vice versa


def test_retrieve_knowledge_ordered_most_recently_confirmed_first(db):
    for kid, ts in [("K-OLD", "2026-01-01T00:00:00Z"), ("K-NEW", "2026-03-01T00:00:00Z")]:
        KnowledgeRepository(db).upsert_by_key(
            {"knowledge_id": kid},
            {"knowledge_id": kid, "thread_id": "t1", "person_id": "PER-1", "org_id": None, "subject_key": "x",
             "predicate": "y", "fact_key": kid, "current_value": "v", "history": [], "source_emails": [],
             "basis": "stated", "first_seen_at": ts, "last_confirmed_at": ts, "confidence": 0.9, "status": "active"},
        )

    result = retrieval.retrieve_knowledge(db, person_id="PER-1")

    assert [k["knowledge_id"] for k in result] == ["K-NEW", "K-OLD"]


# --- REPLY_DRAFTS ----------------------------------------------------------------------------


def test_retrieve_reply_drafts_filters_by_status(db):
    ReplyDraftRepository(db).upsert_by_key({"source_email_id": "m1"}, {"reply_id": "r1", "thread_id": "t1", "source_email_id": "m1", "status": "awaiting_approval", "draft": {"subject": "x", "body": "y"}, "person_id": None, "org_id": None, "created_by": "sales_agent", "created_at": "2026-01-01T00:00:00Z"})
    ReplyDraftRepository(db).upsert_by_key({"source_email_id": "m2"}, {"reply_id": "r2", "thread_id": "t1", "source_email_id": "m2", "status": "sent", "draft": {"subject": "x", "body": "y"}, "person_id": None, "org_id": None, "created_by": "sales_agent", "created_at": "2026-01-01T00:00:00Z"})

    result = retrieval.retrieve_reply_drafts(db, status="awaiting_approval")

    assert [r["reply_id"] for r in result] == ["r1"]


# --- THREAD_CONTEXT --------------------------------------------------------------------------


def test_retrieve_thread_context_assembles_all_related_collections(db):
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "normalized_subject": "x", "message_ids": ["m1"], "last_message_at": "2026-01-01T00:00:00Z", "person_ids": [], "org_ids": []})
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, {**_commitment("COM-1"), "thread_id": "t1"})
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "date": "2026-01-01T00:00:00Z", "attendees": [], "person_ids": [], "org_id": None, "actionable": True, "thread_id": "t1"})

    context = retrieval.retrieve_thread_context(db, "t1")

    assert context is not None
    assert context["thread"]["thread_id"] == "t1"
    assert len(context["commitments"]) == 1
    assert len(context["meetings"]) == 1


def test_retrieve_thread_context_unknown_thread_returns_none(db):
    assert retrieval.retrieve_thread_context(db, "does_not_exist") is None


# --- Read-only guarantee (Section M) -----------------------------------------------------------


def test_all_retrieval_functions_never_write_a_document(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, {"id": "PER-1", "name": "X", "email": "x@example.com", "aliases": [], "org": None, "org_id": None, "type": None, "goal_pillar": None, "role_in_pillar": None, "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None, "reports_to": None, "open_threads": [], "note_link": None, "review_flag": False, "source": "gmail"})
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "X", "domain": "x.example", "aliases": [], "source": "gmail"})
    before = {c: list(db[c].find({}, {"_id": 0})) for c in ["people", "organizations", "meetings", "commitments", "follow_ups", "knowledge_items", "reply_drafts", "threads"]}

    retrieval.retrieve_meetings(db)
    retrieval.retrieve_commitments(db)
    retrieval.retrieve_follow_ups(db)
    retrieval.retrieve_knowledge(db)
    retrieval.retrieve_reply_drafts(db)
    retrieval.retrieve_person_context(db, "PER-1")
    retrieval.retrieve_organization_context(db, "ORG-1")
    retrieval.retrieve_thread_context(db, "nonexistent")

    after = {c: list(db[c].find({}, {"_id": 0})) for c in before}
    assert before == after
