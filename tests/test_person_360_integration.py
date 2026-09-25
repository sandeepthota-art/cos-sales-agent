# tests/test_person_360_integration.py
"""Phase 5: a single, deliberately end-to-end integration test proving that one
canonical person (a synthetic PER-TEST-001, never a real historical Atlas ID) can
retrieve context across every entity type this schema supports, entirely via
canonical id references -- never via duplicated content, and never against real
Atlas data (mongomock only, per this task's isolation rule).
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entities.context import get_person_context

_NOW = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc).isoformat()

PERSON_ID = "PER-TEST-001"
ORG_ID = "ORG-TEST-001"
THREAD_ID = "thread-test-001"
MESSAGE_ID = "msg-test-001"


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def seeded(db):
    """Seeds one synthetic Person/Organization and one fixture per related entity
    type, every one of them referencing PERSON_ID/ORG_ID canonically -- alongside
    their pre-existing, untouched free-text fields (owed_by, attendees, collaborators,
    org), exactly matching how the real pipeline writes these documents today.
    """
    OrganizationRepository(db).upsert_by_key(
        {"id": ORG_ID},
        {"id": ORG_ID, "name": "Test Co", "domain": "testco.example", "aliases": [], "source": "gmail"},
    )
    PersonRepository(db).upsert_by_key(
        {"id": PERSON_ID},
        {
            "id": PERSON_ID, "name": "Test Person", "email": "test.person@testco.example", "aliases": [],
            "org": "Test Co", "org_id": ORG_ID, "type": None, "goal_pillar": None, "role_in_pillar": None,
            "tier": None, "voice_register": None, "last_inbound": _NOW, "last_outbound": None,
            "reports_to": None, "open_threads": [THREAD_ID], "note_link": None,
            "review_flag": False, "source": "gmail",
        },
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": MESSAGE_ID},
        {
            "message_id": MESSAGE_ID, "thread_id": THREAD_ID, "subject": "Test subject",
            "timestamp": _NOW, "entities_referenced": {"people": [PERSON_ID]},
        },
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": THREAD_ID},
        {
            "thread_id": THREAD_ID, "normalized_subject": "test subject",
            "message_ids": [MESSAGE_ID], "person_ids": [PERSON_ID], "org_ids": [ORG_ID],
        },
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-TEST-001"},
        {
            "id": "COM-TEST-001", "what": "send the proposal", "class": "mine",
            "owed_by": "Test Person", "owed_to": "Sender", "person_id": PERSON_ID, "org_id": ORG_ID,
            "source_record": MESSAGE_ID, "made_on": _NOW, "committed_date": None, "date_type": None,
            "status": "open", "goal_pillar": "Sales", "project_id": None, "thread_id": THREAD_ID,
        },
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-TEST-001"},
        {
            "id": "MTG-TEST-001", "date": _NOW, "attendees": ["Test Person", "Sender"],
            "person_ids": [PERSON_ID], "org_id": ORG_ID, "project_or_pillar": None,
            "minutes_record": None, "actions_raised": [], "next_meeting_date": None,
            "agenda_target": None, "actionable": True, "agenda_written": False, "thread_id": THREAD_ID,
        },
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-TEST-001"},
        {"id": "FU-TEST-001", "commitment_id": "COM-TEST-001", "thread_id": THREAD_ID, "person_id": PERSON_ID, "org_id": ORG_ID},
    )
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-TEST-001"},
        {
            "id": "PRJ-TEST-001", "project": "Test Rollout", "cluster": None, "entity": "Test Co",
            "goal_pillar": "Sales", "objective": None, "target": None, "status": None, "owner": None,
            "collaborators": [], "person_ids": [PERSON_ID], "org_id": ORG_ID, "next_milestone": None,
            "due": None, "health": None, "last_movement": None, "note_link": None, "source": "gmail",
        },
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "knowledge-test-001"},
        {
            "knowledge_id": "knowledge-test-001", "thread_id": THREAD_ID, "subject_key": "test_person",
            "predicate": "prefers", "fact_key": "morning_meetings", "current_value": "morning meetings",
            "person_id": PERSON_ID, "org_id": ORG_ID, "history": [], "source_emails": [MESSAGE_ID],
            "basis": "stated", "first_seen_at": _NOW, "last_confirmed_at": _NOW,
            "confidence": 0.9, "status": "active",
        },
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply-test-001"},
        {
            "reply_id": "reply-test-001", "thread_id": THREAD_ID, "source_email_id": MESSAGE_ID,
            "status": "awaiting_approval", "draft": {"subject": "Re: Test subject", "body": "..."},
            "person_id": PERSON_ID, "org_id": ORG_ID, "created_by": "sales_agent",
            "created_at": _NOW, "approved_by": None, "sent_at": None,
        },
    )
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": THREAD_ID, "meeting_fingerprint": "test_fp"},
        {
            "thread_id": THREAD_ID, "meeting_fingerprint": "test_fp", "status": "awaiting_approval",
            "event": {
                "title": "Test meeting", "start": _NOW, "end": _NOW, "timezone": "UTC",
                "description": "test", "attendees": [],
            },
            "actor_type": "authenticated_user", "reason": None,
            "person_id": PERSON_ID, "org_id": ORG_ID, "meeting_id": "MTG-TEST-001",
        },
    )
    return db


def test_person_360_context_retrieves_every_entity_type_via_canonical_reference(seeded):
    context = get_person_context(seeded, PERSON_ID)

    assert context["person"]["id"] == PERSON_ID
    assert context["organization"]["data"]["id"] == ORG_ID
    assert [e["message_id"] for e in context["emails"]["data"]] == [MESSAGE_ID]
    assert [t["thread_id"] for t in context["threads"]["data"]] == [THREAD_ID]

    assert len(context["commitments"]) == 1
    assert context["commitments"][0]["id"] == "COM-TEST-001"
    assert context["commitments"][0]["basis"] == "canonical_person_id"

    assert len(context["meetings"]) == 1
    assert context["meetings"][0]["id"] == "MTG-TEST-001"
    assert context["meetings"][0]["basis"] == "canonical_person_id"

    assert len(context["follow_ups"]) == 1
    assert context["follow_ups"][0]["id"] == "FU-TEST-001"
    assert context["follow_ups"][0]["basis"] == "canonical_person_id"

    assert len(context["projects"]) == 1
    assert context["projects"][0]["id"] == "PRJ-TEST-001"
    assert context["projects"][0]["basis"] == "canonical_person_id"

    assert len(context["knowledge"]) == 1
    assert context["knowledge"][0]["knowledge_id"] == "knowledge-test-001"
    assert context["knowledge"][0]["basis"] == "canonical_person_id"

    assert len(context["reply_drafts"]) == 1
    assert context["reply_drafts"][0]["reply_id"] == "reply-test-001"
    assert context["reply_drafts"][0]["basis"] == "canonical_person_id"

    assert len(context["calendar_actions"]) == 1
    assert context["calendar_actions"][0]["meeting_fingerprint"] == "test_fp"
    assert context["calendar_actions"][0]["basis"] == "canonical_person_id"


def test_person_360_context_creates_no_duplicate_underlying_content(seeded):
    counts_before = {
        "people": PersonRepository(seeded).find_many({}).__len__(),
        "organizations": OrganizationRepository(seeded).find_many({}).__len__(),
        "commitments": CommitmentRepository(seeded).find_many({}).__len__(),
        "meetings": MeetingRepository(seeded).find_many({}).__len__(),
        "follow_ups": FollowUpRepository(seeded).find_many({}).__len__(),
        "projects": ProjectRepository(seeded).find_many({}).__len__(),
        "knowledge": KnowledgeRepository(seeded).find_many({}).__len__(),
        "reply_drafts": ReplyDraftRepository(seeded).find_many({}).__len__(),
        "calendar_actions": CalendarActionRepository(seeded).find_many({}).__len__(),
    }

    get_person_context(seeded, PERSON_ID)
    get_person_context(seeded, PERSON_ID)  # calling it twice must still create nothing

    counts_after = {
        "people": PersonRepository(seeded).find_many({}).__len__(),
        "organizations": OrganizationRepository(seeded).find_many({}).__len__(),
        "commitments": CommitmentRepository(seeded).find_many({}).__len__(),
        "meetings": MeetingRepository(seeded).find_many({}).__len__(),
        "follow_ups": FollowUpRepository(seeded).find_many({}).__len__(),
        "projects": ProjectRepository(seeded).find_many({}).__len__(),
        "knowledge": KnowledgeRepository(seeded).find_many({}).__len__(),
        "reply_drafts": ReplyDraftRepository(seeded).find_many({}).__len__(),
        "calendar_actions": CalendarActionRepository(seeded).find_many({}).__len__(),
    }
    assert counts_before == counts_after
    assert counts_before["people"] == 1  # never fragmented into a second Person


def test_person_360_context_preserves_legacy_display_fields_untouched(seeded):
    get_person_context(seeded, PERSON_ID)

    commitment = CommitmentRepository(seeded).find_one({"id": "COM-TEST-001"})
    meeting = MeetingRepository(seeded).find_one({"id": "MTG-TEST-001"})
    project = ProjectRepository(seeded).find_one({"id": "PRJ-TEST-001"})
    person = PersonRepository(seeded).find_one({"id": PERSON_ID})

    assert commitment["owed_by"] == "Test Person"
    assert commitment["owed_to"] == "Sender"
    assert meeting["attendees"] == ["Test Person", "Sender"]
    assert project["collaborators"] == []  # legacy field, never force-populated
    assert person["org"] == "Test Co"


def test_person_360_context_calendar_action_has_no_external_attendees(seeded):
    context = get_person_context(seeded, PERSON_ID)

    action = context["calendar_actions"][0]
    assert action["event"]["attendees"] == []
    # The person/org/meeting metadata is entirely separate from attendees, and does
    # not, and structurally cannot, ever populate it.
    assert action["person_id"] == PERSON_ID
    assert action["org_id"] == ORG_ID
    assert action["meeting_id"] == "MTG-TEST-001"


def test_person_360_context_calendar_event_model_still_rejects_external_attendees():
    # Direct construction-time proof this task changed nothing about the safety rule.
    from pydantic import ValidationError

    from app.calendar.models import CalendarEvent

    with pytest.raises(ValidationError):
        CalendarEvent(
            title="x", start=_NOW, end=_NOW, timezone="UTC", description="x",
            attendees=["someone@example.com"],
        )
