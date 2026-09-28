import inspect

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    MeetingRepository,
    OpportunityRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.mcp import tools


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _email(message_id, thread_id=None, **overrides):
    doc = {
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "from": {"name": "Jane", "email": "jane@614group.com"},
        "to": [{"name": "Ashok", "email": "ashok@databeat.io"}],
        "cc": [],
        "subject": "Renewal check-in",
        "body": "Let's talk renewal terms next week.",
        "timestamp": "2026-09-10T09:00:00Z",
        "labels": [],
    }
    doc.update(overrides)
    return doc


def _thread(thread_id, message_ids, **overrides):
    doc = {
        "thread_id": thread_id,
        "normalized_subject": "renewal check-in",
        "participant_emails": sorted({"jane@614group.com", "ashok@databeat.io"}),
        "message_ids": message_ids,
        "last_message_at": "2026-09-10T09:00:00",
    }
    doc.update(overrides)
    return doc


def _person(person_id, **overrides):
    doc = {
        "id": person_id,
        "name": "Jane Doe",
        "email": f"{person_id.lower()}@614group.com",
        "aliases": [],
        "org": "614 Group",
        "type": None,
        "goal_pillar": None,
        "role_in_pillar": None,
        "tier": None,
        "voice_register": None,
        "last_inbound": None,
        "last_outbound": None,
        "reports_to": None,
        "open_threads": [],
        "note_link": None,
        "review_flag": False,
        "source": "gmail",
    }
    doc.update(overrides)
    return doc


def _project(project_id, **overrides):
    doc = {
        "id": project_id,
        "project": "DataBeat-Speedvision",
        "cluster": None,
        "entity": "Speedvision",
        "goal_pillar": "Sales",
        "objective": None,
        "target": None,
        "status": None,
        "owner": None,
        "collaborators": [],
        "next_milestone": None,
        "due": None,
        "health": None,
        "last_movement": None,
        "note_link": None,
        "source": "gmail",
    }
    doc.update(overrides)
    return doc


def _commitment(commitment_id, thread_id, **overrides):
    doc = {
        "id": commitment_id,
        "what": "Send pricing sheet",
        "class": "mine",
        "importance": None,
        "owed_by": None,
        "owed_to": None,
        "source_record": "msg_001",
        "made_on": "2026-09-10T09:00:00",
        "committed_date": None,
        "date_type": None,
        "status": "open",
        "goal_pillar": None,
        "project_id": None,
        "thread_id": thread_id,
    }
    doc.update(overrides)
    return doc


def _follow_up(follow_up_id, **overrides):
    doc = {"id": follow_up_id, "commitment_id": None, "thread_id": None}
    doc.update(overrides)
    return doc


def _reply_draft(reply_id, **overrides):
    doc = {
        "reply_id": reply_id,
        "thread_id": "t1",
        # source_email_id has a unique index (app.database.indexes) -- default derives
        # from reply_id so multiple drafts built by this helper never collide.
        "source_email_id": reply_id.replace("reply_", "msg_"),
        "status": "awaiting_approval",
        "draft": {"subject": "Re: Renewal check-in", "body": "Thanks for the note."},
        "created_by": "sales_agent",
        "created_at": "2026-09-10T09:05:00+00:00",
        "approved_by": None,
        "sent_at": None,
    }
    doc.update(overrides)
    return doc


def _meeting(meeting_id, thread_id, **overrides):
    doc = {
        "id": meeting_id,
        "date": None,
        "attendees": [],
        "project_or_pillar": None,
        "minutes_record": None,
        "actions_raised": [],
        "next_meeting_date": None,
        "agenda_target": None,
        "actionable": False,
        "agenda_written": False,
        "thread_id": thread_id,
    }
    doc.update(overrides)
    return doc


# --- search_emails ---


