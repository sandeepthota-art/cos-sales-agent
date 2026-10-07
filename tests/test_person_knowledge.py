"""P0 fix: durable, source-traceable, semantically meaningful person knowledge --
qualitative facts (role/responsibility/preference/goal/interest/concern/pain_point/
objection/buying_signal) EXPLICITLY attributed to a canonical person_id, reusing
the existing KnowledgeItem architecture (never a parallel knowledge system), and
surfaced through Person Context into the LLM's bounded context.

Covers Phase 10 (16 items), Phase 11 (critical end-to-end quality test), and
Phase 12 (thread trail reconstruction) of the P0 person-knowledge task.
"""

import json
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
from app.entities.person_context import get_bounded_person_context_for_llm
from app.entities.thread_events import get_thread_event_trail
from app.interfaces.llm_provider import LLMProvider
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.llm.thread_history import format_person_context

_RAW_1 = "gmail_raw_pk_111"
_RAW_2 = "gmail_raw_pk_222"
_RAW_3 = "gmail_raw_pk_333"
_RAW_4 = "gmail_raw_pk_444"
_RAW_THREAD = "gmail_raw_pk_thread"

_SENDER = "ashok@acme.example"
_AGENT = "sandeep@example.com"


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock", agent_email=_AGENT)


def _raw_email(message_id, body, thread_id=None, **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "Ashok", "email": _SENDER},
        "to": [{"name": "Sandeep", "email": _AGENT}],
        "subject": "Analytics Initiative",
        "body": body,
        "timestamp": "2026-09-13T10:00:00Z",
    }
    if thread_id:
        raw["thread_id"] = thread_id
    raw.update(overrides)
    return raw


class _PersonFactsLLM(LLMProvider):
    """Simulates what a real, correctly-prompted LLM (per the updated
    _ANALYSIS_INSTRUCTIONS) returns -- MockLLMProvider does no NLU at all, so a
    double is required to exercise person_facts_mentioned, exactly like every
    other structured-extraction test in this codebase (e.g.
    _AttributedCommitmentAndMeetingLLM in tests/test_thread_events.py)."""

    def __init__(
        self, person_facts_mentioned=None, people_mentioned=None, requirements=None,
        buying_signals=None, capture: list | None = None, always_verify_same_fact: bool = True,
    ):
        self._person_facts_mentioned = person_facts_mentioned or []
        self._people_mentioned = people_mentioned if people_mentioned is not None else [
            {"name": "Ashok", "email": _SENDER, "org": None, "role_hint": None}
        ]
        self._requirements = requirements or []
        self._buying_signals = buying_signals or []
        self._capture = capture
        self._always_verify_same_fact = always_verify_same_fact

    def analyze_email(self, email, thread_history=None, person_context=None):
        if self._capture is not None:
            self._capture.append({"message_id": email.message_id, "person_context": person_context})
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "evaluation",
            "entities": [], "facts": [], "requirements": self._requirements, "pain_points": [],
            "buying_signals": self._buying_signals, "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [], "companies": [], "products": [],
            "people_mentioned": self._people_mentioned, "projects_mentioned": [], "commitments_mentioned": [],
            "meetings_mentioned": [], "personal_items_mentioned": [],
            "person_facts_mentioned": self._person_facts_mentioned,
            "goal_pillar": "Sales", "label_applied": "1. Read only",
        }

    def update_context(self, previous_context, new_analysis):
        delta: dict[str, Any] = {}
        if new_analysis.get("requirements"):
            delta["requirements"] = {"added": [{"value": v, "basis": "stated"} for v in new_analysis["requirements"]]}
        if new_analysis.get("buying_signals"):
            delta["buying_signals"] = {"added": [{"value": v, "basis": "stated"} for v in new_analysis["buying_signals"]]}
        return delta

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return self._always_verify_same_fact

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


def _ashok_id(db):
    return PersonRepository(db).find_one({"email": _SENDER})["id"]


# --- 1/8/10: explicit role fact, canonical ids, no raw id leakage ----------------------------


def test_explicit_role_statement_becomes_person_attributed_knowledge(db, settings):
    llm = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering", "basis": "stated"}
    ])
    email = _raw_email(_RAW_1, "Ashok is VP Engineering.", thread_id=_RAW_THREAD)
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    items = KnowledgeRepository(db).find_many({"person_id": ashok_id, "predicate": "role"})
    assert len(items) == 1
    item = items[0]
    assert item["current_value"] == "VP Engineering"
    assert item["thread_id"] == "THR-001"
    assert item["source_emails"] == ["EML-001"]
    assert _RAW_1 not in json.dumps(item)
    assert _RAW_THREAD not in json.dumps(item)


