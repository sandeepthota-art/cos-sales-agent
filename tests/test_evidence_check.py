"""Phase 3 item 2: persist_raw_email_label's required evidence check."""

from datetime import datetime, timezone

import mongomock
import pytest

from app import tools
from app.config import Settings
from app.models import parse_email


@pytest.fixture
def db():
    return mongomock.MongoClient().db


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


def test_persist_accepts_an_exact_quote_from_the_body(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    result = tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply", claim_id=None, evidence="renewal pricing")

    assert result["label_applied"] == "1. Needs reply"
    assert result["label_history"][-1]["evidence"] == "renewal pricing"


def test_persist_accepts_a_quote_case_and_whitespace_insensitively(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    result = tools.persist_raw_email_label(
        db, "MSG-1", "1. Needs reply", claim_id=None, evidence="RENEWAL   pricing"
    )

    assert result["label_applied"] == "1. Needs reply"


def test_persist_accepts_a_quote_from_the_subject(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    result = tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply", claim_id=None, evidence="Renewal pricing")

    assert result["label_applied"] == "1. Needs reply"


def test_persist_accepts_a_quote_from_thread_context(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(
        db,
        settings,
        "MSG-2",
        thread_id="T-1",
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
        subject="Follow-up",
        body="Approving the budget increase for this deal.",
    )

    result = tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply", claim_id=None, evidence="budget increase")

    assert result["label_applied"] == "1. Needs reply"


def test_persist_rejects_a_quote_not_present_anywhere(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    result = tools.persist_raw_email_label(
        db, "MSG-1", "1. Needs reply", claim_id=None, evidence="something never written here"
    )

    assert result == {"ok": False, "reason": "evidence_not_found"}
    stored = tools.get_next_unprocessed_raw_email(db)
    assert stored is not None  # never persisted -- still eligible, nothing written


def test_persist_rejects_a_quote_only_present_in_the_quoted_reply_block(db):
    """Evidence must come from body_clean (quote/signature-stripped), not
    the raw quoted-reply tail -- otherwise a stale quoted claim could pass
    as evidence for a fresh decision."""
    settings = _settings()
    _ingest(
        db,
        settings,
        "MSG-1",
        body="Sounds good.\n\n> the original unrelated offer expires Friday",
    )

    result = tools.persist_raw_email_label(
        db, "MSG-1", "1. Needs reply", claim_id=None, evidence="unrelated offer expires"
    )

    assert result == {"ok": False, "reason": "evidence_not_found"}


def test_persist_raises_on_empty_evidence(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    with pytest.raises(ValueError, match="evidence"):
        tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply", claim_id=None, evidence="")


def test_persist_raises_on_evidence_over_300_chars(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1")

    with pytest.raises(ValueError, match="evidence"):
        tools.persist_raw_email_label(db, "MSG-1", "1. Needs reply", claim_id=None, evidence="x" * 301)
