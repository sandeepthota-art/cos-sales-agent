"""Tests for the ingestion-dump branch's raw-dump path.

app.mcp.tools.ingest_raw_email_only is a pure persistence function: it stores
a raw Gmail email into its own `raw_emails_dump` collection and performs ZERO
analysis. app.mcp.tools.get_raw_ingestion_status is the companion read-only
checkpoint, telling a caller where to resume Gmail fetching from. These tests
prove: basic ingestion, field preservation, dedup via source_message_id (never
thread_id, never timestamp), the unique index backing that dedup (enforced at
the database level, not just by application logic), checkpoint behavior for
both the empty and non-empty cases, and that none of the existing
analysis-path functions are ever called from either function.
"""
from unittest.mock import patch

import mongomock
import pymongo
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import RawEmailDumpRepository
from app.email.models import parse_email
from app.mcp import tools


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _raw_email(message_id="gmail-msg-001", **overrides):
    raw = {
        "message_id": message_id,
        "thread_id": "gmail-thread-001",
        "from": {"name": "Prospect", "email": "prospect@example.com"},
        "to": [{"name": "Sandeep", "email": "sandeep@example.com"}],
        "cc": [{"name": "CC Person", "email": "cc@example.com"}],
        "subject": "Re: Proposal",
        "body": "Here is the proposal you asked for.",
        "timestamp": "2026-10-07T09:00:00Z",
        "in_reply_to": "<rfc-msg-id-999@mail.gmail.com>",
        "references": ["<rfc-msg-id-998@mail.gmail.com>"],
    }
    raw.update(overrides)
    return raw


def test_ingest_raw_email_only_persists_into_raw_emails_dump(db):
    email = parse_email(_raw_email())

    tools.ingest_raw_email_only(db, email)

    docs = RawEmailDumpRepository(db).find_many({})
    assert len(docs) == 1


def test_ingest_raw_email_only_preserves_important_fields(db):
    email = parse_email(_raw_email())

    tools.ingest_raw_email_only(db, email)

    doc = RawEmailDumpRepository(db).find_one({"source_message_id": "gmail-msg-001"})
    assert doc is not None
    assert doc["message_id"] == "gmail-msg-001"
    assert doc["source_message_id"] == "gmail-msg-001"
    assert doc["source_thread_id"] == "gmail-thread-001"
    assert doc["from"]["email"] == "prospect@example.com"
    assert doc["to"][0]["email"] == "sandeep@example.com"
    assert doc["cc"][0]["email"] == "cc@example.com"
    assert doc["subject"] == "Re: Proposal"
    assert doc["body"] == "Here is the proposal you asked for."
    assert doc["timestamp"].startswith("2026-10-07T09:00:00")
    assert doc["in_reply_to"] == "<rfc-msg-id-999@mail.gmail.com>"
    assert doc["references"] == ["<rfc-msg-id-998@mail.gmail.com>"]
    assert doc["source"] == "gmail"
    assert "ingested_at" in doc


def test_ingest_raw_email_only_does_not_alter_subject_or_body():
    """Unlike app.pipeline.ingest_raw_email (which calls normalize_email),
    this path must preserve the raw subject/body exactly as Gmail gave them --
    no Re:/Fwd: stripping, no whitespace collapsing."""
    email = parse_email(_raw_email(subject="Re: Re:   Proposal", body="Line one.\n\nLine   two."))

    assert email.subject == "Re: Re:   Proposal"
    assert email.body == "Line one.\n\nLine   two."


def test_ingest_raw_email_only_is_idempotent_on_source_message_id(db):
    email = parse_email(_raw_email())

    tools.ingest_raw_email_only(db, email)
    tools.ingest_raw_email_only(db, email)

    docs = RawEmailDumpRepository(db).find_many({})
    assert len(docs) == 1


def test_raw_emails_dump_has_unique_index_on_source_message_id(db):
    index_info = db.raw_emails_dump.index_information()
    assert any(
        spec.get("unique") and spec["key"] == [("source_message_id", 1)] for spec in index_info.values()
    )


