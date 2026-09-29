"""Person Context Snapshots -- Layer 2 of the three-layer person-memory design
(Standing Person / Person Context Snapshots / Knowledge Items).

Covers: creation on first email, incremental enrichment on a second email,
accumulated bounded context actually reaching LLM reasoning, provenance, processing
outputs beyond KnowledgeItems, conflicting-fact history semantics (reusing
KnowledgeItem's own mechanism, never a competing one), idempotency on retry,
multi-email-same-thread enrichment, canonical EML/THR ids (never raw Gmail ids),
and compatibility with get_person_context/the query layer/KnowledgeItem.
"""

from datetime import datetime, timezone
from typing import Any

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    KnowledgeRepository,
    PersonContextSnapshotRepository,
    PersonRepository,
)
from app.entities.context import get_person_context
from app.entities.person_context import get_bounded_person_context_for_llm
from app.interfaces.llm_provider import LLMProvider
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider
from app.query.schemas import QueryIntentType, QueryRequest
from app.query.service import execute_query

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


def _john_person_id(db):
    person = PersonRepository(db).find_one({"email": "john@customerco.example"})
    assert person is not None
    return person["id"]


class _CapturingLLMProvider(LLMProvider):
    """Delegates every call to a real MockLLMProvider unchanged, but records the
    exact person_context each analyze_email call actually received -- so a test can
    assert on what the pipeline handed the LLM, not just what ended up in Mongo."""

    def __init__(self):
        self._inner = MockLLMProvider()
        self.received_person_contexts: list[dict[str, Any] | None] = []

    def analyze_email(self, email, thread_history=None, person_context=None):
        self.received_person_contexts.append(person_context)
        return self._inner.analyze_email(email, thread_history=thread_history, person_context=person_context)

    def update_context(self, previous_context, new_analysis):
        return self._inner.update_context(previous_context, new_analysis)

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return self._inner.verify_same_fact(existing_value, new_value, subject, predicate)

    def draft_reply(self, context, latest_email):
        return self._inner.draft_reply(context, latest_email)


class _NamedFactLLMProvider(LLMProvider):
    """A double that extracts a knowledge fact whose subject is the sender's own
    real name ("John") -- MockLLMProvider's own facts field always uses the literal
    subject "Customer", which never resolves to a real Person, so this double is
    needed to exercise a knowledge item that DOES get person_id-linked (see
    app.pipeline._link_knowledge_to_entities) and therefore shows up embedded in
    that person's context snapshot."""

    def __init__(self, value: str):
        self._value = value

    def analyze_email(self, email, thread_history=None, person_context=None):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "evaluation",
            "entities": [], "facts": [{"subject": "John", "predicate": "prefers", "object": self._value}],
            "requirements": [], "pain_points": [], "buying_signals": ["pricing request"], "objections": [],
            "competitors": [], "pricing_mentions": [], "commitments": [], "action_items": [], "meetings": [],
            "people": [], "companies": [], "products": [],
            "people_mentioned": [{"name": "John", "email": "john@customerco.example", "org": None, "role_hint": None}],
            "projects_mentioned": [], "commitments_mentioned": [], "meetings_mentioned": [],
            "personal_items_mentioned": [], "goal_pillar": "Sales", "label_applied": "Needs reply",
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


class _AttributedCommitmentAndMeetingLLM(LLMProvider):
    """MockLLMProvider's own commitment/meeting extraction never fills owed_by/
    owed_to/attendees with a real name (see test_end_to_end_validation.py's own
    _ThirdPersonCommitmentLLM docstring for the same, pre-existing limitation), so
    a Commitment/Meeting it produces never resolves a person_id -- not a Person
    Context bug, just Mock's own coverage gap. This double simulates what a real
    LLM returns: a commitment and meeting both attributed to the sender's own
    real name, so app.pipeline._process_entities resolves a real person_id onto
    each, exercising Person Context's "beyond KnowledgeItems" categories."""

    def analyze_email(self, email, thread_history=None, person_context=None):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "commitment",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": [], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [], "companies": [], "products": [],
            "people_mentioned": [{"name": "John", "email": email.from_.email, "org": None, "role_hint": None}],
            "projects_mentioned": [],
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


