# tests/test_entity_context.py
from datetime import datetime, timezone

import mongomock
import pytest

from app.analysis.schemas import (
    EmailAnalysis,
    MentionedPerson,
    MentionedProject,
    RawCommitment,
    RawMeeting,
)
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
from app.entities.context import get_organization_context, get_person_context
from app.entities.resolution import match_resolved_person_by_name, resolve_person
from app.pipeline import _link_knowledge_to_entities, _link_thread_to_entities, _process_entities
from app.replies.models import ReplyDraft, ReplyDraftContent

_NOW = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _email(**overrides):
    from app.email.models import Email

    base = {
        "message_id": "msg_1",
        "thread_id": "thread_1",
        "from": {"name": "Ashok Ganapam", "email": "ashok@databeat.io"},
        "to": [{"name": None, "email": "sandeep@example.com"}],
        "cc": [],
        "subject": "Follow-up",
        "body": "Let's discuss the DataBeat rollout.",
        "timestamp": _NOW,
    }
    base.update(overrides)
    return Email.model_validate(base)


def _analysis(**overrides) -> EmailAnalysis:
    base = dict(
        email_id="msg_1", summary="", intent="evaluation", goal_pillar="Sales",
        label_applied="Undecided", confidence=0.8,
    )
    base.update(overrides)
    return EmailAnalysis.model_validate(base)


# --- Basic context assembly (canonical email/thread/org/related-people links) -------


def test_get_person_context_returns_none_for_unknown_person(db):
    assert get_person_context(db, "PER-999") is None


def test_get_person_context_assembles_canonical_email_and_thread_links(db):
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="thread_1",
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "thread_1"},
        {"thread_id": "thread_1", "normalized_subject": "hello", "message_ids": ["msg_1"]},
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_1"},
        {
            "message_id": "msg_1", "thread_id": "thread_1", "subject": "hello",
            "timestamp": _NOW.isoformat(), "entities_referenced": {"people": [person_id]},
        },
    )

    context = get_person_context(db, person_id)

    assert context["person"]["id"] == person_id
    assert len(context["emails"]["data"]) == 1
    assert context["emails"]["data"][0]["message_id"] == "msg_1"
    assert len(context["threads"]["data"]) == 1
    assert PersonRepository(db).find_many({}).__len__() == 1


def test_get_person_context_includes_organization_and_other_people_at_org(db):
    ashok_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t1",
    )
    colleague_id = resolve_person(
        db, {"name": "Jane Colleague", "email": "jane@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t2",
    )

    context = get_person_context(db, ashok_id)

    assert context["organization"]["data"]["domain"] == "databeat.io"
    other_ids = {p["id"] for p in context["other_people_at_org"]["data"]}
    assert colleague_id in other_ids
    assert ashok_id not in other_ids


def test_thread_document_gets_canonical_person_ids_and_org_ids(db):
    # Forward-path verification (Phase 1): Thread -> person_ids / org_ids, populated by
    # _link_thread_to_entities from the thread's own already-resolved Person records --
    # not asserted anywhere else in this file.
    email = _email()
    entities = _process_entities(db, "thread_1", email, _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
    ), _NOW, "sandeep@example.com")
    ashok_id = entities["people"][0]
    ashok_org_id = PersonRepository(db).find_one({"id": ashok_id})["org_id"]
    # The email's "to" (settings.agent_email in this call) resolves to the operator's
    # own dedicated profile, which is also linked onto this thread.
    operator_id = PersonRepository(db).find_one({"type": "operator"})["id"]

    _link_thread_to_entities(db, "thread_1")

    thread = ThreadRepository(db).find_one({"thread_id": "thread_1"})
    assert thread["person_ids"] == sorted([ashok_id, operator_id])
    assert thread["org_ids"] == [ashok_org_id]


def test_calendar_action_can_reference_a_canonical_meeting_id(db):
    # Forward-path verification (Phase 1): CalendarAction -> meeting_id, not asserted
    # anywhere else in this file (the existing calendar test only checks person_id/
    # org_id and the attendees safety invariant).
    email = _email(body="Let's meet Tuesday at 3pm to discuss the rollout.")
    _run_pipeline_for(db, email)

    action = CalendarActionRepository(db).find_one({"thread_id": "thread_1"})
    assert action is not None
    # detect_meeting's calendar-invite detection is independent of meetings_mentioned
    # (which this MockLLMProvider body doesn't trigger here) -- meeting_id is simply
    # None when no meeting entity was resolved for this email, never fabricated.
    assert "meeting_id" in action