def test_search_emails_filters_by_sender(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1"))
    EmailRepository(db).upsert_by_key(
        {"message_id": "m2"}, _email("m2", **{"from": {"name": "Bob", "email": "bob@other.com"}})
    )

    results = tools.search_emails(db, from_email="jane@614group.com")

    assert [r["message_id"] for r in results] == ["m1"]
    assert results[0]["body"] == "Let's talk renewal terms next week."


def test_search_emails_filters_by_subject_substring_case_insensitive(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", subject="Q3 Renewal Terms"))
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2", subject="Unrelated topic"))

    results = tools.search_emails(db, subject_contains="renewal")

    assert [r["message_id"] for r in results] == ["m1"]


def test_search_emails_filters_by_label_applied(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"}, _email("m1", label_applied="Needs reply: ASAP")
    )
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2", label_applied="Read only"))

    results = tools.search_emails(db, label_applied="Needs reply: ASAP")

    assert [r["message_id"] for r in results] == ["m1"]


def test_search_emails_filters_by_priority(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", priority="P1"))
    EmailRepository(db).upsert_by_key({"message_id": "m2"}, _email("m2", priority="P2"))

    results = tools.search_emails(db, priority="P1")

    assert [r["message_id"] for r in results] == ["m1"]
    assert results[0]["priority"] == "P1"


def test_search_emails_combines_filters(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"}, _email("m1", subject="Renewal", label_applied="Read only")
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "m2"},
        _email("m2", subject="Renewal", label_applied="Needs reply: ASAP"),
    )

    results = tools.search_emails(db, subject_contains="renewal", label_applied="Read only")

    assert [r["message_id"] for r in results] == ["m1"]


def test_search_emails_respects_limit(db):
    for i in range(5):
        EmailRepository(db).upsert_by_key(
            {"message_id": f"m{i}"}, _email(f"m{i}", timestamp=f"2026-09-{10 + i:02d}T09:00:00Z")
        )

    results = tools.search_emails(db, limit=2)

    assert len(results) == 2
    assert [r["message_id"] for r in results] == ["m4", "m3"]  # most recent first


def test_search_emails_returns_empty_list_for_no_matches(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1"))

    results = tools.search_emails(db, from_email="nobody@nowhere.com")

    assert results == []


# --- get_thread ---


def test_get_thread_returns_existing_thread_with_messages_ordered(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"}, _email("m1", thread_id="t1", timestamp="2026-09-10T09:00:00Z")
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "m2"}, _email("m2", thread_id="t1", timestamp="2026-09-11T09:00:00Z")
    )
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, _thread("t1", ["m1", "m2"]))

    result = tools.get_thread(db, "t1")

    assert result["thread_id"] == "t1"
    assert result["message_count"] == 2
    assert [m["message_id"] for m in result["messages"]] == ["m1", "m2"]
    assert result["messages"][0]["body"] == "Let's talk renewal terms next week."


def test_get_thread_includes_latest_context_snapshot_when_present(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", thread_id="t1"))
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, _thread("t1", ["m1"]))
    ContextSnapshotRepository(db).upsert_by_key(
        {"thread_id": "t1", "context_version": 1},
        {
            "thread_id": "t1",
            "context_version": 1,
            "triggering_email_id": "m1",
            "context": {"summary": "Renewal in progress"},
        },
    )

    result = tools.get_thread(db, "t1")

    assert result["latest_context"] == {"summary": "Renewal in progress"}
    assert result["context_version"] == 1


def test_get_thread_returns_none_context_when_no_snapshot_exists(db):
    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1", thread_id="t1"))
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, _thread("t1", ["m1"]))

    result = tools.get_thread(db, "t1")

    assert result["latest_context"] is None
    assert result["context_version"] is None


def test_get_thread_returns_none_for_nonexistent_thread(db):
    result = tools.get_thread(db, "does-not-exist")

    assert result is None


# --- list_people ---


def test_list_people_with_no_filters_returns_all(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person("PER-001"))
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002", org="Other Co"))

    results = tools.list_people(db)

    assert {p["id"] for p in results} == {"PER-001", "PER-002"}


def test_list_people_filters_by_org(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person("PER-001", org="614 Group"))
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002", org="Other Co"))

    results = tools.list_people(db, org="614 Group")

    assert [p["id"] for p in results] == ["PER-001"]


def test_list_people_filters_by_email(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"}, _person("PER-001", email="jane@614group.com")
    )
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002"))

    results = tools.list_people(db, email="jane@614group.com")

    assert [p["id"] for p in results] == ["PER-001"]