# --- 1/9/10: creation, canonical ids, no raw id leakage --------------------------------------


def test_first_email_creates_person_context_snapshot_with_canonical_ids(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = _john_person_id(db)
    snapshots = PersonContextSnapshotRepository(db).find_many({"person_id": john_id})
    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot["source_email_id"] == "EML-001"
    assert snapshot["thread_id"] == "THR-001"
    assert len(snapshot["entries"]) >= 1
    for entry in snapshot["entries"]:
        assert entry["email_id"] == "EML-001"
        assert entry["thread_id"] == "THR-001"

    # No raw Gmail id ever leaks into the person-context domain identifiers.
    import json

    dumped = json.dumps(snapshots)
    assert _RAW_GMAIL_ID_1 not in dumped
    assert _RAW_GMAIL_THREAD not in dumped


# --- 4/5: provenance + processing outputs beyond KnowledgeItems ------------------------------


def test_snapshot_incorporates_processing_outputs_beyond_knowledge_items_with_provenance(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow. Let's schedule a call.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), _AttributedCommitmentAndMeetingLLM(), MockCalendarProvider(), settings)

    john_id = _john_person_id(db)
    snapshot = PersonContextSnapshotRepository(db).find_one({"person_id": john_id})
    categories = {e["category"] for e in snapshot["entries"]}

    # Beyond KnowledgeItems: a real commitment and a real meeting, both resolved by
    # app.pipeline._process_entities from this same email.
    assert "commitment" in categories
    assert "meeting" in categories
    assert "email_interaction" in categories
    for entry in snapshot["entries"]:
        assert entry["provenance"] in {"observed", "inferred"}


# --- 2/3/8: second email incorporates + receives prior accumulated context -------------------


def test_second_email_incorporates_and_receives_prior_accumulated_bounded_context(db, settings):
    llm = _CapturingLLMProvider()
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Following up on the proposal.", thread_id=_RAW_GMAIL_THREAD,
        timestamp="2026-09-14T10:30:00Z",
    )

    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm, MockCalendarProvider(), settings)
    john_id = _john_person_id(db)
    context_after_email_1 = get_bounded_person_context_for_llm(db, john_id)
    assert context_after_email_1["current_context"] != []

    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm, MockCalendarProvider(), settings)

    # The SECOND call's person_context (captured before email_2's own processing
    # ran) must reflect what email_1 already established -- proving the wiring
    # actually reaches LLM reasoning, not just Mongo.
    assert len(llm.received_person_contexts) == 2
    assert llm.received_person_contexts[0] is None  # John was brand new for email_1
    second_call_context = llm.received_person_contexts[1]
    assert second_call_context is not None
    assert second_call_context["person_id"] == john_id
    assert second_call_context["current_context"] == context_after_email_1["current_context"]

    # Multiple emails in the same thread enrich the SAME person's context (not two
    # unrelated people) -- two independent snapshots, same person_id, same thread_id.
    snapshots = PersonContextSnapshotRepository(db).find_many({"person_id": john_id})
    assert len(snapshots) == 2
    assert {s["source_email_id"] for s in snapshots} == {"EML-001", "EML-002"}
    assert {s["thread_id"] for s in snapshots} == {"THR-001"}

    # After email_2, its bounded view shows email_2's own entries as current and
    # email_1's as historical/reconstructed -- distinguishing current from history.
    context_after_email_2 = get_bounded_person_context_for_llm(db, john_id)
    assert context_after_email_2["current_context"] != context_after_email_1["current_context"]
    assert context_after_email_2["historical_context"] != []
    assert all(e["provenance"] == "reconstructed" for e in context_after_email_2["historical_context"])


# --- 6/13: conflicting facts preserve history/current-state semantics (reused, not duplicated) --