# --- Part 14, tests 1-4 & 13: canonical linking through _process_entities -----------


def test_commitment_can_point_to_the_canonical_person(db):
    # TEST 1
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        commitments_mentioned=[
            RawCommitment.model_validate(
                {"what": "send pricing", "class": "mine", "owed_by": None, "owed_to": "Ashok"}
            )
        ],
    )

    entities = _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")

    ashok_id = entities["people"][0]
    commitment = CommitmentRepository(db).find_one({"id": entities["commitments"][0]})
    assert commitment["person_id"] == ashok_id
    assert commitment["org_id"] is not None


def test_meeting_can_point_to_the_canonical_person(db):
    # TEST 2
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        meetings_mentioned=[
            RawMeeting.model_validate({"attendees": ["Ashok"], "is_past": False, "actions_raised": []})
        ],
    )

    entities = _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")

    ashok_id = entities["people"][0]
    meeting = MeetingRepository(db).find_one({"id": entities["meetings"][0]})
    assert ashok_id in meeting["person_ids"]
    assert meeting["org_id"] is not None


def test_project_can_point_to_the_canonical_person(db):
    # TEST 3
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        projects_mentioned=[MentionedProject(name="Rollout", org="DataBeat")],
    )

    entities = _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")

    ashok_id = entities["people"][0]
    project = ProjectRepository(db).find_one({"id": entities["projects"][0]})
    assert ashok_id in project["person_ids"]
    assert project["org_id"] is not None
    # collaborators (free-text, legacy) is untouched -- never populated by this path.
    assert project["collaborators"] == []


def test_follow_up_inherits_canonical_person_from_its_commitment(db):
    # TEST 4
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        commitments_mentioned=[
            RawCommitment.model_validate(
                {"what": "send pricing", "class": "mine", "owed_by": None, "owed_to": "Ashok"}
            )
        ],
    )

    entities = _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")

    ashok_id = entities["people"][0]
    commitment = CommitmentRepository(db).find_one({"id": entities["commitments"][0]})
    follow_up = FollowUpRepository(db).find_one({"id": entities["follow_ups"][0]})
    assert follow_up["person_id"] == commitment["person_id"] == ashok_id
    assert follow_up["org_id"] == commitment["org_id"]


def test_knowledge_item_can_point_to_the_canonical_person_when_appropriate(db):
    # TEST 5: "Ashok prefers morning meetings" -> person_id.
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        facts=[{"subject": "Ashok", "predicate": "prefers", "object": "morning meetings"}],
    )
    _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k1"},
        {
            "knowledge_id": "k1", "thread_id": "thread_1", "subject_key": "ashok", "predicate": "prefers",
            "fact_key": "morning_meetings", "current_value": "morning meetings", "history": [],
            "source_emails": ["msg_1"], "basis": "stated", "first_seen_at": _NOW.isoformat(),
            "last_confirmed_at": _NOW.isoformat(), "confidence": 0.75, "status": "active",
        },
    )

    _link_knowledge_to_entities(db, "thread_1", "msg_1")

    item = KnowledgeRepository(db).find_one({"knowledge_id": "k1"})
    ashok_id = PersonRepository(db).find_one({"email": "ashok@databeat.io"})["id"]
    assert item["person_id"] == ashok_id


def test_knowledge_item_about_the_company_points_to_the_canonical_org(db):
    # Part 6 example: "DataBeat has 500 employees" -> ORG-xxx, not a Person.
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
    )
    _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k2"},
        {
            "knowledge_id": "k2", "thread_id": "thread_1", "subject_key": "databeat", "predicate": "has",
            "fact_key": "employee_count", "current_value": "500 employees", "history": [],
            "source_emails": ["msg_1"], "basis": "stated", "first_seen_at": _NOW.isoformat(),
            "last_confirmed_at": _NOW.isoformat(), "confidence": 0.75, "status": "active",
        },
    )

    _link_knowledge_to_entities(db, "thread_1", "msg_1")

    item = KnowledgeRepository(db).find_one({"knowledge_id": "k2"})
    assert item.get("person_id") is None
    assert item.get("org_id") is not None