def test_list_people_filters_by_thread_id(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"}, _person("PER-001", open_threads=["t1", "t2"])
    )
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002", open_threads=["t3"]))

    results = tools.list_people(db, thread_id="t1")

    assert [p["id"] for p in results] == ["PER-001"]


# --- list_projects ---


def test_list_projects_filters_by_entity(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001", entity="Speedvision"))
    ProjectRepository(db).upsert_by_key({"id": "PRJ-002"}, _project("PRJ-002", entity="Conde Nast"))

    results = tools.list_projects(db, entity="Speedvision")

    assert [p["id"] for p in results] == ["PRJ-001"]


def test_list_projects_filters_by_goal_pillar(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001", goal_pillar="Sales"))
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-002"}, _project("PRJ-002", goal_pillar="Marketing")
    )

    results = tools.list_projects(db, goal_pillar="Marketing")

    assert [p["id"] for p in results] == ["PRJ-002"]


# --- list_opportunities / update_opportunity_fields ---


def _opportunity(opportunity_id, **overrides):
    doc = {
        "id": opportunity_id,
        "name": "Acme Renewal",
        "entity": "Acme",
        "org_id": None,
        "description": None,
        "status": "open",
        "stage": None,
        "owner": None,
        "value": None,
        "currency": None,
        "expected_close_date": None,
        "next_action": None,
        "source_email_ids": [],
        "project_ids": ["PRJ-001"],
        "meeting_ids": [],
        "person_ids": [],
        "buying_signals": [],
        "last_activity_at": "2026-09-13T10:30:00Z",
        "created_at": "2026-09-13T10:30:00Z",
        "updated_at": "2026-09-13T10:30:00Z",
    }
    doc.update(overrides)
    return doc


def test_list_opportunities_filters_by_org_id(db):
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, _opportunity("OPP-001", org_id="ORG-1"))
    OpportunityRepository(db).upsert_by_key({"id": "OPP-002"}, _opportunity("OPP-002", org_id="ORG-2"))

    results = tools.list_opportunities(db, org_id="ORG-1")

    assert [o["id"] for o in results] == ["OPP-001"]


def test_list_opportunities_filters_by_status(db):
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, _opportunity("OPP-001", status="open"))
    OpportunityRepository(db).upsert_by_key({"id": "OPP-002"}, _opportunity("OPP-002", status="won"))

    results = tools.list_opportunities(db, status="won")

    assert [o["id"] for o in results] == ["OPP-002"]


def test_list_opportunities_filters_by_project_id_membership(db):
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, _opportunity("OPP-001", project_ids=["PRJ-001", "PRJ-002"]))
    OpportunityRepository(db).upsert_by_key({"id": "OPP-002"}, _opportunity("OPP-002", project_ids=["PRJ-003"]))

    results = tools.list_opportunities(db, project_id="PRJ-002")

    assert [o["id"] for o in results] == ["OPP-001"]


def test_update_opportunity_fields_sets_only_the_manual_crm_fields_supplied(db):
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, _opportunity("OPP-001"))

    result = tools.update_opportunity_fields(
        db, "OPP-001", stage="negotiation", value=50000.0, currency="USD"
    )

    assert result["stage"] == "negotiation"
    assert result["value"] == 50000.0
    assert result["currency"] == "USD"
    # Untouched fields stay exactly as they were.
    assert result["owner"] is None
    assert result["next_action"] is None
    assert result["expected_close_date"] is None
    # Pipeline-derived fields are never touched by this tool.
    assert result["project_ids"] == ["PRJ-001"]


def test_update_opportunity_fields_does_not_reset_an_already_set_field_when_omitted(db):
    OpportunityRepository(db).upsert_by_key({"id": "OPP-001"}, _opportunity("OPP-001", stage="prospecting"))

    result = tools.update_opportunity_fields(db, "OPP-001", owner="Sandeep")

    assert result["stage"] == "prospecting"  # untouched, not reset to null
    assert result["owner"] == "Sandeep"


def test_update_opportunity_fields_raises_for_an_unknown_opportunity_id(db):
    with pytest.raises(ValueError, match="no opportunity found"):
        tools.update_opportunity_fields(db, "OPP-DOES-NOT-EXIST", stage="negotiation")