# --- 2/6: subsequent email adds a preference fact to the SAME person, visible in snapshot -----


def test_subsequent_email_adds_preference_fact_to_same_person_context_snapshot(db, settings):
    email_1 = _raw_email(_RAW_1, "Ashok is VP Engineering.", thread_id=_RAW_THREAD)
    email_2 = _raw_email(
        _RAW_2, "Ashok prefers Tuesday meetings.", thread_id=_RAW_THREAD, timestamp="2026-09-14T10:00:00Z",
    )
    llm_1 = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering"}
    ])
    llm_2 = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "preference", "value": "Tuesday meetings"}
    ])
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm_1, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm_2, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    snapshot_2 = PersonContextSnapshotRepository(db).find_one({"person_id": ashok_id, "source_email_id": "EML-002"})
    categories = {e["category"] for e in snapshot_2["entries"]}
    assert "preference" in categories
    preference_entry = next(e for e in snapshot_2["entries"] if e["category"] == "preference")
    assert preference_entry["summary"] == "Tuesday meetings"
    assert preference_entry["email_id"] == "EML-002"
    assert preference_entry["thread_id"] == "THR-001"

    # Both people still resolve to the SAME canonical person (not two fragments).
    assert PersonRepository(db).find_many({"email": _SENDER}).__len__() == 1


# --- 3/5: concern attributed when explicit; a statement about ANOTHER person is not the sender's --


def test_concern_attributed_to_explicit_person_not_automatically_to_sender(db, settings):
    llm = _PersonFactsLLM(
        people_mentioned=[
            {"name": "Ashok", "email": _SENDER, "org": None, "role_hint": None},
            {"name": "Vijender", "email": "vijender@acme.example", "org": None, "role_hint": None},
        ],
        person_facts_mentioned=[
            {"person_name": "Ashok", "person_email": _SENDER, "category": "concern", "value": "the security review"},
            {"person_name": "Vijender", "person_email": "vijender@acme.example", "category": "responsibility", "value": "handles procurement"},
        ],
    )
    email = _raw_email(_RAW_1, "Ashok is concerned about the security review. Vijender handles procurement.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    vijender_id = PersonRepository(db).find_one({"email": "vijender@acme.example"})["id"]
    assert ashok_id != vijender_id

    ashok_concern = KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "concern"})
    assert ashok_concern["current_value"] == "the security review"

    vijender_responsibility = KnowledgeRepository(db).find_one({"person_id": vijender_id, "predicate": "responsibility"})
    assert vijender_responsibility["current_value"] == "handles procurement"

    # The responsibility fact about Vijender must NEVER end up attributed to Ashok.
    assert KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "responsibility"}) is None


# --- 4: a thread-level statement with no person attribution stays thread-scoped ---------------


def test_unattributed_thread_level_signal_remains_thread_scoped_not_person_scoped(db, settings):
    llm = _PersonFactsLLM(requirements=["pilot completed before October"], person_facts_mentioned=[])
    email = _raw_email(_RAW_1, "We need the pilot completed before October.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    thread_level_item = KnowledgeRepository(db).find_one({"predicate": "requires"})
    assert thread_level_item is not None
    assert thread_level_item["current_value"] == "pilot completed before October"
    assert thread_level_item.get("person_id") is None  # never guessed onto the sender
    assert KnowledgeRepository(db).find_one({"person_id": ashok_id}) is None


# --- 7: subsequent LLM reasoning actually receives the person-attributed facts ----------------


def test_subsequent_llm_call_receives_person_attributed_facts_in_bounded_context(db, settings):
    capture: list = []
    email_1 = _raw_email(_RAW_1, "Ashok is VP Engineering.", thread_id=_RAW_THREAD)
    email_2 = _raw_email(
        _RAW_2, "Following up.", thread_id=_RAW_THREAD, timestamp="2026-09-14T10:00:00Z",
    )
    llm_1 = _PersonFactsLLM(
        person_facts_mentioned=[{"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering"}],
        capture=capture,
    )
    llm_2 = _PersonFactsLLM(capture=capture)  # no new facts -- proves the role fact PERSISTS, not just echoed back
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm_1, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm_2, MockCalendarProvider(), settings)

    assert len(capture) == 2
    person_context_for_email_2 = capture[1]["person_context"]
    assert person_context_for_email_2 is not None
    knowledge_values = [k["current_value"] for k in person_context_for_email_2["knowledge"]]
    assert "VP Engineering" in knowledge_values
    role_item = next(k for k in person_context_for_email_2["knowledge"] if k["predicate"] == "role")
    assert role_item["basis"] == "stated"

    # And it renders into the actual prompt text the LLM would see.
    prompt_text = format_person_context(person_context_for_email_2)
    assert "role" in prompt_text
    assert "VP Engineering" in prompt_text


