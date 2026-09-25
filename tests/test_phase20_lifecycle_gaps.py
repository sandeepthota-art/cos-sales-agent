# tests/test_phase20_lifecycle_gaps.py
"""Phase 20 found, and Phase 20.1 fixed, a canonical-resolution boundary gap:
app.pipeline.run_pipeline and app.mcp.tools (ingest_email/create_reply_draft)
resolved a reply draft's or calendar action's `person_id` via a raw
`PersonRepository(db).find_one({"email": ...})` lookup, which did NOT go through
app.entities.resolution.resolve_person's lifecycle-aware redirect (Phase 19.1).
A Person's own canonical resolution was already correctly merge-aware; the
reply-draft/calendar-action linking was not.

Phase 20.1's fix: both call sites now go through the new, shared
app.entities.resolution.resolve_canonical_person_for_email, which reuses the exact
same lifecycle redirect (_reuse_target) resolve_person's own email branch uses --
not a second, competing identity-resolution implementation. These tests, originally
`xfail(strict=True)` pins of the gap, now assert the fixed, correct behavior
directly. No Atlas data is touched -- mongomock only.
"""
import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import CalendarActionRepository, PersonRepository, ReplyDraftRepository
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


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


def _seed_merged_pair(db, duplicate_email: str, canonical_email: str) -> tuple[str, str]:
    """Mirrors exactly what a real anchor-anchor consolidation (see
    app.duplicate_consolidation) leaves behind: an ACTIVE canonical Person with its
    own real email, and a retired duplicate whose OWN email is left untouched and
    still exact-matches future mail from that address.
    """
    canonical_id, duplicate_id = "PER-CANON", "PER-DUP"
    PersonRepository(db).upsert_by_key({"id": canonical_id}, _person(id=canonical_id, email=canonical_email))
    PersonRepository(db).upsert_by_key(
        {"id": duplicate_id},
        _person(id=duplicate_id, email=duplicate_email, status="merged", merged_into=canonical_id),
    )
    return canonical_id, duplicate_id


def test_person_resolution_itself_correctly_redirects_merged_sender(db, settings):
    """Control/contrast case: proves app.entities.resolution.resolve_person (used
    inside app.pipeline._process_entities) is lifecycle-aware, independent of the
    reply-draft/calendar-action fix verified below.
    """
    canonical_id, duplicate_id = _seed_merged_pair(db, duplicate_email="john@example.com", canonical_email="john.official@example.com")
    payloads = [{
        "message_id": "msg_001", "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": settings.agent_email}], "subject": "Enterprise CRM Proposal",
        "body": "We currently use Salesforce but pricing is a pain point.", "timestamp": "2026-09-13T10:30:00Z",
    }]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored_email = db.emails.find_one({"message_id": "msg_001"})
    assert canonical_id in stored_email["entities_referenced"]["people"]
    assert duplicate_id not in stored_email["entities_referenced"]["people"]


def test_reply_draft_person_id_resolves_to_canonical_not_merged_sender(db, settings):
    canonical_id, duplicate_id = _seed_merged_pair(db, duplicate_email="john@example.com", canonical_email="john.official@example.com")
    payloads = [{
        "message_id": "msg_001", "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": settings.agent_email}], "subject": "Enterprise CRM Proposal",
        "body": "We currently use Salesforce but pricing is a pain point.", "timestamp": "2026-09-13T10:30:00Z",
    }]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft is not None
    assert draft["person_id"] == canonical_id
    assert draft["person_id"] != duplicate_id


def test_calendar_action_person_id_resolves_to_canonical_not_merged_sender(db, settings):
    canonical_id, duplicate_id = _seed_merged_pair(db, duplicate_email="john@example.com", canonical_email="john.official@example.com")
    payloads = [{
        "message_id": "msg_001", "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": settings.agent_email}], "subject": "Enterprise CRM Proposal",
        "body": "Let's schedule a call soon.", "timestamp": "2026-09-13T10:30:00Z",
    }]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    action = CalendarActionRepository(db).find_one({"thread_id": "thread_msg_001"})
    assert action is not None
    assert action["person_id"] == canonical_id
    assert action["person_id"] != duplicate_id
    # Calendar safety is completely independent of this fix -- attendees must still
    # be empty regardless of which person_id ends up stored.
    assert action["event"]["attendees"] == []