# --- list_commitments ---


def test_list_commitments_filters_by_thread_id(db):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, _commitment("COM-001", thread_id="t1")
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-002"}, _commitment("COM-002", thread_id="t2")
    )

    results = tools.list_commitments(db, thread_id="t1")

    assert [c["id"] for c in results] == ["COM-001"]


def test_list_commitments_filters_by_class_using_stored_class_key(db):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, _commitment("COM-001", "t1", **{"class": "mine"})
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-002"}, _commitment("COM-002", "t1", **{"class": "owed_to_me"})
    )

    results = tools.list_commitments(db, class_="owed_to_me")

    assert [c["id"] for c in results] == ["COM-002"]


def test_list_commitments_filters_by_project_id(db):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, _commitment("COM-001", "t1", project_id="PRJ-001")
    )
    CommitmentRepository(db).upsert_by_key({"id": "COM-002"}, _commitment("COM-002", "t1"))

    results = tools.list_commitments(db, project_id="PRJ-001")

    assert [c["id"] for c in results] == ["COM-001"]


# --- list_follow_ups ---


def test_list_follow_ups_filters_by_commitment_id(db):
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-001"}, _follow_up("FU-001", commitment_id="COM-001")
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-002"}, _follow_up("FU-002", thread_id="t1")
    )

    results = tools.list_follow_ups(db, commitment_id="COM-001")

    assert [f["id"] for f in results] == ["FU-001"]


def test_list_follow_ups_filters_by_thread_id(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-001"}, _follow_up("FU-001", thread_id="t1"))
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-002"}, _follow_up("FU-002", commitment_id="COM-001")
    )

    results = tools.list_follow_ups(db, thread_id="t1")

    assert [f["id"] for f in results] == ["FU-001"]


def test_list_follow_ups_never_fabricates_a_status_field(db):
    FollowUpRepository(db).upsert_by_key({"id": "FU-001"}, _follow_up("FU-001", thread_id="t1"))

    results = tools.list_follow_ups(db)

    assert "status" not in results[0]


def test_list_follow_ups_filters_by_escalation_level(db):
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-L1"}, _follow_up("FU-L1", thread_id="t1", escalation_level=1)
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-L2"}, _follow_up("FU-L2", thread_id="t2", escalation_level=2)
    )

    results = tools.list_follow_ups(db, escalation_level=2)

    assert [f["id"] for f in results] == ["FU-L2"]


def test_list_follow_ups_filters_by_audience(db):
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-INT"}, _follow_up("FU-INT", thread_id="t1", audience="internal")
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-CLI"}, _follow_up("FU-CLI", thread_id="t2", audience="client_fixed_date")
    )

    results = tools.list_follow_ups(db, audience="internal")

    assert [f["id"] for f in results] == ["FU-INT"]


def test_list_follow_ups_filters_by_status(db):
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-ACT"}, _follow_up("FU-ACT", thread_id="t1", status="active")
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-RES"}, _follow_up("FU-RES", thread_id="t2", status="resolved")
    )

    results = tools.list_follow_ups(db, status="resolved")

    assert [f["id"] for f in results] == ["FU-RES"]


# --- list_meetings ---


def test_list_meetings_filters_by_thread_id(db):
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting("MTG-001", "t1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-002"}, _meeting("MTG-002", "t2"))

    results = tools.list_meetings(db, thread_id="t1")

    assert [m["id"] for m in results] == ["MTG-001"]


def test_list_meetings_filters_by_actionable(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"}, _meeting("MTG-001", "t1", actionable=True)
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-002"}, _meeting("MTG-002", "t2", actionable=False)
    )

    results = tools.list_meetings(db, actionable=True)

    assert [m["id"] for m in results] == ["MTG-001"]


# --- list_meetings: category filter (BRD gap-analysis FR-04 integration gap) --------


_AGENT_EMAIL = "agent@ourcompany.example"