def test_conflicting_knowledge_fact_preserves_history_via_existing_mechanism(db, settings):
    # Same thread, same subject_key (both derived from thread_id itself for the
    # requirements field -- see app.pipeline._process_knowledge), same fact_key
    # ("seat_count", via app.knowledge.normalize.classify_fact_key) -- a genuine
    # exact-match conflict that exercises app.knowledge.deduplication.process_new_fact's
    # own, pre-existing history mechanism. Person Context never re-implements this.
    email_1 = _raw_email(_RAW_GMAIL_ID_1, "We need 50 seats.", thread_id=_RAW_GMAIL_THREAD)
    email_2 = _raw_email(
        _RAW_GMAIL_ID_2, "Actually we need 75 seats.", thread_id=_RAW_GMAIL_THREAD,
        timestamp="2026-09-14T10:30:00Z",
    )
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    items = KnowledgeRepository(db).find_many({"thread_id": "THR-001", "predicate": "requires"})
    assert len(items) == 1
    item = items[0]
    assert item["current_value"] == "75 seats"  # current state reflects the newer observation
    assert len(item["history"]) == 2  # both observations preserved, in order
    assert item["history"][0]["value"] == "50 seats"
    assert item["history"][1]["value"] == "75 seats"


def test_knowledge_fact_linked_to_person_appears_in_their_context_with_correct_provenance(db, settings):
    llm = _NamedFactLLMProvider("morning meetings")
    email = _raw_email(_RAW_GMAIL_ID_1, "I prefer morning meetings.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    john_id = _john_person_id(db)
    snapshot = PersonContextSnapshotRepository(db).find_one({"person_id": john_id})
    knowledge_entries = [e for e in snapshot["entries"] if e["category"] == "knowledge"]
    assert len(knowledge_entries) == 1
    assert knowledge_entries[0]["provenance"] == "observed"  # basis="stated" on the underlying KnowledgeItem
    assert "morning meetings" in knowledge_entries[0]["summary"]


# --- 7: retrying the same email does not duplicate the logical enrichment -------------------


def test_retrying_the_same_email_does_not_duplicate_person_context(db, settings):
    from app.entities.person_context import enrich_person_context_from_email
    from app.pipeline import _link_knowledge_to_entities, _link_thread_to_entities, _process_entities
    from app.analysis.extractor import analyze_email_with_validation
    from app.email.models import parse_email
    from app.pipeline import ingest_raw_email, resolve_and_persist_thread
    from app.database.repositories import EmailRepository, ThreadRepository

    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    raw = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    email, _ = ingest_raw_email(email_repo, parse_email(raw), db)
    thread_id = resolve_and_persist_thread(thread_repo, email_repo, email, db)
    outcome = analyze_email_with_validation(MockLLMProvider(), email)
    analysis = outcome.analysis
    entities_referenced = _process_entities(
        db, thread_id, email, analysis, email.timestamp, settings.agent_email, MockLLMProvider(), settings.agent_name
    )
    _link_knowledge_to_entities(db, thread_id, email.message_id)
    _link_thread_to_entities(db, thread_id)

    enrich_person_context_from_email(db, thread_id, email, analysis, entities_referenced, email.timestamp)
    enrich_person_context_from_email(db, thread_id, email, analysis, entities_referenced, email.timestamp)  # retry

    john_id = _john_person_id(db)
    snapshots = PersonContextSnapshotRepository(db).find_many({"person_id": john_id})
    assert len(snapshots) == 1  # one logical enrichment, not two


# --- 11/12/13: existing get_person_context / query layer / KnowledgeItem compatibility --------


def test_get_person_context_remains_compatible(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    john_id = _john_person_id(db)
    context = get_person_context(db, john_id)

    assert context is not None
    assert context["person"]["id"] == john_id
    assert "knowledge" in context and "commitments" in context  # unchanged shape


def test_query_layer_remains_compatible_after_person_context_enrichment(db, settings):
    email = _raw_email(_RAW_GMAIL_ID_1, "I will send the proposal tomorrow.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    request = QueryRequest(text="What are my commitments?", reference_datetime=datetime(2026, 9, 15, tzinfo=timezone.utc))
    result = execute_query(db, request)

    assert result.status == "ok"
    assert result.intent == QueryIntentType.COMMITMENTS
    assert len(result.evidence) == 1
    assert result.evidence[0].email_id == "EML-001"
