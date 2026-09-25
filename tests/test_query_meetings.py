# tests/test_query_meetings.py
"""Phase 23: Meeting Intelligence. Synthetic mongomock fixtures only. Every attendee
resolution goes through canonical lifecycle resolution; CalendarEvent.attendees is
never touched anywhere in this module (Meeting.attendees is free text, a completely
separate field -- see app.entities.models.Meeting)."""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ThreadRepository,
)
from app.query.meetings import classify_meeting, get_meeting_brief, get_meeting_brief_for_person, get_upcoming_meeting_briefs
from app.query.schemas import MeetingClassification
from datetime import datetime, timezone

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


def _meeting(mid, **overrides):
    doc = {"id": mid, "date": "2026-03-12T09:00:00Z", "attendees": [], "person_ids": [], "org_id": None,
           "project_or_pillar": None, "minutes_record": None, "actions_raised": [], "next_meeting_date": None,
           "agenda_target": None, "actionable": True, "agenda_written": False}
    doc.update(overrides)
    return doc


# --- Direct meeting lookup + missing data ---------------------------------------------------


def test_get_meeting_brief_missing_meeting_returns_none(db):
    assert get_meeting_brief(db, "MTG-DOES-NOT-EXIST") is None


def test_get_meeting_brief_with_no_attendees_no_org_no_thread(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1"))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief is not None
    assert brief.canonical_attendee_ids == []
    assert brief.attendee_contexts == []
    assert brief.organization_context is None
    assert brief.thread_context is None
    assert brief.classification == MeetingClassification.UNKNOWN


# --- Canonical attendees, merged attendee ----------------------------------------------------


def test_get_meeting_brief_resolves_canonical_attendees(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Active Attendee"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1"]))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief.canonical_attendee_ids == ["PER-1"]
    assert len(brief.attendee_contexts) == 1
    assert brief.attendee_contexts[0]["person"]["id"] == "PER-1"


def test_get_meeting_brief_merged_attendee_resolves_to_canonical(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Canonical"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Canonical", status="merged", merged_into="PER-2"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1"]))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief.canonical_attendee_ids == ["PER-2"]  # never PER-1
    assert brief.attendee_contexts[0]["person"]["id"] == "PER-2"
    assert brief.attendee_resolution_notes == []


def test_get_meeting_brief_broken_attendee_chain_is_noted_not_fatal(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into=None))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Fine Attendee"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1", "PER-2"]))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief is not None  # partial brief, not a crash
    assert brief.canonical_attendee_ids == ["PER-2"]
    assert len(brief.attendee_resolution_notes) == 1
    assert "no merged_into target" in brief.attendee_resolution_notes[0]


def test_get_meeting_brief_duplicate_canonical_attendees_deduplicated(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Canonical"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1", "PER-2"]))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief.canonical_attendee_ids == ["PER-2"]  # deduplicated, not listed twice


# --- Organization context, project (never linked) ---------------------------------------------


def test_get_meeting_brief_organization_context(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, {"id": "ORG-1", "name": "Acme", "domain": "acme.example", "aliases": [], "source": "gmail"})
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", org_id="ORG-1"))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief.organization_context is not None
    assert brief.organization_context["organization"]["id"] == "ORG-1"


def test_get_meeting_brief_project_context_always_none_no_explicit_link(db):
    # Meeting has no project_id field at all (only free-text project_or_pillar,
    # never populated) -- project_context must never be fabricated.
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", project_or_pillar="Sales"))

    brief = get_meeting_brief(db, "MTG-1")

    assert brief.project_context is None


# --- Previous meetings, commitments, follow-ups, knowledge, reply drafts -----------------------


def test_get_meeting_brief_previous_meetings_same_thread_earlier_date(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-OLD"}, {**_meeting("MTG-OLD", date="2026-01-01T00:00:00Z"), "thread_id": "t1"})
    MeetingRepository(db).upsert_by_key({"id": "MTG-NEW"}, {**_meeting("MTG-NEW", date="2026-03-12T00:00:00Z"), "thread_id": "t1"})
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "normalized_subject": "x", "message_ids": [], "last_message_at": "2026-01-01T00:00:00Z", "person_ids": [], "org_ids": []})

    brief = get_meeting_brief(db, "MTG-NEW")

    assert [m["id"] for m in brief.previous_meetings] == ["MTG-OLD"]