def test_list_meetings_no_category_returns_every_meeting_unchanged(db):
    # Backward compatibility: mixed classifications present, no category filter
    # requested -- every meeting still comes back, exactly like before this change.
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _meeting("MTG-SALES", "t1", project_or_pillar="Sales")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-FINANCE"}, _meeting("MTG-FINANCE", "t2", project_or_pillar="Finance")
    )
    MeetingRepository(db).upsert_by_key({"id": "MTG-UNKNOWN"}, _meeting("MTG-UNKNOWN", "t3"))

    results = tools.list_meetings(db)

    assert {m["id"] for m in results} == {"MTG-SALES", "MTG-FINANCE", "MTG-UNKNOWN"}


def test_list_meetings_category_sales_returns_only_sales_meetings(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _meeting("MTG-SALES", "t1", project_or_pillar="Sales")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-FINANCE"}, _meeting("MTG-FINANCE", "t2", project_or_pillar="Finance")
    )

    results = tools.list_meetings(db, category="SALES")

    assert [m["id"] for m in results] == ["MTG-SALES"]


def test_list_meetings_category_finance_returns_only_finance_meetings(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _meeting("MTG-SALES", "t1", project_or_pillar="Sales")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-FINANCE"}, _meeting("MTG-FINANCE", "t2", project_or_pillar="Finance")
    )

    results = tools.list_meetings(db, category="finance")

    assert [m["id"] for m in results] == ["MTG-FINANCE"]


def test_list_meetings_category_internal_returns_only_internal_meetings(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-COLLEAGUE"}, _person("PER-COLLEAGUE", email="colleague@ourcompany.example")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-CUSTOMER"}, _person("PER-CUSTOMER", email="buyer@theirco.example")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-INTERNAL"}, _meeting("MTG-INTERNAL", "t1", person_ids=["PER-COLLEAGUE"])
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-CUSTOMER"}, _meeting("MTG-CUSTOMER", "t2", person_ids=["PER-CUSTOMER"])
    )

    results = tools.list_meetings(db, category="internal", agent_email=_AGENT_EMAIL)

    assert [m["id"] for m in results] == ["MTG-INTERNAL"]


def test_list_meetings_category_customer_returns_only_customer_meetings(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-COLLEAGUE"}, _person("PER-COLLEAGUE", email="colleague@ourcompany.example")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-CUSTOMER"}, _person("PER-CUSTOMER", email="buyer@theirco.example")
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-INTERNAL"}, _meeting("MTG-INTERNAL", "t1", person_ids=["PER-COLLEAGUE"])
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-CUSTOMER"}, _meeting("MTG-CUSTOMER", "t2", person_ids=["PER-CUSTOMER"])
    )

    results = tools.list_meetings(db, category="customer", agent_email=_AGENT_EMAIL)

    assert [m["id"] for m in results] == ["MTG-CUSTOMER"]


def test_list_meetings_category_unknown_returns_only_unclassifiable_meetings(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _meeting("MTG-SALES", "t1", project_or_pillar="Sales")
    )
    MeetingRepository(db).upsert_by_key({"id": "MTG-NO-ATTENDEES"}, _meeting("MTG-NO-ATTENDEES", "t2"))

    results = tools.list_meetings(db, category="unknown", agent_email=_AGENT_EMAIL)

    assert [m["id"] for m in results] == ["MTG-NO-ATTENDEES"]


@pytest.mark.parametrize("bad_category", ["prospect", "project", "bogus", ""])
def test_list_meetings_rejects_unsupported_categories_deterministically(db, bad_category):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES"}, _meeting("MTG-SALES", "t1", project_or_pillar="Sales")
    )

    with pytest.raises(ValueError, match="category must be one of"):
        tools.list_meetings(db, category=bad_category)


def test_list_meetings_category_combined_with_date_range(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES-TODAY"},
        _meeting("MTG-SALES-TODAY", "t1", project_or_pillar="Sales", date="2026-09-27T10:00:00Z"),
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES-NEXT-WEEK"},
        _meeting("MTG-SALES-NEXT-WEEK", "t2", project_or_pillar="Sales", date="2026-10-04T10:00:00Z"),
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-FINANCE-TODAY"},
        _meeting("MTG-FINANCE-TODAY", "t3", project_or_pillar="Finance", date="2026-09-27T11:00:00Z"),
    )

    results = tools.list_meetings(
        db, category="sales", start_date="2026-09-27T00:00:00Z", end_date="2026-09-28T00:00:00Z"
    )

    assert [m["id"] for m in results] == ["MTG-SALES-TODAY"]


