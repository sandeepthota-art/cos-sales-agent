import mongomock
import pytest

from app.analysis.schemas import EmailAnalysis
from app.config.settings import Settings
from app.context.models import ContextDelta
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    EmailRepository,
    OrganizationRepository,
    PersonRepository,
)
from app.email.models import parse_email
from app.mcp.tools import (
    ingest_email,
    list_processed_emails,
    mark_email_completed,
    merge_person_records,
    persist_context_delta,
    persist_email_analysis,
    preview_duplicate_person_candidates,
)


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


def _complete_via_granular_tools(db, settings, raw, analysis=None, delta=None):
    """Drives one email through the granular pipeline (ingest -> analyze -> context
    -> complete) to COMPLETED -- the same end-state these tests used to reach via the
    single-LLM-call process_email, which was removed (it depended on a server-side
    LLM_API_KEY that was unreliable in this project; every real caller had already
    moved to these granular tools instead). Returns ingest_email's own result dict.
    """
    email = parse_email(raw)
    ingest_result = ingest_email(db, email)
    if ingest_result["already_completed"]:
        return ingest_result
    message_id = ingest_result["message_id"]
    thread_id = ingest_result["thread_id"]
    analysis = analysis or EmailAnalysis(email_id=message_id, summary="", intent="")
    persist_email_analysis(db, message_id, analysis, settings)
    persist_context_delta(db, thread_id, message_id, delta or ContextDelta())
    mark_email_completed(db, message_id)
    return ingest_result


def test_list_processed_emails_returns_stored_fields_most_recent_first(db, settings):
    _complete_via_granular_tools(
        db, settings, _raw_email("msg_001", "Body one.", timestamp="2026-09-13T10:00:00Z"),
        delta=ContextDelta(summary="Body one summary."),
    )
    _complete_via_granular_tools(
        db,
        settings,
        _raw_email(
            "msg_002",
            "Body two is a fair bit longer than one hundred and fifty characters so that "
            "the preview truncation actually has something real to cut off in this test.",
            timestamp="2026-09-14T10:00:00Z",
        ),
        delta=ContextDelta(summary="Body two summary."),
    )

    results = list_processed_emails(db, limit=50)

    assert [r["message_id"] for r in results] == ["EML-002", "EML-001"]
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
        _complete_via_granular_tools(
            db, settings, _raw_email(f"msg_{i:03d}", "Body.", timestamp=f"2026-09-{13 + i:02d}T10:00:00Z")
        )

    results = list_processed_emails(db, limit=2)

    assert len(results) == 2
    assert [r["message_id"] for r in results] == ["EML-003", "EML-002"]


def test_list_processed_emails_includes_entity_metadata(db, settings):
    _complete_via_granular_tools(
        db,
        settings,
        _raw_email("1a08090646ebaa45", "I will send the proposal on Friday."),
        analysis=EmailAnalysis(
            email_id="EML-001", summary="", intent="",
            goal_pillar="Sales", label_applied="Needs reply: ASAP",
        ),
    )

    results = list_processed_emails(db, limit=50)

    entry = results[0]
    assert entry["goal_pillar"] == "Sales"
    assert entry["label_applied"] == "Needs reply: ASAP"
    # John (sender) + the operator's own dedicated profile (Ashok, settings.agent_email).
    assert len(entry["entities_referenced"]["people"]) == 2


def test_list_processed_emails_defaults_entity_fields_when_email_never_reached_that_stage(db, settings):
    # An email that's only been ingest_email'd (or any pre-existing email document
    # from before entity metadata existed) has none of the 4 entity-metadata keys.
    # Direct key access on any of them would raise KeyError and crash the whole tool
    # call, hiding every email, not just this one.
    ingest_email(db, parse_email(_raw_email("msg_001", "Some body text.")))

    results = list_processed_emails(db, limit=50)

    assert len(results) == 1
    entry = results[0]
    assert entry["message_id"] == "EML-001"
    assert entry["processing_status"]["stage"] == "THREADED"
    assert entry["goal_pillar"] is None
    assert entry["label_applied"] is None
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
    # no record_id/date/etc.
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
    _complete_via_granular_tools(db, settings, _raw_email("msg_processed", "Body."))

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
    _complete_via_granular_tools(
        db, settings, _raw_email("msg_processed", "Body.", timestamp="2026-09-15T10:00:00Z")
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "msg_raw"},
        _raw_only_email("msg_raw", timestamp="2026-09-10T09:00:00Z"),
    )

    results = list_processed_emails(db, limit=50)

    assert len(results) == 2
    by_id = {r["message_id"]: r for r in results}
    assert by_id["EML-001"]["processing_status"]["stage"] == "COMPLETED"
    assert by_id["msg_raw"]["processing_status"] == {"stage": None, "error": None}