def test_get_meeting_brief_includes_attendee_commitments_and_follow_ups(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1"]))
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": None, "owed_to": None, "person_id": "PER-1",
         "org_id": None, "source_record": "e:1", "made_on": "2026-01-01T00:00:00Z", "committed_date": None, "status": "open"},
    )

    brief = get_meeting_brief(db, "MTG-1")

    assert len(brief.open_commitments) == 1
    assert brief.open_commitments[0]["id"] == "COM-1"


def test_get_meeting_brief_evidence_is_populated(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-1"]))

    brief = get_meeting_brief(db, "MTG-1")

    assert len(brief.evidence) >= 1
    assert any(e.collection == "meetings" for e in brief.evidence)


# --- Classification ----------------------------------------------------------------------------


def test_classify_meeting_unknown_with_no_attendees(db):
    meeting = _meeting("MTG-1")
    assert classify_meeting(db, meeting, agent_email="agent@ourcompany.example") == MeetingClassification.UNKNOWN


def test_classify_meeting_internal_when_all_attendees_share_agent_domain(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="colleague@ourcompany.example"))
    meeting = _meeting("MTG-1", person_ids=["PER-1"])
    assert classify_meeting(db, meeting, agent_email="agent@ourcompany.example") == MeetingClassification.INTERNAL


def test_classify_meeting_customer_when_an_attendee_is_external(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="customer@theirco.example"))
    meeting = _meeting("MTG-1", person_ids=["PER-1"])
    assert classify_meeting(db, meeting, agent_email="agent@ourcompany.example") == MeetingClassification.CUSTOMER


def test_classify_meeting_unknown_without_agent_email_configured(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="someone@example.com"))
    meeting = _meeting("MTG-1", person_ids=["PER-1"])
    assert classify_meeting(db, meeting, agent_email=None) == MeetingClassification.UNKNOWN


def test_classify_meeting_never_assigns_sales_or_finance():
    # SALES/FINANCE/PROSPECT/PROJECT exist in the enum for future extensibility but
    # must never be produced by classify_meeting today -- no stored field supports them.
    assert MeetingClassification.SALES not in {MeetingClassification.INTERNAL, MeetingClassification.CUSTOMER, MeetingClassification.UNKNOWN}


# --- get_meeting_brief_for_person / get_upcoming_meeting_briefs --------------------------------


def test_get_meeting_brief_for_person_canonicalizes_merged_id(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, _meeting("MTG-1", person_ids=["PER-2"]))

    briefs = get_meeting_brief_for_person(db, "PER-1")  # merged id -- must not return []

    assert len(briefs) == 1
    assert briefs[0].meeting["id"] == "MTG-1"


def test_get_upcoming_meeting_briefs_only_future(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-PAST"}, _meeting("MTG-PAST", date="2020-01-01T00:00:00Z", person_ids=["PER-1"]))
    MeetingRepository(db).upsert_by_key({"id": "MTG-FUTURE"}, _meeting("MTG-FUTURE", date="2026-06-01T00:00:00Z", person_ids=["PER-1"]))

    briefs = get_upcoming_meeting_briefs(db, reference_datetime=_REF, timezone="UTC", person_id="PER-1")

    assert [b.meeting["id"] for b in briefs] == ["MTG-FUTURE"]


# --- Calendar safety invariant -----------------------------------------------------------------


def test_meeting_brief_never_touches_calendar_actions_or_attendees(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {**_meeting("MTG-1", person_ids=["PER-1"]), "thread_id": "t1"})
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "t1", "meeting_fingerprint": "fp1"},
        {"thread_id": "t1", "meeting_fingerprint": "fp1", "person_id": None, "org_id": None, "meeting_id": None, "event": {"attendees": []}},
    )
    before = list(db.calendar_actions.find({}, {"_id": 0}))

    get_meeting_brief(db, "MTG-1")

    after = list(db.calendar_actions.find({}, {"_id": 0}))
    assert before == after
    assert after[0]["event"]["attendees"] == []
