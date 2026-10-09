"""persist_email_analysis's optional skip_knowledge flag: lets a caller run
the deterministic entity-resolution path (_process_entities) for a test batch
of emails without also writing knowledge_items -- useful for a deliberate
entities-only test run. No skill in this codebase is currently wired to use
it (context-building, the stage that calls persist_email_analysis for real,
does so without skip_knowledge on purpose, since it builds the knowledge
layer too). Default (skip_knowledge unset / False) is byte-identical to
pre-existing behavior.
"""
import mongomock
import pytest

from app.analysis.schemas import EmailAnalysis, Fact, MentionedPerson, PersonFactMention, RawCommitment
from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import CommitmentRepository
from app.email.models import parse_email
from app.mcp.tools import ingest_email, persist_email_analysis


def _raw_email(message_id, body, subject="Proposal", **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": subject,
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    raw.update(overrides)
    return raw


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(calendar_provider="mock", llm_provider="mock", agent_email="ashok@example.com", agent_name=None)


def test_skip_knowledge_true_creates_no_knowledge_items(db, settings):
    ingest_result = ingest_email(db, parse_email(_raw_email("msg_001", "Pricing is a concern for us.")))
    analysis = EmailAnalysis(
        email_id=ingest_result["message_id"], summary="s", intent="i",
        facts=[Fact(subject="deal", predicate="budget", object="50000")],
        pain_points=["pricing is a concern"],
    )

    persist_email_analysis(db, ingest_result["message_id"], analysis, settings, skip_knowledge=True)

    assert db.knowledge_items.count_documents({}) == 0


def test_skip_knowledge_true_still_resolves_entities(db, settings):
    ingest_result = ingest_email(db, parse_email(_raw_email("msg_002", "Loop in Priya on this.")))
    analysis = EmailAnalysis(
        email_id=ingest_result["message_id"], summary="s", intent="i",
        people_mentioned=[MentionedPerson(name="Priya Singh", email="priya@example.com", org="Example Co")],
        commitments_mentioned=[RawCommitment(what="send pricing", commitment_class="mine", date_phrase="Friday")],
    )

    result = persist_email_analysis(db, ingest_result["message_id"], analysis, settings, skip_knowledge=True)

    assert len(result["entities_referenced"]["people"]) == 3  # John, Priya, operator
    assert len(result["entities_referenced"]["commitments"]) == 1
    assert db.commitments.count_documents({}) == 1
    assert db.knowledge_items.count_documents({}) == 0


def test_skip_knowledge_true_skips_person_facts_too(db, settings):
    """Pins _process_entities' own _process_person_facts call specifically --
    distinct from _process_knowledge and the WORKS_AT loop, this is the third
    and last path that can write to knowledge_items, and the only one of the
    three not already exercised by a non-empty-result assertion above (a
    missing `if not skip_knowledge:` guard here would still pass every other
    test in this file, since person_facts_mentioned is empty everywhere else)."""
    ingest_result = ingest_email(db, parse_email(_raw_email("msg_005", "Priya owns the budget decision.")))
    analysis = EmailAnalysis(
        email_id=ingest_result["message_id"], summary="s", intent="i",
        people_mentioned=[MentionedPerson(name="Priya Singh", email="priya@example.com", org="Example Co")],
        person_facts_mentioned=[
            PersonFactMention(person_name="Priya Singh", person_email="priya@example.com",
                               category="role", value="owns the budget decision"),
        ],
    )

    persist_email_analysis(db, ingest_result["message_id"], analysis, settings, skip_knowledge=True)

    assert db.knowledge_items.count_documents({}) == 0


def test_skip_knowledge_false_is_the_default_and_unchanged(db, settings):
    ingest_result = ingest_email(db, parse_email(_raw_email("msg_003", "Pricing is a concern for us.")))
    analysis = EmailAnalysis(
        email_id=ingest_result["message_id"], summary="s", intent="i",
        pain_points=["pricing is a concern"],
    )

    persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    assert db.knowledge_items.count_documents({}) > 0


def test_skip_knowledge_true_still_reaches_meeting_processed_stage(db, settings):
    ingest_result = ingest_email(db, parse_email(_raw_email("msg_004", "Just checking in.")))
    analysis = EmailAnalysis(email_id=ingest_result["message_id"], summary="s", intent="i")

    persist_email_analysis(db, ingest_result["message_id"], analysis, settings, skip_knowledge=True)

    stored = db.emails.find_one({"message_id": ingest_result["message_id"]})
    assert stored["processing_status"]["stage"] == "MEETING_PROCESSED"
