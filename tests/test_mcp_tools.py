import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository, OrganizationRepository, PersonRepository
from app.email.models import parse_email
from app.interfaces.llm_provider import LLMProvider
from app.mcp.tools import list_processed_emails, preview_duplicate_person_candidates, process_email
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider


def _raw_email(message_id, body, subject="Enterprise CRM Proposal", **overrides):
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


class _AlwaysBrokenLLM(LLMProvider):
    def analyze_email(self, email):
        return {"summary": "not enough fields"}

    def update_context(self, previous_context, new_analysis):
        raise AssertionError("should not be reached when analysis fails")

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise AssertionError

    def draft_reply(self, context, latest_email):
        raise AssertionError


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    # agent_email matches _raw_email's default "to" address -- envelope-based Person
    # resolution (app.pipeline._process_entities) must never create a Person for our
    # own mailbox, so this has to line up with the fixture data, not the class default.
    return Settings(calendar_provider="mock", llm_provider="mock", agent_email="ashok@example.com", agent_name=None)


def test_process_email_returns_draft_and_knowledge_for_buying_signal(db, settings):
    email = parse_email(
        _raw_email(
            "msg_001",
            "We currently use Salesforce but pricing has become a real pain point. "
            "Could you send over pricing?",
        )
    )

    result = process_email(db, email, MockLLMProvider(), MockCalendarProvider(), settings)

    assert result["status"] == "completed"
    assert result["error"] is None
    assert result["thread_id"] is not None
    assert result["reply_draft"] is not None
    assert result["reply_draft"]["subject"].startswith("Re:")
    assert any(item["current_value"] == "Salesforce" for item in result["knowledge"])
    assert result["calendar_proposal"] is None


def test_process_email_is_idempotent_on_replay(db, settings):
    raw = _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")

    first = process_email(db, parse_email(raw), MockLLMProvider(), MockCalendarProvider(), settings)
    second = process_email(db, parse_email(raw), MockLLMProvider(), MockCalendarProvider(), settings)

    assert first["status"] == "completed"
    assert second["status"] == "skipped"
    assert second["thread_id"] == first["thread_id"]
    assert second["knowledge"] == first["knowledge"]


def test_process_email_surfaces_meeting_proposal_without_scheduling_it(db, settings):
    email = parse_email(
        _raw_email("msg_001", "Let's meet Tuesday at 3 PM for 30 minutes to discuss pricing.")
    )
    calendar_provider = MockCalendarProvider()

    result = process_email(db, email, MockLLMProvider(), calendar_provider, settings)

    assert result["status"] == "completed"
    assert result["calendar_proposal"] is not None
    assert result["calendar_proposal"]["status"] == "awaiting_approval"
    assert calendar_provider.created_events == []


def test_process_email_returns_failed_status_with_error_on_analysis_failure(db, settings):
    email = parse_email(_raw_email("msg_001", "Some body text."))

    result = process_email(db, email, _AlwaysBrokenLLM(), MockCalendarProvider(), settings)

    assert result["status"] == "failed"
    assert result["error"] is not None
    assert result["thread_id"] is None
    assert result["knowledge"] == []
    assert result["reply_draft"] is None
    assert result["calendar_proposal"] is None


def test_process_email_returns_failed_status_when_email_limit_yields_no_result(db):
    settings = Settings(calendar_provider="mock", llm_provider="mock", email_limit=0)
    email = parse_email(_raw_email("msg_001", "Some body text."))

    result = process_email(db, email, MockLLMProvider(), MockCalendarProvider(), settings)

    assert result == {
        "status": "failed",
        "error": "pipeline produced no result for this email",
        "thread_id": None,
        "context_summary": None,
        "knowledge": [],
        "reply_draft": None,
        "calendar_proposal": None,
    }


