"""Thread Events -- an append-only, descriptive audit trail over the existing
pipeline (never a second pipeline, never responsible for entity mutation itself).

Covers Phase 10's 19 requirements and the Phase 11 edge cases (A-K) from the
Thread Events task: creation/reuse/update event types, idempotency on retry,
deterministic ordering, canonical-id-only identifiers, failure isolation, and
compatibility with Person Context / the query layer / canonical-ID behavior.
"""

import json
from datetime import datetime, timezone

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import PersonRepository, ThreadEventRepository
from app.entities.thread_events import get_thread_event_trail, record_event, try_record_event
from app.interfaces.llm_provider import LLMProvider
from app.mcp import tools
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider

_RAW_GMAIL_ID_1 = "gmail_raw_aaa111"
_RAW_GMAIL_ID_2 = "gmail_raw_bbb222"
_RAW_GMAIL_THREAD = "gmail_raw_thread_ccc333"


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock", agent_email="ashok@example.com")


def _raw_email(message_id, body, thread_id=None, **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Project Alpha",
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    if thread_id:
        raw["thread_id"] = thread_id
    raw.update(overrides)
    return raw


def _events_for(db, thread_id, event_type=None, entity_type=None):
    events = get_thread_event_trail(db, thread_id)
    if event_type:
        events = [e for e in events if e["event_type"] == event_type]
    if entity_type:
        events = [e for e in events if e["entity_type"] == entity_type]
    return events


class _AttributedCommitmentAndMeetingLLM(LLMProvider):
    """Real name attribution (see tests/test_person_context.py's own double of the
    same shape) -- MockLLMProvider never fills owed_by/owed_to/attendees with a
    real name, so its commitments/meetings never resolve a person_id."""

    def analyze_email(self, email, thread_history=None, person_context=None):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "commitment",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": [], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [], "companies": [], "products": [],
            "people_mentioned": [{"name": "John", "email": email.from_.email, "org": None, "role_hint": None}],
            "projects_mentioned": [{"name": "Project Alpha", "org": None, "objective_hint": None}],
            "commitments_mentioned": [
                {"what": "send the proposal", "class": "mine", "owed_by": "John", "owed_to": "Ashok", "date_phrase": None, "importance_hint": None}
            ],
            "meetings_mentioned": [
                {"date_phrase": None, "attendees": ["John"], "is_past": False, "actions_raised": []}
            ],
            "personal_items_mentioned": [], "goal_pillar": "Sales", "label_applied": "Needs reply",
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


# --- 1/9/10: email_received + canonical ids, no raw id leakage -------------------------------


def test_email_received_event_uses_canonical_ids_and_never_leaks_raw_gmail_ids(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    received = [e for e in trail if e["event_type"] == "email_received"]
    assert len(received) == 1
    assert received[0]["thread_id"] == "THR-001"
    assert received[0]["email_id"] == "EML-001"
    assert received[0]["entity_id"] == "EML-001"

    dumped = json.dumps(trail)
    assert _RAW_GMAIL_ID_1 not in dumped
    assert _RAW_GMAIL_THREAD not in dumped


# --- 2/3: person created vs reused -------------------------------------------------------------


def test_new_person_generates_entity_created_event(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = PersonRepository(db).find_one({"email": "john@customerco.example"})["id"]
    person_events = _events_for(db, "THR-001", entity_type="person")
    john_event = next(e for e in person_events if e["entity_id"] == john_id)
    assert john_event["event_type"] == "entity_created"
    assert john_event["operation"] == "created"


def test_reusing_an_existing_person_generates_entity_reused_event(db, settings):
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Following up.", thread_id=_RAW_GMAIL_THREAD, timestamp="2026-09-14T10:30:00Z",
    )
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = PersonRepository(db).find_one({"email": "john@customerco.example"})["id"]
    events_email_2 = [e for e in get_thread_event_trail(db, "THR-001") if e["email_id"] == "EML-002"]
    john_event = next(e for e in events_email_2 if e["entity_type"] == "person" and e["entity_id"] == john_id)
    assert john_event["event_type"] in {"entity_reused", "entity_updated"}
    assert john_event["operation"] in {"reused", "updated"}


# --- 4/5/6: commitment/meeting/follow-up/project created vs reused vs updated -----------------


def test_commitment_meeting_follow_up_project_created_events(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow. Let's schedule a call.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), _AttributedCommitmentAndMeetingLLM(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    event_types = {e["event_type"] for e in trail}
    assert "commitment_created" in event_types
    assert "meeting_created" in event_types
    assert "follow_up_created" in event_types
    # project: no named project_created type -- generic entity_created is expected.
    project_events = [e for e in trail if e["entity_type"] == "project"]
    assert any(e["event_type"] == "entity_created" and e["operation"] == "created" for e in project_events)


def test_reusing_commitment_and_meeting_produces_reused_events(db, settings):
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "First email.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Second email.", thread_id=_RAW_GMAIL_THREAD, timestamp="2026-09-14T10:30:00Z",
    )
    llm = _AttributedCommitmentAndMeetingLLM()
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm, MockCalendarProvider(), settings)

    events_email_2 = [e for e in get_thread_event_trail(db, "THR-001") if e["email_id"] == "EML-002"]
    assert any(e["event_type"] == "commitment_reused" for e in events_email_2)
    assert any(e["event_type"] == "meeting_reused" for e in events_email_2)


def test_project_backfill_on_reuse_produces_project_updated_event(db, settings):
    # The PROJECT's own mention.org ("CustomerCo") must stay IDENTICAL across both
    # emails -- resolve_project's dedup match is entity(mention.org)+name+goal_pillar,
    # so changing it would create a second, distinct project rather than reusing the
    # first one. What differs is whether the PERSON mention (John) also carries
    # org="CustomerCo": on email_1 it doesn't, so no Person in resolved_people has a
    # matching `org` text and project_org_id stays None at creation; on email_2 it
    # does, giving _process_entities real evidence to backfill project_org_id onto
    # the ALREADY-EXISTING project -- a genuine "updated" outcome, not "created".
    email_1 = {
        "message_id": _RAW_GMAIL_ID_1, "from": {"name": "John", "email": "john@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}], "subject": "Project Alpha",
        "body": "no-op", "timestamp": "2026-09-13T10:30:00Z", "thread_id": _RAW_GMAIL_THREAD,
    }

    class _ProjectLLM(LLMProvider):
        def __init__(self, person_org: str | None):
            self._person_org = person_org

        def analyze_email(self, email, thread_history=None, person_context=None):
            return {
                "email_id": email.message_id, "summary": "s", "intent": "evaluation", "entities": [], "facts": [],
                "requirements": [], "pain_points": [], "buying_signals": [], "objections": [], "competitors": [],
                "pricing_mentions": [], "commitments": [], "action_items": [], "meetings": [], "people": [],
                "companies": [], "products": [],
                "people_mentioned": [
                    {"name": "John", "email": "john@customerco.example", "org": self._person_org, "role_hint": None}
                ],
                "projects_mentioned": [{"name": "Project Alpha", "org": "CustomerCo", "objective_hint": None}],
                "commitments_mentioned": [], "meetings_mentioned": [], "personal_items_mentioned": [],
                "goal_pillar": "Sales", "label_applied": "Read only",
            }

        def update_context(self, previous_context, new_analysis):
            return {}

        def verify_same_fact(self, existing_value, new_value, subject, predicate):
            return False

        def draft_reply(self, context, latest_email):
            return {"subject": "s", "body": "b"}

    email_2 = dict(email_1, message_id=_RAW_GMAIL_ID_2, timestamp="2026-09-14T10:30:00Z")
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), _ProjectLLM(person_org=None), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), _ProjectLLM(person_org="CustomerCo"), MockCalendarProvider(), settings)

    events_email_2 = [e for e in get_thread_event_trail(db, "THR-001") if e["email_id"] == "EML-002"]
    project_event = next(e for e in events_email_2 if e["entity_type"] == "project")
    assert project_event["event_type"] == "project_updated"
    assert project_event["operation"] == "updated"