def test_thread_level_knowledge_item_gets_neither_person_nor_org(db):
    # Not every knowledge item is about a specific person or company.
    email = _email()
    analysis = _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
    )
    _process_entities(db, "thread_1", email, analysis, _NOW, "sandeep@example.com")
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k3"},
        {
            "knowledge_id": "k3", "thread_id": "thread_1", "subject_key": "thread_1", "predicate": "requires",
            "fact_key": "seat_count", "current_value": "25 seats", "history": [],
            "source_emails": ["msg_1"], "basis": "stated", "first_seen_at": _NOW.isoformat(),
            "last_confirmed_at": _NOW.isoformat(), "confidence": 0.75, "status": "active",
        },
    )

    _link_knowledge_to_entities(db, "thread_1", "msg_1")

    item = KnowledgeRepository(db).find_one({"knowledge_id": "k3"})
    assert item.get("person_id") is None
    assert item.get("org_id") is None


def test_ambiguous_name_never_produces_a_canonical_relationship(db):
    # TEST 13: two resolved people with compatible name tokens for the same bare
    # mention -- match_resolved_person_by_name must return None, never guess.
    resolved_people = [
        {"id": "PER-001", "name": "Ashok Ganapam", "org_id": "ORG-001"},
        {"id": "PER-002", "name": "Ashok Kumar", "org_id": "ORG-001"},
    ]
    assert match_resolved_person_by_name(resolved_people, "Ashok") is None


# --- Part 14, test 6 & 7: reply draft / calendar action (via run_pipeline) ----------


def _run_pipeline_for(db, email, settings=None):
    from app.config.settings import Settings
    from app.providers.calendar.mock import MockCalendarProvider
    from app.providers.email.mock import MockEmailProvider
    from app.providers.llm.mock import MockLLMProvider
    from app.pipeline import run_pipeline

    settings = settings or Settings(
        email_provider="mock", calendar_provider="mock", llm_provider="mock",
        agent_email="sandeep@example.com",
    )
    provider = MockEmailProvider(payloads=[email.model_dump(mode="json", by_alias=True)])
    return run_pipeline(db, provider, MockLLMProvider(), MockCalendarProvider(), settings)


def test_reply_draft_can_point_to_the_canonical_person(db):
    # TEST 6
    email = _email(body="Could you send over pricing? Let's meet tomorrow at 3pm.")
    _run_pipeline_for(db, email)

    ashok_id = PersonRepository(db).find_one({"email": "ashok@databeat.io"})["id"]
    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_1"})
    assert draft is not None
    assert draft["person_id"] == ashok_id
    assert draft["org_id"] is not None


def test_calendar_action_can_reference_the_canonical_person_without_external_attendees(db):
    # TEST 7
    email = _email(body="Let's meet Tuesday at 3pm to discuss the rollout.")
    _run_pipeline_for(db, email)

    ashok_id = PersonRepository(db).find_one({"email": "ashok@databeat.io"})["id"]
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_1"})
    assert action is not None
    assert action["person_id"] == ashok_id
    # The existing calendar safety rule is completely untouched by this metadata.
    assert action["event"]["attendees"] == []


# --- Part 14, tests 8 & 9: multiple objects converge on one canonical ID -----------


def test_multiple_objects_referring_to_ashok_all_use_the_same_canonical_person(db):
    # TEST 8
    email1 = _email(
        message_id="msg_1", thread_id="thread_1",
        body="Could you send over pricing for Ashok's team?",
    )
    entities1 = _process_entities(db, "thread_1", email1, _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        commitments_mentioned=[
            RawCommitment.model_validate({"what": "send pricing", "class": "mine", "owed_by": None, "owed_to": "Ashok Ganapam"})
        ],
        meetings_mentioned=[RawMeeting.model_validate({"attendees": ["Ashok"], "is_past": False, "actions_raised": []})],
    ), _NOW, "sandeep@example.com")

    ashok_id = entities1["people"][0]
    commitment = CommitmentRepository(db).find_one({"id": entities1["commitments"][0]})
    meeting = MeetingRepository(db).find_one({"id": entities1["meetings"][0]})

    assert commitment["person_id"] == ashok_id
    assert ashok_id in meeting["person_ids"]
    # Ashok + the operator's own dedicated profile (the email's "to", agent_email).
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_multiple_objects_referring_to_databeat_all_use_the_same_canonical_org(db):
    # TEST 9
    email = _email()
    entities = _process_entities(db, "thread_1", email, _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        projects_mentioned=[MentionedProject(name="Rollout", org="DataBeat")],
    ), _NOW, "sandeep@example.com")

    person = PersonRepository(db).find_one({"id": entities["people"][0]})
    project = ProjectRepository(db).find_one({"id": entities["projects"][0]})

    assert person["org_id"] == project["org_id"]
    assert OrganizationRepository(db).find_many({}).__len__() == 1