def test_list_meetings_category_and_date_range_with_no_matches_returns_empty_not_an_error(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-SALES-TODAY"},
        _meeting("MTG-SALES-TODAY", "t1", project_or_pillar="Sales", date="2026-09-27T10:00:00Z"),
    )

    results = tools.list_meetings(
        db, category="finance", start_date="2026-09-27T00:00:00Z", end_date="2026-09-28T00:00:00Z"
    )

    assert results == []


def test_list_meetings_date_range_excludes_undated_meetings(db):
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-DATED"},
        _meeting("MTG-DATED", "t1", project_or_pillar="Sales", date="2026-09-27T10:00:00Z"),
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-UNDATED"}, _meeting("MTG-UNDATED", "t2", project_or_pillar="Sales", date=None)
    )

    results = tools.list_meetings(
        db, category="sales", start_date="2026-09-27T00:00:00Z", end_date="2026-09-28T00:00:00Z"
    )

    assert [m["id"] for m in results] == ["MTG-DATED"]


@pytest.mark.parametrize("bad_field", ["start_date", "end_date"])
def test_list_meetings_rejects_unparseable_date_bounds(db, bad_field):
    with pytest.raises(ValueError, match="not a valid ISO 8601"):
        tools.list_meetings(db, **{bad_field: "not-a-date"})


# --- get_project_summary ---


def test_get_project_summary_returns_project_and_related_commitments_and_follow_ups(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001"))
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, _commitment("COM-001", "t1", project_id="PRJ-001")
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-001"}, _follow_up("FU-001", commitment_id="COM-001")
    )
    # Unrelated commitment/follow-up that must NOT show up in the summary.
    CommitmentRepository(db).upsert_by_key({"id": "COM-002"}, _commitment("COM-002", "t2"))
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-002"}, _follow_up("FU-002", commitment_id="COM-002")
    )

    result = tools.get_project_summary(db, "PRJ-001")

    assert result["project"]["id"] == "PRJ-001"
    assert [c["id"] for c in result["related_commitments"]] == ["COM-001"]
    assert [f["id"] for f in result["related_follow_ups"]] == ["FU-001"]


def test_get_project_summary_returns_empty_meetings_and_knowledge_never_fabricated(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001"))

    result = tools.get_project_summary(db, "PRJ-001")

    assert result["related_meetings"] == []
    assert result["related_knowledge_items"] == []
    assert result["related_people"] == []
    assert "NOT SUPPORTED" in result["relationship_notes"]["related_meetings"]


def test_get_project_summary_returns_none_for_nonexistent_project(db):
    result = tools.get_project_summary(db, "PRJ-999")

    assert result is None


# --- get_company_summary ---


def test_get_company_summary_returns_people_and_projects_for_org(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person("PER-001", org="Speedvision"))
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002", org="Other Co"))
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001", entity="Speedvision"))
    ProjectRepository(db).upsert_by_key({"id": "PRJ-002"}, _project("PRJ-002", entity="Other Co"))

    result = tools.get_company_summary(db, "Speedvision")

    assert [p["id"] for p in result["people"]] == ["PER-001"]
    assert [p["id"] for p in result["projects"]] == ["PRJ-001"]


def test_get_company_summary_finds_commitments_indirectly_through_matching_projects(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001", entity="Speedvision"))
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"}, _commitment("COM-001", "t1", project_id="PRJ-001")
    )

    result = tools.get_company_summary(db, "Speedvision")

    assert [c["id"] for c in result["related_commitments"]] == ["COM-001"]


def test_get_company_summary_returns_empty_lists_for_unknown_org_never_erroring(db):
    result = tools.get_company_summary(db, "Nobody Inc")

    assert result["people"] == []
    assert result["projects"] == []
    assert result["related_commitments"] == []
    assert result["related_meetings"] == []
    assert result["related_knowledge_items"] == []


# --- list_reply_drafts / get_reply_draft ---


def test_list_reply_drafts_filters_by_thread_id(db):
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_001"}, _reply_draft("reply_001", thread_id="t1")
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_002"}, _reply_draft("reply_002", thread_id="t2")
    )

    results = tools.list_reply_drafts(db, thread_id="t1")

    assert [d["reply_id"] for d in results] == ["reply_001"]