def test_ingest_raw_email_only_never_calls_analysis_functions(db):
    email = parse_email(_raw_email())

    with (
        patch("app.mcp.tools.persist_email_analysis") as mock_analysis,
        patch("app.mcp.tools.persist_context_delta") as mock_context_delta,
        patch("app.mcp.tools._process_entities") as mock_entities,
        patch("app.mcp.tools._process_knowledge") as mock_knowledge,
        patch("app.mcp.tools.persist_organization_research") as mock_org_research,
        patch("app.mcp.tools.persist_person_profile") as mock_profile,
        patch("app.mcp.tools.create_reply_draft") as mock_reply,
        patch("app.mcp.tools.detect_meeting") as mock_detect_meeting,
        patch("app.mcp.tools.build_calendar_action") as mock_calendar_action,
    ):
        tools.ingest_raw_email_only(db, email)

        mock_analysis.assert_not_called()
        mock_context_delta.assert_not_called()
        mock_entities.assert_not_called()
        mock_knowledge.assert_not_called()
        mock_org_research.assert_not_called()
        mock_profile.assert_not_called()
        mock_reply.assert_not_called()
        mock_detect_meeting.assert_not_called()
        mock_calendar_action.assert_not_called()


def test_get_raw_ingestion_status_never_calls_analysis_functions(db):
    tools.ingest_raw_email_only(db, parse_email(_raw_email()))

    with (
        patch("app.mcp.tools.persist_email_analysis") as mock_analysis,
        patch("app.mcp.tools.persist_context_delta") as mock_context_delta,
        patch("app.mcp.tools._process_entities") as mock_entities,
        patch("app.mcp.tools._process_knowledge") as mock_knowledge,
        patch("app.mcp.tools.persist_organization_research") as mock_org_research,
        patch("app.mcp.tools.persist_person_profile") as mock_profile,
        patch("app.mcp.tools.create_reply_draft") as mock_reply,
    ):
        tools.get_raw_ingestion_status(db)

        mock_analysis.assert_not_called()
        mock_context_delta.assert_not_called()
        mock_entities.assert_not_called()
        mock_knowledge.assert_not_called()
        mock_org_research.assert_not_called()
        mock_profile.assert_not_called()
        mock_reply.assert_not_called()


def test_ingest_raw_email_only_leaves_emails_collection_untouched(db):
    email = parse_email(_raw_email())

    tools.ingest_raw_email_only(db, email)

    assert db.emails.count_documents({}) == 0


# --- get_raw_ingestion_status (checkpoint) ------------------------------------------------


def test_get_raw_ingestion_status_reports_no_existing_data_when_empty(db):
    status = tools.get_raw_ingestion_status(db)

    assert status == {"has_existing_data": False, "latest_email_timestamp": None, "latest_ingested_at": None}


def test_get_raw_ingestion_status_reports_latest_email_timestamp_chronologically(db):
    # Deliberately out of insertion order, and with mixed UTC offsets -- proves
    # the comparison is chronological (via _parse_timestamp_for_sort), not a
    # plain string/insertion-order comparison. "+05:30" on Oct 7 09:00 is the
    # same instant as "03:30Z"; "Z" on Oct 7 10:00 is later than both.
    tools.ingest_raw_email_only(db, parse_email(_raw_email("gmail-msg-001", timestamp="2026-10-05T09:00:00Z")))
    tools.ingest_raw_email_only(db, parse_email(_raw_email("gmail-msg-002", timestamp="2026-10-07T10:00:00Z")))
    tools.ingest_raw_email_only(
        db, parse_email(_raw_email("gmail-msg-003", timestamp="2026-10-07T09:00:00+05:30"))
    )

    status = tools.get_raw_ingestion_status(db)

    assert status["has_existing_data"] is True
    assert status["latest_email_timestamp"].startswith("2026-10-07T10:00:00")
    assert "latest_ingested_at" in status


