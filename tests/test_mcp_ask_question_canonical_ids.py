"""Closes the last app/query/* coverage gap identified during the query-layer audit:
every existing test that exercises tools.ask_question (the real, live MCP entry
point into app.query.service.execute_query) seeds MongoDB directly with hand-built,
arbitrary placeholder ids -- none run a real run_pipeline first. That leaves zero
evidence that a genuinely pipeline-produced canonical EML-nnn id (as opposed to a
raw Gmail-shaped id) flows correctly all the way through ask_question's own wire
response, at the exact call the MCP server actually exposes.

This file runs the real pipeline against a raw, Gmail-shaped message id, then calls
tools.ask_question itself (never execute_query directly), and asserts the canonical
id -- not the raw one -- is what appears in EvidenceItem.email_id (see
app/query/schemas.py and app/query/evidence.py, renamed from source_message_id
during the query-layer audit).
"""

import json

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.mcp import tools
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider

# Deliberately Gmail-shaped and unique -- if this ever leaked into the wire response
# in place of the canonical id, this exact string would be trivial to spot.
_RAW_GMAIL_MESSAGE_ID = "18f348ce69f3_raw_9c8b7a6d5e4f"


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


def _raw_sales_email():
    return {
        "message_id": _RAW_GMAIL_MESSAGE_ID,
        "from": {"name": "John", "email": "john@customerco.example"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Project Alpha proposal",
        "body": "I will send the proposal tomorrow. Let's move forward on pricing.",
        "timestamp": "2026-09-13T10:30:00Z",
    }


def test_ask_question_commitments_evidence_email_id_is_canonical_not_raw_gmail_id(db, settings):
    run_pipeline(db, MockEmailProvider(payloads=[_raw_sales_email()]), MockLLMProvider(), MockCalendarProvider(), settings)

    result = tools.ask_question(db, settings, "What are my commitments?")

    assert result["status"] == "ok"
    assert result["intent"] == "commitments"
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["email_id"] == "EML-001"
    assert result["evidence"][0]["email_id"] != _RAW_GMAIL_MESSAGE_ID
    # The raw Gmail id must never appear anywhere in the wire response at all --
    # not just absent from this one field.
    assert _RAW_GMAIL_MESSAGE_ID not in json.dumps(result)


def test_ask_question_reply_drafts_evidence_email_id_is_canonical_not_raw_gmail_id(db, settings):
    email = _raw_sales_email()
    email["body"] = "Can you send me the proposal for Project Alpha?"
    run_pipeline(db, MockEmailProvider(payloads=[email]), MockLLMProvider(), MockCalendarProvider(), settings)

    result = tools.ask_question(db, settings, "Show me reply drafts.")

    assert result["status"] == "ok"
    assert result["intent"] == "reply_drafts"
    assert len(result["evidence"]) == 1
    assert result["evidence"][0]["email_id"] == "EML-001"
    assert result["evidence"][0]["email_id"] != _RAW_GMAIL_MESSAGE_ID
    assert _RAW_GMAIL_MESSAGE_ID not in json.dumps(result)