# --- 7: person_context_enriched event --------------------------------------------------------


def test_person_context_enrichment_creates_event(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = PersonRepository(db).find_one({"email": "john@customerco.example"})["id"]
    events = _events_for(db, "THR-001", event_type="person_context_enriched")
    john_events = [e for e in events if e["metadata"].get("person_id") == john_id]
    assert len(john_events) == 1
    assert john_events[0]["entity_type"] == "person_context"
    assert john_events[0]["operation"] == "enriched"


# --- 8/B/12: multiple emails, one ordered trail, deterministic sequence -----------------------


def test_multiple_emails_in_one_thread_produce_one_ordered_trail(db, settings):
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Following up.", thread_id=_RAW_GMAIL_THREAD, timestamp="2026-09-14T10:30:00Z",
    )
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    sequences = [e["sequence"] for e in trail]
    assert sequences == sorted(sequences)
    assert len(sequences) == len(set(sequences))  # no collisions
    assert {e["email_id"] for e in trail} == {"EML-001", "EML-002"}
    # Every EML-001 event's sequence is strictly less than every EML-002 event's --
    # the trail reflects real chronological/processing order, not just grouping.
    max_seq_email_1 = max(e["sequence"] for e in trail if e["email_id"] == "EML-001")
    min_seq_email_2 = min(e["sequence"] for e in trail if e["email_id"] == "EML-002")
    assert max_seq_email_1 < min_seq_email_2