def test_list_reply_drafts_filters_by_source_email_id(db):
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_001"}, _reply_draft("reply_001", source_email_id="msg_001")
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_002"}, _reply_draft("reply_002", source_email_id="msg_002")
    )

    results = tools.list_reply_drafts(db, source_email_id="msg_002")

    assert [d["reply_id"] for d in results] == ["reply_002"]


def test_list_reply_drafts_filters_by_status(db):
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_001"}, _reply_draft("reply_001", status="awaiting_approval")
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"reply_id": "reply_002"}, _reply_draft("reply_002", status="approved")
    )

    results = tools.list_reply_drafts(db, status="approved")

    assert [d["reply_id"] for d in results] == ["reply_002"]


def test_list_reply_drafts_exposes_provenance_fields(db):
    ReplyDraftRepository(db).upsert_by_key({"reply_id": "reply_001"}, _reply_draft("reply_001"))

    result = tools.list_reply_drafts(db)[0]

    assert result["reply_id"] == "reply_001"
    assert result["source_email_id"] == "msg_001"
    assert result["thread_id"] == "t1"
    assert result["draft"]["body"] == "Thanks for the note."
    assert result["status"] == "awaiting_approval"
    assert result["created_at"] == "2026-09-10T09:05:00+00:00"


def test_list_reply_drafts_never_fabricates_created_at_for_legacy_records(db):
    # A draft written before the created_at field existed has no such key at all in
    # Mongo -- never invented here, exactly like _safe_processing_status's handling of
    # a legacy email with no processing_status.
    legacy = _reply_draft("reply_001")
    del legacy["created_at"]
    ReplyDraftRepository(db).upsert_by_key({"reply_id": "reply_001"}, legacy)

    result = tools.list_reply_drafts(db)[0]

    assert "created_at" not in result


def test_get_reply_draft_returns_one_draft_by_reply_id(db):
    ReplyDraftRepository(db).upsert_by_key({"reply_id": "reply_001"}, _reply_draft("reply_001"))
    ReplyDraftRepository(db).upsert_by_key({"reply_id": "reply_002"}, _reply_draft("reply_002"))

    result = tools.get_reply_draft(db, "reply_001")

    assert result["reply_id"] == "reply_001"


def test_get_reply_draft_returns_none_for_nonexistent_draft(db):
    result = tools.get_reply_draft(db, "reply_999")

    assert result is None


# --- Cross-cutting guarantees ---


def test_no_query_tool_source_references_an_llm_provider_or_api():
    source = inspect.getsource(tools)
    forbidden = ["anthropic", "openai", "ClaudeProvider", "OpenAIProvider"]
    # The module legitimately imports LLMProvider (a Protocol/interface) and
    # run_pipeline for process_email -- neither calls a real API by existing.
    for term in forbidden:
        assert term not in source, f"unexpected LLM/API reference: {term}"


@pytest.mark.parametrize(
    "call",
    [
        lambda db: tools.search_emails(db),
        lambda db: tools.list_people(db),
        lambda db: tools.list_projects(db),
        lambda db: tools.list_commitments(db),
        lambda db: tools.list_follow_ups(db),
        lambda db: tools.list_meetings(db),
        lambda db: tools.list_reply_drafts(db),
        lambda db: tools.get_reply_draft(db, "nope"),
        lambda db: tools.get_thread(db, "nope"),
        lambda db: tools.get_project_summary(db, "PRJ-999"),
        lambda db: tools.get_company_summary(db, "Nobody"),
    ],
)
def test_query_tools_never_write_to_mongodb(db, call):
    before = {name: list(db[name].find({})) for name in db.list_collection_names()}

    call(db)

    after = {name: list(db[name].find({})) for name in db.list_collection_names()}
    assert before == after