def test_list_processed_emails_returns_stored_fields_most_recent_first(db, settings):
    process_email(
        db,
        parse_email(_raw_email("msg_001", "Body one.", timestamp="2026-09-13T10:00:00Z")),
        MockLLMProvider(),
        MockCalendarProvider(),
        settings,
    )
    process_email(
        db,
        parse_email(
            _raw_email(
                "msg_002",
                "Body two is a fair bit longer than one hundred and fifty characters so that "
                "the preview truncation actually has something real to cut off in this test.",
                timestamp="2026-09-14T10:00:00Z",
            )
        ),
        MockLLMProvider(),
        MockCalendarProvider(),
        settings,
    )

    results = list_processed_emails(db, limit=50)

    assert [r["message_id"] for r in results] == ["msg_002", "msg_001"]
    first = results[0]
    assert first["thread_id"] is not None
    assert first["from"] == {"name": "John", "email": "john@example.com"}
    assert first["to"] == [{"name": "Ashok", "email": "ashok@example.com"}]
    assert first["cc"] == []
    assert first["subject"] == "Enterprise CRM Proposal"
    assert first["timestamp"] == "2026-09-14T10:00:00Z"
    assert first["processing_status"]["stage"] == "COMPLETED"
    assert first["processing_status"]["error"] is None
    assert first["summary"]
    assert len(first["body_preview"]) <= 150
    assert first["body_preview"] == first["body_preview"][:150]


def test_list_processed_emails_respects_limit(db, settings):
    for i in range(3):
        process_email(
            db,
            parse_email(_raw_email(f"msg_{i:03d}", "Body.", timestamp=f"2026-09-{13 + i:02d}T10:00:00Z")),
            MockLLMProvider(),
            MockCalendarProvider(),
            settings,
        )

    results = list_processed_emails(db, limit=2)

    assert len(results) == 2
    assert [r["message_id"] for r in results] == ["msg_002", "msg_001"]


def test_list_processed_emails_includes_entity_metadata(db, settings):
    process_email(
        db,
        parse_email(_raw_email("1a08090646ebaa45", "I will send the proposal on Friday.")),
        MockLLMProvider(),
        MockCalendarProvider(),
        settings,
    )

    results = list_processed_emails(db, limit=50)

    entry = results[0]
    assert entry["record_id"] == "1a08090646ebaa45"
    assert entry["source_type"] == "gmail"
    assert entry["source_link"] == "https://mail.google.com/mail/u/0/#all/1a08090646ebaa45"
    assert entry["date"] == "2026-09-13"
    assert entry["goal_pillar"] == "Sales"
    assert entry["label_applied"] in {"Needs reply: ASAP", "Read only"}
    assert isinstance(entry["confidence"], float)
    # John (sender) + the operator's own dedicated profile (Ashok, settings.agent_email).
    assert len(entry["entities_referenced"]["people"]) == 2


def test_list_processed_emails_defaults_entity_fields_when_email_never_reached_that_stage(db, settings):
    # An email that fails before ENTITIES_PROCESSED (or any pre-existing email document
    # from before this feature existed) has none of the 8 entity-metadata keys. Direct
    # key access on any of them would raise KeyError and crash the whole tool call,
    # hiding every email, not just the broken one.
    process_email(
        db,
        parse_email(_raw_email("msg_001", "Some body text.")),
        _AlwaysBrokenLLM(),
        MockCalendarProvider(),
        settings,
    )

    results = list_processed_emails(db, limit=50)

    assert len(results) == 1
    entry = results[0]
    assert entry["message_id"] == "msg_001"
    assert entry["processing_status"]["stage"] == "FAILED"
    assert entry["record_id"] is None
    assert entry["source_type"] is None
    assert entry["source_link"] is None
    assert entry["date"] is None
    assert entry["goal_pillar"] is None
    assert entry["label_applied"] is None
    assert entry["confidence"] is None
    assert entry["entities_referenced"] == {
        "people": [],
        "projects": [],
        "commitments": [],
        "follow_ups": [],
        "meetings": [],
        "personal": [],
    }


# --- Regression tests: list_processed_emails must not crash on emails inserted by
# --- --mode=raw-file, which never write a processing_status field at all.