# --- C: two different threads involving the same person ---------------------------------------


def test_two_different_threads_for_the_same_person_produce_separate_trails(db, settings):
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "Thread one.", thread_id="gmail_thread_one")
    email_2 = _raw_email(_RAW_GMAIL_ID_2, "Thread two.", thread_id="gmail_thread_two", timestamp="2026-09-14T10:30:00Z")
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = PersonRepository(db).find_one({"email": "john@customerco.example"})["id"]
    trail_1 = _events_for(db, "THR-001", entity_type="person")
    trail_2 = _events_for(db, "THR-002", entity_type="person")
    assert any(e["entity_id"] == john_id for e in trail_1)
    assert any(e["entity_id"] == john_id for e in trail_2)
    # No cross-contamination: THR-001's trail never contains an EML-002 event.
    assert all(e["email_id"] == "EML-001" for e in trail_1)
    assert all(e["email_id"] == "EML-002" for e in trail_2)


# --- D: multiple people mentioned in one email --------------------------------------------------


def test_multiple_people_in_one_email_each_get_their_own_event(db, settings):
    email = _raw_email(
        _RAW_GMAIL_ID_1, "I will send the proposal tomorrow.",
        cc=[{"name": "Jane", "email": "jane@customerco.example"}],
    )
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = PersonRepository(db).find_one({"email": "john@customerco.example"})["id"]
    jane_id = PersonRepository(db).find_one({"email": "jane@customerco.example"})["id"]
    person_events = _events_for(db, "THR-001", entity_type="person")
    assert any(e["entity_id"] == john_id for e in person_events)
    assert any(e["entity_id"] == jane_id for e in person_events)
    assert john_id != jane_id


# --- 11/A: retrying the same email does not duplicate logical events --------------------------


def test_retrying_same_operation_does_not_duplicate_events(db, settings):
    result_1 = record_event(db, "THR-001", "EML-001", "entity_created", "person", "PER-001", "created", "Person PER-001 created")
    result_2 = record_event(db, "THR-001", "EML-001", "entity_created", "person", "PER-001", "created", "Person PER-001 created")

    events = ThreadEventRepository(db).find_many({"thread_id": "THR-001"})
    assert len(events) == 1
    assert result_1.id == result_2.id
    assert result_1.sequence == result_2.sequence


def test_retry_that_observes_a_different_operation_corrects_the_same_event_in_place(db, settings):
    created = record_event(db, "THR-001", "EML-001", "entity_created", "person", "PER-001", "created", "created")
    corrected = record_event(db, "THR-001", "EML-001", "entity_created", "person", "PER-001", "updated", "updated on retry")

    events = ThreadEventRepository(db).find_many({"thread_id": "THR-001"})
    assert len(events) == 1
    assert corrected.id == created.id
    assert corrected.sequence == created.sequence  # sequence never reallocated on retry
    assert events[0]["operation"] == "updated"  # corrected in place, not appended


def test_a_later_email_produces_a_new_event_not_a_mutation_of_an_earlier_one(db, settings):
    first = record_event(db, "THR-001", "EML-001", "entity_reused", "person", "PER-001", "reused", "reused in EML-001")
    second = record_event(db, "THR-001", "EML-002", "entity_reused", "person", "PER-001", "reused", "reused in EML-002")

    events = ThreadEventRepository(db).find_many({"thread_id": "THR-001"})
    assert len(events) == 2
    assert first.id != second.id
    assert first.sequence != second.sequence


# --- 13/H: event persistence failure never breaks core email processing -----------------------