def test_list_processed_emails_skips_a_malformed_stub_document_instead_of_crashing(db, settings):
    # Real-world case: an interrupted/retried ingest_email call can leave behind a
    # document with only message_id + processing_status -- no subject/body/from/
    # to/timestamp at all. That was never a genuinely processed email, so it's
    # skipped entirely rather than shown with fabricated placeholder values for
    # the fields it's missing.
    EmailRepository(db).upsert_by_key(
        {"message_id": "stub_from_failed_retry"},
        {"message_id": "stub_from_failed_retry",
         "processing_status": {"stage": "CONTEXT_BUILT", "error": None, "failed_stage": None,
                                "updated_at": "2026-09-13T10:00:00Z"}},
    )
    _complete_via_granular_tools(db, settings, _raw_email("msg_processed", "Body."))

    results = list_processed_emails(db, limit=50)

    assert [r["message_id"] for r in results] == ["EML-001"]


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


# --- merge_person_records (direct, caller-confirmed merge -- no plan/approval gate) -----


def test_merge_person_records_repoints_a_commitment_and_retires_the_duplicate(db):
    _seed_duplicate_pair(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "CMT-1"},
        {"id": "CMT-1", "what": "Send pricing", "person_id": "PER-390", "status": "open"},
    )

    result = merge_person_records(db, "PER-390", "PER-391", "Same person, confirmed from email signature")

    assert result["status"] == "COMPLETED"
    assert result["updated_counts"]["commitments"] == 1
    commitment = CommitmentRepository(db).find_one({"id": "CMT-1"})
    assert commitment["person_id"] == "PER-391"
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    assert duplicate["status"] == "merged"
    assert duplicate["merged_into"] == "PER-391"
    assert duplicate["merge_reason"] == "Same person, confirmed from email signature"
    assert duplicate["merged_at"]


def test_merge_person_records_repoints_entities_referenced_on_emails(db):
    _seed_duplicate_pair(db)
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        {
            "message_id": "EML-001", "subject": "Hi",
            "entities_referenced": {"people": ["PER-390"], "projects": []},
        },
    )

    result = merge_person_records(db, "PER-390", "PER-391", "Same person")

    assert result["updated_counts"]["emails"] == 1
    email = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert email["entities_referenced"]["people"] == ["PER-391"]


def test_merge_person_records_raises_for_unknown_duplicate_id(db):
    _seed_duplicate_pair(db)
    with pytest.raises(ValueError, match="PER-999"):
        merge_person_records(db, "PER-999", "PER-391", "reason")


def test_merge_person_records_raises_for_unknown_canonical_id(db):
    _seed_duplicate_pair(db)
    with pytest.raises(ValueError, match="PER-999"):
        merge_person_records(db, "PER-390", "PER-999", "reason")


def test_merge_person_records_raises_when_ids_are_the_same(db):
    _seed_duplicate_pair(db)
    with pytest.raises(ValueError, match="must be different"):
        merge_person_records(db, "PER-390", "PER-390", "reason")


def test_merge_person_records_raises_when_duplicate_already_merged(db):
    _seed_duplicate_pair(db)
    merge_person_records(db, "PER-390", "PER-391", "first merge")

    with pytest.raises(ValueError, match="already merged"):
        merge_person_records(db, "PER-390", "PER-391", "second attempt")


def test_merge_person_records_blocks_on_external_calendar_attendee_conflict(db):
    _seed_duplicate_pair(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "person_id": "PER-390",
            "org_id": None, "meeting_id": None, "event": {"attendees": ["external@customer.com"]},
        },
    )

    result = merge_person_records(db, "PER-390", "PER-391", "reason")

    assert result["status"] == "BLOCKED"
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    assert duplicate["status"] == "active"
    assert duplicate.get("merged_into") is None