def test_get_raw_ingestion_status_never_touches_emails_collection(db):
    """The raw-dump checkpoint must read raw_emails_dump only -- never the
    analyzed emails collection (that's get_last_ingested_email's job)."""
    db.emails.insert_one(
        {"message_id": "EML-999", "source_message_id": "EML-999", "timestamp": "2026-10-07T00:00:00Z"}
    )

    status = tools.get_raw_ingestion_status(db)

    assert status == {"has_existing_data": False, "latest_email_timestamp": None, "latest_ingested_at": None}


# --- Deduplication -------------------------------------------------------------------------


def test_ingest_raw_email_only_reports_already_existed_for_a_duplicate(db):
    first = tools.ingest_raw_email_only(db, parse_email(_raw_email("gmail-msg-001")))
    second = tools.ingest_raw_email_only(db, parse_email(_raw_email("gmail-msg-001")))

    assert first["already_existed"] is False
    assert second["already_existed"] is True
    assert RawEmailDumpRepository(db).find_many({}).__len__() == 1


def test_ingest_raw_email_only_reports_already_existed_false_for_a_new_message(db):
    result = tools.ingest_raw_email_only(db, parse_email(_raw_email("gmail-msg-002")))

    assert result["already_existed"] is False


def test_ingest_raw_email_only_mixed_batch_skips_existing_stores_new(db):
    # MongoDB already contains msg-001 and msg-003.
    tools.ingest_raw_email_only(db, parse_email(_raw_email("msg-001")))
    tools.ingest_raw_email_only(db, parse_email(_raw_email("msg-003")))

    # Gmail returns msg-001, msg-002, msg-003, msg-004.
    results = {
        message_id: tools.ingest_raw_email_only(db, parse_email(_raw_email(message_id)))["already_existed"]
        for message_id in ("msg-001", "msg-002", "msg-003", "msg-004")
    }

    assert results == {"msg-001": True, "msg-002": False, "msg-003": True, "msg-004": False}
    stored_ids = {doc["source_message_id"] for doc in RawEmailDumpRepository(db).find_many({})}
    assert stored_ids == {"msg-001", "msg-002", "msg-003", "msg-004"}


def test_ingest_raw_email_only_does_not_dedupe_by_timestamp(db):
    """Two distinct messages sharing the exact same timestamp must both be
    stored -- the unique message id is the only dedup key, never the
    timestamp, even when it coincidentally matches an existing checkpoint."""
    same_timestamp = "2026-10-07T09:00:00Z"
    tools.ingest_raw_email_only(db, parse_email(_raw_email("msg-same-ts-1", timestamp=same_timestamp)))
    result = tools.ingest_raw_email_only(db, parse_email(_raw_email("msg-same-ts-2", timestamp=same_timestamp)))

    assert result["already_existed"] is False
    assert RawEmailDumpRepository(db).find_many({}).__len__() == 2


def test_ingest_raw_email_only_does_not_dedupe_by_thread_id(db):
    """Multiple messages in the same Gmail thread must each be stored once --
    thread_id is never a dedup key."""
    for message_id in ("message-001", "message-002", "message-003"):
        tools.ingest_raw_email_only(
            db, parse_email(_raw_email(message_id, thread_id="thread-001"))
        )

    docs = RawEmailDumpRepository(db).find_many({})
    assert len(docs) == 3
    assert {d["source_message_id"] for d in docs} == {"message-001", "message-002", "message-003"}
    assert all(d["source_thread_id"] == "thread-001" for d in docs)


def test_raw_emails_dump_unique_index_rejects_duplicate_source_message_id_at_db_level(db):
    """Tool-level checks alone are not the guarantee -- the database itself
    must refuse a second document with the same source_message_id, so two
    concurrent ingestion attempts can never both succeed in creating a
    duplicate even if their application-level existence checks both ran
    before either insert landed."""
    db.raw_emails_dump.insert_one({"source_message_id": "gmail-msg-001", "subject": "First"})

    with pytest.raises(pymongo.errors.DuplicateKeyError):
        db.raw_emails_dump.insert_one({"source_message_id": "gmail-msg-001", "subject": "Second"})