# --- Part 14, tests 10-12: get_person_context priority/labeling --------------------


def test_get_person_context_retrieves_canonical_relationships(db):
    # TEST 10
    email = _email()
    entities = _process_entities(db, "thread_1", email, _analysis(
        people_mentioned=[MentionedPerson(name="Ashok Ganapam", email="ashok@databeat.io", org="DataBeat")],
        commitments_mentioned=[
            RawCommitment.model_validate({"what": "send pricing", "class": "mine", "owed_by": None, "owed_to": "Ashok"})
        ],
        meetings_mentioned=[RawMeeting.model_validate({"attendees": ["Ashok"], "is_past": False, "actions_raised": []})],
        projects_mentioned=[MentionedProject(name="Rollout", org="DataBeat")],
    ), _NOW, "sandeep@example.com")
    _link_thread_to_entities(db, "thread_1")

    ashok_id = entities["people"][0]
    context = get_person_context(db, ashok_id)

    assert len(context["commitments"]) == 1
    assert context["commitments"][0]["basis"] == "canonical_person_id"
    assert len(context["meetings"]) == 1
    assert context["meetings"][0]["basis"] == "canonical_person_id"
    assert len(context["follow_ups"]) == 1
    assert context["follow_ups"][0]["basis"] == "canonical_person_id"
    assert len(context["projects"]) == 1
    assert context["projects"][0]["basis"] == "canonical_person_id"


def test_get_person_context_never_copies_related_content_into_the_person_document(db):
    # TEST 11
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="thread_1",
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_1"},
        {
            "message_id": "msg_1", "thread_id": "thread_1", "subject": "hello",
            "timestamp": _NOW.isoformat(), "entities_referenced": {"people": [person_id]},
        },
    )

    get_person_context(db, person_id)  # assembling context must not mutate the Person

    stored = PersonRepository(db).find_one({"id": person_id})
    assert set(stored.keys()) == {
        "id", "name", "email", "aliases", "org", "org_id", "type", "goal_pillar",
        "role_in_pillar", "tier", "voice_register", "preferences", "last_inbound", "last_outbound",
        "reports_to", "open_threads", "note_link", "review_flag", "source",
        "status", "merged_into",
    }


def test_get_person_context_falls_back_to_legacy_name_match_only_when_no_canonical_link_exists(db):
    # TEST 12: a commitment written before canonical references existed (no person_id
    # at all) is still surfaced, but explicitly labeled as a legacy, name-matched
    # result -- never presented as equivalent to a canonical relationship.
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="thread_1",
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-legacy"},
        {
            "id": "COM-legacy", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "msg_0", "made_on": _NOW.isoformat(),
            "thread_id": "thread_1", "status": "open", "person_id": None, "org_id": None,
        },
    )

    context = get_person_context(db, person_id)

    assert len(context["commitments"]) == 1
    assert context["commitments"][0]["basis"] == "legacy_thread_scoped_name_match"


# --- Company 360 (get_organization_context) ------------------------------------------------


def test_get_organization_context_returns_none_for_unknown_org(db):
    assert get_organization_context(db, "ORG-999") is None


