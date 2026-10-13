"""app.context.claim_next_thread_for_context -- synthetic data only."""

from datetime import datetime, timezone

import mongomock
import pytest

from app import context, tools
from app.config import Settings
from app.context_config import MAX_BODY_CHARS_PER_MESSAGE, MAX_MESSAGES_PER_THREAD_CLAIM
from app.db import RawEmailDumpRepository, initialize_indexes
from app.models import parse_email


@pytest.fixture
def db():
    database = mongomock.MongoClient().db
    initialize_indexes(database)
    return database


def _settings(**overrides):
    defaults = {"agent_email": "agent@ourcompany.example"}
    defaults.update(overrides)
    return Settings(**defaults)


def _email_doc(message_id: str, **overrides):
    doc = {
        "message_id": message_id,
        "from": {"name": "Jane", "email": "jane@customerco.example"},
        "to": [{"name": "Agent", "email": "agent@ourcompany.example"}],
        "subject": "Renewal pricing",
        "body": "Can we discuss renewal pricing for next quarter?",
        "timestamp": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _ingest(db, settings, message_id: str, **overrides) -> dict:
    return tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id, **overrides)), settings)


def test_claims_oldest_thread_first(db):
    settings = _settings()
    _ingest(db, settings, "MSG-2", thread_id="T-2", timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))

    result = context.claim_next_thread_for_context(db)

    assert result["thread_id"] == "T-1"


def test_claims_all_unprocessed_messages_of_thread_with_one_shared_claim_id(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, 9, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-2", thread_id="T-1", timestamp=datetime(2026, 8, 1, 10, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-3", thread_id="T-1", timestamp=datetime(2026, 8, 1, 11, tzinfo=timezone.utc))

    result = context.claim_next_thread_for_context(db)

    assert [m["message_id"] for m in result["messages"]] == ["MSG-1", "MSG-2", "MSG-3"]
    claimed_docs = list(db.raw_emails_dump.find({"thread_id": "T-1"}))
    assert len({d["context_claim_id"] for d in claimed_docs}) == 1
    assert claimed_docs[0]["context_claim_id"] == result["claim_id"]


def test_concurrent_claimers_get_disjoint_threads(db):
    settings = _settings()
    _ingest(db, settings, "MSG-A", thread_id="T-A", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-B", thread_id="T-B", timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc))

    first = context.claim_next_thread_for_context(db)
    second = context.claim_next_thread_for_context(db)

    assert first["thread_id"] != second["thread_id"]
    assert {first["thread_id"], second["thread_id"]} == {"T-A", "T-B"}


def test_history_only_messages_are_eligible_for_context(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    RawEmailDumpRepository(db).update_many_by_key({"message_id": "MSG-OLD"}, {"history_only": True})

    result = context.claim_next_thread_for_context(db)

    assert result["empty"] is False
    assert result["messages"][0]["message_id"] == "MSG-OLD"


def test_claim_never_touches_labeling_fields(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))

    context.claim_next_thread_for_context(db)

    doc = db.raw_emails_dump.find_one({"message_id": "MSG-1"})
    assert doc["label_applied"] is None
    assert doc["classification_version"] == 0
    assert doc["claim_id"] is None
    assert doc["label_claimed_at"] == ""


def test_caps_at_max_messages_per_thread_claim(db):
    settings = _settings()
    for i in range(MAX_MESSAGES_PER_THREAD_CLAIM + 5):
        _ingest(db, settings, f"MSG-{i}", thread_id="T-BIG", timestamp=datetime(2026, 8, 1, 0, i, tzinfo=timezone.utc))

    result = context.claim_next_thread_for_context(db)

    assert len(result["messages"]) == MAX_MESSAGES_PER_THREAD_CLAIM


def test_truncation_flag_set_for_long_body(db):
    settings = _settings()
    long_body = "x" * (MAX_BODY_CHARS_PER_MESSAGE + 500)
    _ingest(db, settings, "MSG-LONG", thread_id="T-1", body=long_body, timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))

    result = context.claim_next_thread_for_context(db)

    message = result["messages"][0]
    assert message["truncated"] is True
    assert len(message["body_clean"]) == MAX_BODY_CHARS_PER_MESSAGE


def test_empty_when_nothing_eligible(db):
    result = context.claim_next_thread_for_context(db)
    assert result == {"empty": True}