def _raw_only_email(message_id, **overrides):
    # Shape mirrors what app.raw_ingestion.run_raw_file_ingestion actually stores:
    # a plain Email.model_dump() with no processing_status, no entities_referenced,
    # no record_id/source_type/etc.
    doc = {
        "message_id": message_id,
        "thread_id": message_id,
        "from": {"name": "Jane", "email": "jane@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "cc": [],
        "subject": "Raw-only email",
        "body": "This email was only raw-ingested, never processed.",
        "timestamp": "2026-09-10T09:00:00Z",
        "labels": [],
    }
    doc.update(overrides)
    return doc


def test_list_processed_emails_does_not_crash_on_processed_email_with_processing_status(db, settings):
    process_email(
        db,
        parse_email(_raw_email("msg_processed", "Body.")),
        MockLLMProvider(),
        MockCalendarProvider(),
        settings,
    )

    results = list_processed_emails(db, limit=50)

    assert len(results) == 1
    assert results[0]["processing_status"]["stage"] == "COMPLETED"
    assert results[0]["processing_status"]["error"] is None


def test_list_processed_emails_does_not_crash_on_raw_email_without_processing_status(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_raw"}, _raw_only_email("msg_raw")
    )

    results = list_processed_emails(db, limit=50)

    assert len(results) == 1
    assert results[0]["message_id"] == "msg_raw"
    assert results[0]["processing_status"] == {"stage": None, "error": None}


def test_list_processed_emails_handles_mixed_raw_and_processed_emails_without_crashing(db, settings):
    process_email(
        db,
        parse_email(_raw_email("msg_processed", "Body.", timestamp="2026-09-15T10:00:00Z")),
        MockLLMProvider(),
        MockCalendarProvider(),
        settings,
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_raw"},
        _raw_only_email("msg_raw", timestamp="2026-09-10T09:00:00Z"),
    )

    results = list_processed_emails(db, limit=50)

    assert len(results) == 2
    by_id = {r["message_id"]: r for r in results}
    assert by_id["msg_processed"]["processing_status"]["stage"] == "COMPLETED"
    assert by_id["msg_raw"]["processing_status"] == {"stage": None, "error": None}


# --- preview_duplicate_person_candidates (Task 2: read-only safety-net report) ---------


def _person(**overrides):
    doc = {
        "id": "PER-391", "name": "Ashok Ganapam", "email": "ashok@databeat.io", "aliases": [],
        "org": "DataBeat", "org_id": "ORG-001", "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": ["thread_anchor"], "note_link": None,
        "review_flag": False, "source": "gmail", "status": "active", "merged_into": None,
    }
    doc.update(overrides)
    return doc


def _seed_duplicate_pair(db, duplicate_id="PER-390", canonical_id="PER-391", shared_thread="thread_anchor"):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"},
    )
    PersonRepository(db).upsert_by_key(
        {"id": canonical_id}, _person(id=canonical_id, open_threads=[shared_thread])
    )
    PersonRepository(db).upsert_by_key(
        {"id": duplicate_id},
        _person(id=duplicate_id, name="Ashok Ganapam", email=None, org_id=None, open_threads=[shared_thread]),
    )


def test_preview_duplicate_person_candidates_reports_a_safe_to_review_pair(db):
    _seed_duplicate_pair(db)

    result = preview_duplicate_person_candidates(db)

    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["duplicate_person_id"] == "PER-390"
    assert candidate["canonical_person_id"] == "PER-391"
    assert candidate["confidence"]
    assert candidate["evidence"]
    assert isinstance(candidate["downstream_records_affected"], int)


def test_preview_duplicate_person_candidates_never_writes_to_the_database(db):
    _seed_duplicate_pair(db)

    preview_duplicate_person_candidates(db)

    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    canonical = PersonRepository(db).find_one({"id": "PER-391"})
    assert duplicate["status"] == "active"
    assert duplicate["merged_into"] is None
    assert canonical["status"] == "active"


def test_preview_duplicate_person_candidates_is_empty_when_no_duplicates_exist(db):
    result = preview_duplicate_person_candidates(db)

    assert result == {"candidate_count": 0, "candidates": []}