def test_get_organization_context_assembles_canonical_org_id_links(db):
    from app.database.repositories import (
        CommitmentRepository,
        FollowUpRepository,
        MeetingRepository,
        OrganizationRepository,
        ProjectRepository,
        ReplyDraftRepository,
        ThreadRepository,
    )

    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="thread_1",
    )
    # resolve_person's own domain-based org_id resolution already links this person to
    # a real Organization -- overwrite with the pre-seeded ORG-001 id for a
    # deterministic, known-id assertion below.
    from app.database.repositories import PersonRepository as PR

    PR(db).upsert_by_key({"id": person_id}, {**PR(db).find_one({"id": person_id}), "org_id": "ORG-001"})

    ThreadRepository(db).upsert_by_key(
        {"thread_id": "thread_1"},
        {"thread_id": "thread_1", "normalized_subject": "x", "message_ids": ["m1"], "org_ids": ["ORG-001"]},
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m1", "made_on": _NOW.isoformat(), "status": "open",
            "thread_id": "thread_1", "person_id": person_id, "org_id": "ORG-001",
        },
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"},
        {"id": "MTG-001", "date": _NOW.isoformat(), "attendees": [], "person_ids": [person_id], "org_id": "ORG-001", "actionable": True, "thread_id": "thread_1"},
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-001"}, {"id": "FU-001", "commitment_id": "COM-001", "thread_id": "thread_1", "person_id": person_id, "org_id": "ORG-001"}
    )
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-001"},
        {
            "id": "PRJ-001", "project": "Rollout", "entity": "DataBeat", "goal_pillar": "Sales",
            "collaborators": [], "person_ids": [person_id], "org_id": "ORG-001", "source": "gmail",
        },
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "m1"},
        {
            "reply_id": "reply_m1", "thread_id": "thread_1", "source_email_id": "m1", "status": "awaiting_approval",
            "draft": {"subject": "Re: x", "body": "..."}, "person_id": person_id, "org_id": "ORG-001",
            "created_by": "sales_agent", "created_at": _NOW.isoformat(),
        },
    )

    context = get_organization_context(db, "ORG-001")

    assert context["organization"]["id"] == "ORG-001"
    assert [p["id"] for p in context["people"]["data"]] == [person_id]
    assert [t["thread_id"] for t in context["threads"]["data"]] == ["thread_1"]
    assert [c["id"] for c in context["commitments"]["data"]] == ["COM-001"]
    assert [m["id"] for m in context["meetings"]["data"]] == ["MTG-001"]
    assert [f["id"] for f in context["follow_ups"]["data"]] == ["FU-001"]
    assert [p["id"] for p in context["projects"]["data"]] == ["PRJ-001"]
    assert [r["reply_id"] for r in context["reply_drafts"]["data"]] == ["reply_m1"]
    for category in ["people", "threads", "commitments", "meetings", "follow_ups", "projects", "reply_drafts"]:
        assert context[category]["basis"] == "canonical_org_id"


def test_get_organization_context_never_copies_content_into_the_organization_document(db):
    from app.database.repositories import OrganizationRepository

    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="thread_1",
    )

    get_organization_context(db, "ORG-001")
    get_organization_context(db, "ORG-001")

    org = OrganizationRepository(db).find_one({"id": "ORG-001"})
    assert set(org.keys()) == {"id", "name", "domain", "aliases", "source"}


# --- Phase 19.1: get_person_context surfaces the canonical redirect, additively -----------


def test_get_person_context_active_person_canonical_id_is_itself(db):
    person_id = resolve_person(
        db, {"name": "Active Person", "email": "active360@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    context = get_person_context(db, person_id)

    assert context["canonical_person_id"] == person_id
    assert context["canonical_resolution_error"] is None


def test_get_person_context_merged_person_reports_canonical_target(db):
    canonical_id = resolve_person(
        db, {"name": "Canonical 360", "email": "canonical360@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    duplicate_id = resolve_person(
        db, {"name": "Duplicate 360", "email": "duplicate360@example.com"}, is_sender=True, now=_NOW, thread_id="thread_2"
    )
    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    PersonRepository(db).upsert_by_key({"id": duplicate_id}, {**duplicate, "status": "merged", "merged_into": canonical_id})

    context = get_person_context(db, duplicate_id)

    # Historical info is preserved -- "person" is still the duplicate's own document,
    # not silently replaced by the canonical's -- only the new field points onward.
    assert context["person"]["id"] == duplicate_id
    assert context["canonical_person_id"] == canonical_id
    assert context["canonical_resolution_error"] is None


def test_get_person_context_broken_chain_surfaces_error_without_raising(db):
    duplicate_id = resolve_person(
        db, {"name": "Broken Chain", "email": "brokenchain@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    PersonRepository(db).upsert_by_key({"id": duplicate_id}, {**duplicate, "status": "merged", "merged_into": None})

    context = get_person_context(db, duplicate_id)  # must not raise

    assert context is not None
    assert context["canonical_person_id"] is None
    assert "no merged_into target" in context["canonical_resolution_error"]
