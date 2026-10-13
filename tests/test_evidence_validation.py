"""Tests for app.entities.evidence_validation (adapted from origin/main's
app.context.py::_validate_delta) and its wiring into
app.pipeline._process_person_facts. PersonFactMention.evidence is optional
only at the Pydantic schema level (app.analysis.schemas) -- enforcement task:
evidence is now mandatory at the validation boundary, so a mention with no
evidence at all is rejected exactly like one with fabricated or blank
evidence; see app.entities.evidence_validation's module docstring for why
strictness lives there and not in the schema.

Unit-level tests call validate_person_fact_mention directly (pure function,
no database). Integration-level tests run the real pipeline end to end with
a double LLM provider, exactly like tests/test_person_knowledge.py's own
_PersonFactsLLM convention, to prove invalid facts never reach
knowledge_items/person_context_snapshots.
"""

from datetime import datetime, timezone
from typing import Any

import mongomock
import pytest

from app.analysis.schemas import EmailAnalysis, PersonFactMention
from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import KnowledgeRepository, PersonContextSnapshotRepository, PersonRepository
from app.email.models import parse_email
from app.entities.evidence_validation import validate_person_fact_mention
from app.interfaces.llm_provider import LLMProvider
from app.mcp.tools import ingest_email, persist_email_analysis
import app.pipeline as pipeline_module
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider

_SENDER = "ashok@acme.example"
_AGENT = "sandeep@example.com"


def _email(message_id="EML-TEST-001", thread_id="THR-TEST", body="", subject="Analytics Initiative"):
    return parse_email(
        {
            "message_id": message_id, "thread_id": thread_id,
            "from": {"name": "Ashok", "email": _SENDER},
            "to": [{"name": "Sandeep", "email": _AGENT}],
            "subject": subject, "body": body,
            "timestamp": "2026-09-13T10:00:00Z",
        }
    )


def _mention(**overrides):
    base = {
        "person_name": "Ashok", "person_email": _SENDER, "category": "role",
        "value": "VP Engineering", "basis": "stated",
    }
    base.update(overrides)
    return PersonFactMention.model_validate(base)


# --- 1: valid fact, evidence present in the correct email -------------------------------------


def test_valid_evidence_present_in_correct_email_is_accepted():
    email = _email(body="Hi team, Ashok is VP Engineering at Acme now.")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is True
    assert reason is None


# --- 2: fabricated evidence is rejected ---------------------------------------------------------


def test_fabricated_evidence_not_present_anywhere_is_rejected():
    email = _email(body="Hi team, let's sync on Tuesday.")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "evidence_not_found_in_source_email"


# --- 3: evidence taken from another email is rejected --------------------------------------------


def test_evidence_genuinely_from_a_different_email_is_rejected():
    """The quote is real -- it just isn't in THIS email. The function has no
    mechanism to check it against any other message; this proves that
    checking it against the wrong one correctly fails rather than passing by
    coincidence."""
    email_a = _email(message_id="EML-A", body="Let's sync on Tuesday about the renewal.")
    email_b_text = "Ashok is VP Engineering at Acme now."
    mention = _mention(evidence="Ashok is VP Engineering")
    assert validate_person_fact_mention(mention, _email(message_id="EML-B", body=email_b_text))[0] is True

    valid, reason = validate_person_fact_mention(mention, email_a)

    assert valid is False
    assert reason == "evidence_not_found_in_source_email"


# --- 4: empty vs missing evidence are handled distinctly -----------------------------------------


def test_whitespace_only_evidence_is_rejected():
    """A caller that DOES cite evidence but leaves it blank must never be
    treated as valid."""
    email = _email(body="Ashok is VP Engineering.")
    mention = _mention(evidence="   ")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "empty_evidence"


def test_bare_empty_string_evidence_is_rejected():
    email = _email(body="Ashok is VP Engineering.")
    mention = _mention(evidence="")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "empty_evidence"


def test_evidence_field_entirely_omitted_is_rejected():
    """Enforcement task: evidence=None (never supplied at all) is now
    rejected exactly like blank evidence -- there is no more unvalidated
    pass-through for a caller that omits the field entirely."""
    email = _email(body="Totally unrelated content.")
    mention = _mention(evidence=None)

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "missing_evidence"


# --- 5: missing source email or body is handled safely --------------------------------------------