# --- 9: changing a fact updates KnowledgeItem history correctly (reused mechanism) ------------


def test_changing_a_person_fact_updates_history_via_existing_mechanism(db, settings):
    email_1 = _raw_email(_RAW_1, "Ashok prefers Tuesday meetings.", thread_id=_RAW_THREAD)
    email_2 = _raw_email(
        _RAW_2, "Ashok now prefers Wednesday meetings.", thread_id=_RAW_THREAD, timestamp="2026-09-14T10:00:00Z",
    )
    llm_1 = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "preference", "value": "Tuesday meetings"}
    ])
    llm_2 = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "preference", "value": "Wednesday meetings"}
    ])
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm_1, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm_2, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    items = KnowledgeRepository(db).find_many({"person_id": ashok_id, "predicate": "preference"})
    assert len(items) == 1  # same fact, updated -- not a second, competing item
    item = items[0]
    assert item["current_value"] == "Wednesday meetings"
    assert len(item["history"]) == 2
    assert item["history"][0]["value"] == "Tuesday meetings"
    assert item["history"][1]["value"] == "Wednesday meetings"


# --- 11: existing structural context continues to work alongside semantic knowledge -----------


def test_existing_structural_context_continues_to_work_alongside_semantic_knowledge(db, settings):
    llm = _PersonFactsLLM(
        person_facts_mentioned=[{"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering"}],
    )
    email = _raw_email(_RAW_1, "Ashok is VP Engineering.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    snapshot = PersonContextSnapshotRepository(db).find_one({"person_id": ashok_id})
    categories = {e["category"] for e in snapshot["entries"]}
    assert "email_interaction" in categories
    assert "role" in categories


# --- 12/13/14: context_enriched event + real delta metadata + no fabricated deltas ------------


def test_context_enriched_event_emitted_with_real_changes_and_no_fabricated_deltas(db, settings):
    llm = _PersonFactsLLM(requirements=["pilot completed before October"])
    email = _raw_email(_RAW_1, "We need the pilot completed before October.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    context_events = [e for e in trail if e["event_type"] == "context_enriched"]
    assert len(context_events) == 1
    event = context_events[0]
    assert event["entity_type"] == "thread_context"
    assert event["operation"] == "enriched"
    assert "requirements" in event["metadata"]["changes"][0]["field"] or any(
        c["field"] == "requirements" for c in event["metadata"]["changes"]
    )

    # No fabricated deltas: a "created" person event has no delta key at all.
    person_events = [e for e in trail if e["entity_type"] == "person" and e["operation"] == "created"]
    assert len(person_events) >= 1
    assert "delta" not in person_events[0]["metadata"]


def test_person_updated_event_reports_a_real_field_level_delta(db, settings):
    email_1 = _raw_email(_RAW_1, "First message.", thread_id=_RAW_THREAD)
    email_2 = _raw_email(
        _RAW_2, "Second message.", thread_id=_RAW_THREAD, timestamp="2026-09-14T10:00:00Z",
    )
    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    ashok_id = _ashok_id(db)
    trail = get_thread_event_trail(db, "THR-001")
    updated_events = [
        e for e in trail if e["entity_type"] == "person" and e["entity_id"] == ashok_id and e["operation"] == "updated"
    ]
    assert len(updated_events) >= 1
    delta = updated_events[0]["metadata"]["delta"]
    assert "last_inbound" in delta
    assert delta["last_inbound"]["old"] is not None or delta["last_inbound"]["new"] is not None


# --- 15: retry does not duplicate person-fact knowledge, snapshots, or events -----------------


def test_retry_does_not_duplicate_person_fact_knowledge(db, settings):
    llm = _PersonFactsLLM(person_facts_mentioned=[
        {"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering"}
    ])
    email = _raw_email(_RAW_1, "Ashok is VP Engineering.")

    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)
    before_knowledge = db.knowledge_items.count_documents({})
    before_snapshots = db.person_context_snapshots.count_documents({})
    before_events = db.thread_events.count_documents({})

    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)  # retry

    assert db.knowledge_items.count_documents({}) == before_knowledge
    assert db.person_context_snapshots.count_documents({}) == before_snapshots
    assert db.thread_events.count_documents({}) == before_events