def test_event_persistence_failure_does_not_break_core_pipeline(db, settings, monkeypatch):
    import app.entities.thread_events as thread_events_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated thread-event write failure")

    monkeypatch.setattr(thread_events_module, "record_event", _boom)

    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    summary = run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    assert summary.completed == 1
    assert summary.failed == 0
    stored = db.emails.find_one({"source_message_id": _RAW_GMAIL_ID_1}, {"_id": 0})
    assert stored["processing_status"]["stage"] == "COMPLETED"
    # Not one single Thread Event was written -- confirms it really broke, and the
    # pipeline still completed anyway.
    assert db.thread_events.count_documents({}) == 0


def test_try_record_event_returns_none_on_failure_without_raising(db, monkeypatch):
    import app.entities.thread_events as thread_events_module

    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(thread_events_module, "record_event", _boom)

    result = try_record_event(db, "THR-001", "EML-001", "entity_created", "person", "PER-001", "created", "x")

    assert result is None


# --- 14/G: processing failure produces a processing_failed event ------------------------------


def test_processing_failure_produces_processing_failed_event(db, settings, monkeypatch):
    import app.pipeline as pipeline_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated entity resolution failure")

    monkeypatch.setattr(pipeline_module, "resolve_person_with_operation", _boom)

    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    events = _events_for(db, "THR-001", event_type="processing_failed")
    assert len(events) == 1
    assert events[0]["entity_type"] == "pipeline"
    assert events[0]["operation"] == "failed"
    assert events[0]["entity_id"] == "ENTITIES_PROCESSED"
    assert "simulated entity resolution failure" not in json.dumps(events[0])  # no raw exception text leak beyond classification
    assert events[0]["metadata"]["error_type"] == "RuntimeError"


# --- 15: get_thread_event_trail ordered hierarchy ----------------------------------------------


def test_get_thread_event_trail_returns_events_ordered_by_sequence(db, settings):
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Let's schedule a call.", thread_id=_RAW_GMAIL_THREAD, timestamp="2026-09-14T10:30:00Z",
    )
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    assert len(trail) > 0
    assert [e["sequence"] for e in trail] == sorted(e["sequence"] for e in trail)
    assert trail[0]["event_type"] == "email_received"  # the very first thing that happens for a thread


# --- J: thread with no raw Gmail thread id ------------------------------------------------------


def test_thread_with_no_raw_gmail_thread_id_still_gets_canonical_thread_id(db, settings):
    email = {
        "message_id": _RAW_GMAIL_ID_1, "from": {"name": "John", "email": "john@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}], "subject": "No thread id at all",
        "body": "I will send the proposal tomorrow.", "timestamp": "2026-09-13T10:30:00Z",
        # deliberately no "thread_id" key at all
    }
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    assert len(trail) > 0
    assert all(e["thread_id"] == "THR-001" for e in trail)


# --- K: raw Gmail ids that look superficially similar to canonical ids -------------------------


def test_raw_gmail_id_that_looks_like_a_canonical_id_is_never_confused_with_one(db, settings):
    email = {
        "message_id": "EML-999-LOOKALIKE", "from": {"name": "John", "email": "john@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}], "subject": "Lookalike id",
        "body": "I will send the proposal tomorrow.", "timestamp": "2026-09-13T10:30:00Z",
        "thread_id": "THR-999-LOOKALIKE",
    }
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")  # the REAL, freshly-allocated canonical thread id
    assert len(trail) > 0
    assert all(e["thread_id"] == "THR-001" for e in trail)
    assert all(e["email_id"] in {None, "EML-001"} for e in trail)
    # The raw, lookalike strings are preserved only as source_* fields, never as a
    # Thread Event's own canonical thread_id/email_id.
    email_doc = db.emails.find_one({"source_message_id": "EML-999-LOOKALIKE"}, {"_id": 0})
    assert email_doc["message_id"] == "EML-001"
    assert email_doc["id"] == "EML-001"
    assert get_thread_event_trail(db, "EML-999-LOOKALIKE") == []  # not a real, indexed thread


# --- 16/17/18: existing Person Context / query / canonical-id behavior remains unchanged ------


def test_get_thread_mcp_tool_exposes_the_event_trail_additively(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    result = tools.get_thread(db, "THR-001")

    assert result is not None
    assert result["thread_id"] == "THR-001"  # existing keys untouched
    assert "event_trail" in result
    assert len(result["event_trail"]) > 0
    assert _RAW_GMAIL_ID_1 not in json.dumps(result["event_trail"])