def test_missing_source_email_is_handled_safely_not_raised():
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, None)

    assert valid is False
    assert reason == "missing_source_email"


def test_missing_body_text_is_handled_safely():
    email = _email(body="", subject="")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "missing_source_body"


def test_evidence_matched_against_subject_alone_when_body_is_empty():
    email = _email(body="", subject="Ashok is VP Engineering")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is True
    assert reason is None


# --- 6: harmless whitespace differences do not break a valid match --------------------------------


def test_whitespace_and_line_wrapping_differences_do_not_cause_valid_evidence_to_fail():
    email = _email(body="Hi team,\n\nAshok   is\nVP Engineering  at Acme.\n\nThanks.")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is True
    assert reason is None


def test_case_differences_do_not_cause_valid_evidence_to_fail():
    email = _email(body="ASHOK IS VP ENGINEERING AT ACME.")
    mention = _mention(evidence="Ashok is VP Engineering")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is True


# --- 7: negated evidence does not validate the opposite claim -------------------------------------


def test_negated_evidence_does_not_support_the_opposite_claim():
    email = _email(body="Update: the meeting was not approved by finance.")
    mention = _mention(category="other", value="The meeting was approved", evidence="the meeting was not approved")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is False
    assert reason == "negation_mismatch_heuristic"


def test_negated_evidence_DOES_support_a_value_that_echoes_the_negation():
    """The heuristic must not reject a value that correctly reports the
    negative outcome -- only a value that silently drops the negation."""
    email = _email(body="Update: the meeting was not approved by finance.")
    mention = _mention(category="other", value="meeting not approved", evidence="the meeting was not approved")

    valid, reason = validate_person_fact_mention(mention, email)

    assert valid is True


# --- 8: contradiction without a negation marker is a known, undetected limitation -----------------


def test_semantic_contradiction_without_a_negation_marker_is_a_known_limitation_not_caught():
    """Documents, rather than hides, a real gap: evidence is literally
    present and contains no negation marker, yet the claimed value
    contradicts what a human would conclude from it. Substring matching
    (and this module's narrow negation heuristic) cannot detect this --
    see the module docstring's stated limitation. This test pins the
    CURRENT, honest behavior (accepted) so a future reader sees the gap
    documented in code, not silently assumed solved."""
    email = _email(body="Finance increased the budget for the pilot this quarter.")
    mention = _mention(category="other", value="Budget was cut", evidence="Finance increased the budget")

    valid, _ = validate_person_fact_mention(mention, email)

    assert valid is True  # known limitation, not a desired outcome


# --- 9/10: end-to-end through the real pipeline --------------------------------------------------


class _EvidenceLLM(LLMProvider):
    def __init__(self, person_facts_mentioned):
        self._person_facts_mentioned = person_facts_mentioned

    def analyze_email(self, email, thread_history=None, person_context=None):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "evaluation",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": [], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [], "companies": [], "products": [],
            "people_mentioned": [{"name": "Ashok", "email": _SENDER, "org": None, "role_hint": None}],
            "projects_mentioned": [], "commitments_mentioned": [], "meetings_mentioned": [],
            "personal_items_mentioned": [], "person_facts_mentioned": self._person_facts_mentioned,
            "goal_pillar": "Sales", "label_applied": "1. Read only",
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return True

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock", agent_email=_AGENT)


def _raw_email(message_id, body, **overrides):
    raw = {
        "message_id": message_id, "from": {"name": "Ashok", "email": _SENDER},
        "to": [{"name": "Sandeep", "email": _AGENT}], "subject": "Analytics Initiative",
        "body": body, "timestamp": "2026-09-13T10:00:00Z",
    }
    raw.update(overrides)
    return raw


def test_fabricated_fact_is_never_persisted_into_knowledge_or_person_context(db, settings):
    llm = _EvidenceLLM(person_facts_mentioned=[
        {
            "person_name": "Ashok", "person_email": _SENDER, "category": "role",
            "value": "VP Engineering", "evidence": "Ashok is VP Engineering",
        }
    ])
    email = _raw_email("EML-FAB", "Let's sync on Tuesday about the renewal.")  # no such claim in this email

    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = PersonRepository(db).find_one({"email": _SENDER})["id"]
    assert KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "role"}) is None
    snapshot = PersonContextSnapshotRepository(db).find_one({"person_id": ashok_id, "source_email_id": "EML-001"})
    categories = {e["category"] for e in snapshot["entries"]} if snapshot else set()
    assert "role" not in categories


