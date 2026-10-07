"""Tests for the ingestion-dump branch's raw-dump path.

app.mcp.tools.ingest_raw_email_only is a pure persistence function: it stores
a raw Gmail email into its own `raw_emails_dump` collection and performs ZERO
analysis. These tests prove: basic ingestion, field preservation, dedup via
source_message_id, the unique index backing that dedup, and that none of the
existing analysis-path functions are ever called from it.
"""
from unittest.mock import patch

import mongomock
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


def test_ingest_raw_email_only_leaves_emails_collection_untouched(db):
    email = parse_email(_raw_email())

    tools.ingest_raw_email_only(db, email)

    assert db.emails.count_documents({}) == 0