# --- Phase 11: critical quality end-to-end scenario -------------------------------------------


def test_critical_quality_scenario_semantic_profile_reaches_llm_before_email_4(db, settings):
    capture: list = []
    email_1 = _raw_email(_RAW_1, "Ashok is leading the analytics initiative at Acme.", thread_id=_RAW_THREAD)
    email_2 = _raw_email(
        _RAW_2, "Ashok wants the pilot completed before October.", thread_id=_RAW_THREAD,
        timestamp="2026-09-14T10:00:00Z",
    )
    email_3 = _raw_email(
        _RAW_3, "Ashok prefers weekly calls on Tuesday and is concerned about the security review.",
        thread_id=_RAW_THREAD, timestamp="2026-09-15T10:00:00Z",
    )
    email_4 = _raw_email(_RAW_4, "Checking in.", thread_id=_RAW_THREAD, timestamp="2026-09-16T10:00:00Z")

    llm_1 = _PersonFactsLLM(
        person_facts_mentioned=[
            {"person_name": "Ashok", "person_email": _SENDER, "category": "responsibility", "value": "leading the analytics initiative at Acme"},
        ],
        capture=capture,
    )
    llm_2 = _PersonFactsLLM(
        person_facts_mentioned=[
            {"person_name": "Ashok", "person_email": _SENDER, "category": "goal", "value": "pilot completed before October"},
        ],
        capture=capture,
    )
    llm_3 = _PersonFactsLLM(
        person_facts_mentioned=[
            {"person_name": "Ashok", "person_email": _SENDER, "category": "preference", "value": "weekly calls on Tuesday"},
            {"person_name": "Ashok", "person_email": _SENDER, "category": "concern", "value": "the security review"},
        ],
        capture=capture,
    )
    llm_4 = _PersonFactsLLM(capture=capture)

    run_pipeline(db, MockEmailProvider(payloads=[email_1]), llm_1, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), llm_2, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_3]), llm_3, MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_4]), llm_4, MockCalendarProvider(), settings)

    assert len(capture) == 4
    person_context_before_email_4 = capture[3]["person_context"]
    assert person_context_before_email_4 is not None

    knowledge = person_context_before_email_4["knowledge"]
    values_by_predicate = {k["predicate"]: k["current_value"] for k in knowledge}

    assert values_by_predicate.get("responsibility") == "leading the analytics initiative at Acme"
    assert values_by_predicate.get("goal") == "pilot completed before October"
    assert values_by_predicate.get("preference") == "weekly calls on Tuesday"
    assert values_by_predicate.get("concern") == "the security review"

    # Independently confirmed via get_bounded_person_context_for_llm computed at
    # the same point in time (not just trusting the captured kwarg).
    ashok_id = _ashok_id(db)
    recomputed = get_bounded_person_context_for_llm(db, ashok_id)
    assert recomputed["knowledge"] == person_context_before_email_4["knowledge"]

    # Source provenance: every fact traces back to a real EML/THR pair.
    for knowledge_id in [k["knowledge_id"] for k in knowledge]:
        item = KnowledgeRepository(db).find_one({"knowledge_id": knowledge_id})
        assert item["thread_id"] == "THR-001"
        assert item["source_emails"][0].startswith("EML-")


# --- Phase 12: thread trail reconstruction (email -> extracted info -> knowledge/context ------
# --- changes -> entity changes -> commitments/follow-ups -> context_enriched) -----------------


def test_thread_trail_reconstructs_the_full_ingestion_story(db, settings):
    llm = _PersonFactsLLM(
        person_facts_mentioned=[{"person_name": "Ashok", "person_email": _SENDER, "category": "role", "value": "VP Engineering"}],
        requirements=["pilot completed before October"],
    )
    email = _raw_email(_RAW_1, "Ashok is VP Engineering. We need the pilot completed before October.")
    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    trail = get_thread_event_trail(db, "THR-001")
    event_types_in_order = [e["event_type"] for e in trail]

    assert event_types_in_order[0] == "email_received"
    assert "knowledge_created" in event_types_in_order  # the role fact AND the requirement
    assert "context_enriched" in event_types_in_order  # ThreadContext picked up the requirement
    assert "person_context_enriched" in event_types_in_order
    # Deterministic ordering the whole way through.
    sequences = [e["sequence"] for e in trail]
    assert sequences == sorted(sequences)