def test_mixed_batch_persists_the_evidenced_fact_and_rejects_the_one_missing_evidence(db, settings):
    """Enforcement task: a mixed batch in one email -- a fact WITH supporting
    evidence, and a fact with NO evidence field at all. Both are genuinely
    true of this email's text, but only the evidenced one may persist; the
    one missing evidence must be dropped, not silently let through as a
    'legacy caller' exception. One invalid fact must not block the other,
    valid fact in the same email."""
    llm = _EvidenceLLM(person_facts_mentioned=[
        {
            "person_name": "Ashok", "person_email": _SENDER, "category": "role",
            "value": "VP Engineering", "evidence": "Ashok is VP Engineering",
        },
        {
            "person_name": "Ashok", "person_email": _SENDER, "category": "preference",
            "value": "Tuesday meetings",  # no evidence field at all -- must now be rejected
        },
    ])
    email = _raw_email("EML-OK", "Ashok is VP Engineering and prefers Tuesday meetings.")

    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    ashok_id = PersonRepository(db).find_one({"email": _SENDER})["id"]
    role_item = KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "role"})
    preference_item = KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "preference"})
    assert role_item["current_value"] == "VP Engineering"  # evidenced fact persists
    assert preference_item is None  # fact missing evidence never reaches persistence


# --- 11: an invalid fact never reaches process_new_fact itself -----------------------------------


def test_invalid_fact_never_reaches_process_new_fact_valid_one_does(db, settings, monkeypatch):
    """Direct proof, not just an absence-of-persistence inference: spies on
    app.pipeline.process_new_fact (the single function that writes a
    person-attributed KnowledgeItem) to confirm it is called exactly once --
    for the evidenced fact -- and never for the one with fabricated
    evidence, in the same email."""
    calls: list[str] = []
    real_process_new_fact = pipeline_module.process_new_fact

    def spying_process_new_fact(*args, **kwargs):
        # predicate is the 4th positional arg in every call site's signature.
        calls.append(args[3])
        return real_process_new_fact(*args, **kwargs)

    monkeypatch.setattr(pipeline_module, "process_new_fact", spying_process_new_fact)

    llm = _EvidenceLLM(person_facts_mentioned=[
        {
            "person_name": "Ashok", "person_email": _SENDER, "category": "role",
            "value": "VP Engineering", "evidence": "Ashok is VP Engineering",
        },
        {
            "person_name": "Ashok", "person_email": _SENDER, "category": "preference",
            "value": "Tuesday meetings", "evidence": "Ashok enjoys long walks",  # fabricated, not in this email
        },
    ])
    email = _raw_email("EML-SPY", "Ashok is VP Engineering.")

    run_pipeline(db, MockEmailProvider(payloads=[email]), llm, MockCalendarProvider(), settings)

    assert "role" in calls
    assert "preference" not in calls


# --- 12: both production entry points enforce the same rule --------------------------------------


def test_persist_email_analysis_directly_enforces_evidence_validation(db, settings):
    """persist_email_analysis is the OTHER production entry point into
    _process_person_facts (distinct from run_pipeline) -- called directly
    here, bypassing run_pipeline entirely, to prove it independently
    enforces the same rule rather than relying on run_pipeline's own
    wiring."""
    raw = _raw_email("EML-DIRECT", "Let's sync on Tuesday about the renewal.")  # no such claim in this email
    ingest_result = ingest_email(db, parse_email(raw))
    analysis = EmailAnalysis(
        email_id=ingest_result["message_id"], summary="s", intent="i",
        people_mentioned=[{"name": "Ashok", "email": _SENDER, "org": None, "role_hint": None}],
        person_facts_mentioned=[
            PersonFactMention(
                person_name="Ashok", person_email=_SENDER, category="role",
                value="VP Engineering", evidence="Ashok is VP Engineering",  # fabricated, not in this email
            )
        ],
    )

    persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    ashok_id = PersonRepository(db).find_one({"email": _SENDER})["id"]
    assert KnowledgeRepository(db).find_one({"person_id": ashok_id, "predicate": "role"}) is None