def test_search_emails_respects_max_query_limit_cap(db):
    for i in range(5):
        EmailRepository(db).upsert_by_key(
            {"message_id": f"m{i}"}, _email(f"m{i}", timestamp=f"2026-09-{10 + i:02d}T09:00:00Z")
        )

    results = tools.search_emails(db, limit=1_000_000)

    assert len(results) == 5  # never crashes or misbehaves on an absurd limit


def test_search_emails_results_are_json_serializable(db):
    import json

    EmailRepository(db).upsert_by_key({"message_id": "m1"}, _email("m1"))

    results = tools.search_emails(db)

    json.dumps(results)  # raises if anything non-JSON-serializable (ObjectId, datetime) leaked through


# --- ask_question / whats_on_my_table (BRD gap-analysis FR-01 / FR-02) -----------------------


@pytest.fixture
def settings():
    from app.config.settings import Settings

    return Settings(
        email_provider="mock", calendar_provider="mock", llm_provider="mock",
        agent_email="agent@ourcompany.example", timezone="UTC",
    )


def test_ask_question_returns_structured_query_engine_result(db, settings):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "send pricing", "class": "owed_to_me", "person_id": None, "org_id": None,
         "source_record": "e:1", "made_on": "2026-03-01T00:00:00Z", "committed_date": None, "status": "open"},
    )

    result = tools.ask_question(db, settings, "What commitments are due?")

    assert result["status"] == "ok"
    assert result["intent"] == "commitments"
    assert any(r["id"] == "COM-1" for r in result["records"])


def test_ask_question_unrecognized_text_returns_error_status_not_a_guess(db, settings):
    result = tools.ask_question(db, settings, "xyzzy plugh completely nonsensical gibberish")

    assert result["status"] == "error"
    assert result["intent"] == "unsupported"


def test_ask_question_never_makes_an_llm_call(db, settings, monkeypatch):
    # No llm_classify is ever passed through -- verified by making ANY attempt to
    # construct a real LLM provider raise, then confirming ask_question still works.
    import app.providers.factory as factory_module

    def _boom(*args, **kwargs):
        raise AssertionError("ask_question must never construct an LLM provider")

    monkeypatch.setattr(factory_module.ProviderFactory, "create_llm_provider", _boom)

    result = tools.ask_question(db, settings, "What meetings do I have today?")

    assert result["intent"] == "meetings"


def test_ask_question_results_are_json_serializable(db, settings):
    import json

    result = tools.ask_question(db, settings, "What are my P1 emails?")

    json.dumps(result)


def test_whats_on_my_table_includes_every_category_key_even_when_empty(db, settings):
    result = tools.whats_on_my_table(db, settings)

    assert set(result.keys()) == {
        "p1_emails", "pending_replies", "overdue_follow_ups",
        "commitments_due", "upcoming_meetings", "active_projects",
    }
    for value in result.values():
        assert value == []


def test_whats_on_my_table_aggregates_real_data_per_category(db, settings):
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"}, _email("m1", priority="P1", label_applied="Needs reply: ASAP")
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "m1"},
        {"reply_id": "reply_m1", "thread_id": "m1", "source_email_id": "m1", "status": "awaiting_approval",
         "draft": {"subject": "Re: Renewal check-in", "body": "..."}},
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-1"},
        {"id": "FU-1", "commitment_id": None, "thread_id": "m1", "person_id": None, "org_id": None,
         "escalation_level": 1, "surfaced": False, "status": "active", "audience": "client_fixed_date",
         "follow_up_earliest_at": "2020-01-01T00:00:00Z", "follow_up_latest_at": "2020-01-02T00:00:00Z"},
    )
    ProjectRepository(db).upsert_by_key({"id": "PRJ-1"}, {"id": "PRJ-1", "project": "Renewal Q4"})

    result = tools.whats_on_my_table(db, settings)

    assert [e["message_id"] for e in result["p1_emails"]] == ["m1"]
    assert [d["reply_id"] for d in result["pending_replies"]] == ["reply_m1"]
    assert [f["id"] for f in result["overdue_follow_ups"]] == ["FU-1"]
    assert [p["id"] for p in result["active_projects"]] == ["PRJ-1"]


def test_whats_on_my_table_is_json_serializable(db, settings):
    import json

    result = tools.whats_on_my_table(db, settings)

    json.dumps(result)
