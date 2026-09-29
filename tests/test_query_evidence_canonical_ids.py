"""app/query/* audit regression: EvidenceItem.email_id (renamed from
source_message_id -- see app/query/schemas.py:EvidenceItem) must carry the real,
canonically-produced EML-nnn id once a genuine run_pipeline has processed the email,
not just an arbitrary placeholder a unit test hand-seeds. No existing test in
tests/test_query_*.py invoked run_pipeline at all -- they all seed MongoDB directly
with placeholder strings ("t1", "m1") as both stored data and query args, so they
give zero evidence that a real, pipeline-produced canonical value flows correctly
into this specific field. This file closes that gap.
"""

from datetime import datetime, timezone

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider
from app.query.schemas import QueryIntentType, QueryRequest
from app.query.service import execute_query


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


def _raw_email(message_id, body, **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Project Alpha",
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    raw.update(overrides)
    return raw


def _ask(db, text):
    request = QueryRequest(text=text, reference_datetime=datetime(2026, 9, 15, tzinfo=timezone.utc), timezone="UTC")
    return execute_query(db, request)


def test_commitments_query_evidence_email_id_is_the_real_canonical_pipeline_id(db, settings):
    payloads = [_raw_email("m1", "I will send the proposal tomorrow.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    result = _ask(db, "What are my commitments?")

    assert result.intent == QueryIntentType.COMMITMENTS
    assert len(result.evidence) == 1
    assert result.evidence[0].email_id == "EML-001"


def test_reply_drafts_query_evidence_email_id_is_the_real_canonical_pipeline_id(db, settings):
    payloads = [_raw_email("m1", "Can you send me the proposal for Project Alpha?")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    result = _ask(db, "Show me reply drafts.")

    assert result.intent == QueryIntentType.REPLY_DRAFTS
    assert len(result.evidence) == 1
    assert result.evidence[0].email_id == "EML-001"
