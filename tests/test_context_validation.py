"""app.context.persist_context_delta -- validation pipeline, synthetic data
only. Every test here asserts {"ok": False, "reason": "validation_failed"}
AND that nothing was written (all-or-nothing)."""

from datetime import datetime, timezone

import mongomock
import pytest

from app import context, tools
from app.config import Settings
from app.context_config import MAX_ENTITIES_PER_DELTA, VALUE_MAX_LENGTH
from app.db import initialize_indexes
from app.models import parse_email

BODY = (
    "Can we discuss renewal pricing for next quarter? "
    "Jane Doe is the account manager at CustomerCo. "
    "You can also reach support@customerco.example for billing questions. "
    "Her personal email is jane@gmail.com if needed."
)


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
        "body": BODY,
        "timestamp": datetime(2026, 8, 1, 9, 0, tzinfo=timezone.utc),
    }
    doc.update(overrides)
    return doc


def _ingest(db, settings, message_id: str, **overrides) -> dict:
    return tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id, **overrides)), settings)


def _claim(db, **overrides):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", **overrides)
    claimed = context.claim_next_thread_for_context(db)
    return claimed["claim_id"], claimed["thread_id"]


def _assert_nothing_written(db):
    assert db.observations.count_documents({}) == 0
    doc = db.raw_emails_dump.find_one({"message_id": "MSG-1"})
    assert doc["context_version"] == 0


def _valid_observation(**overrides):
    obs = {
        "entity_type": "person",
        "entity_key": "jane@customerco.example",
        "attribute": "department",
        "value": "Sales",
        "evidence": "Can we discuss renewal pricing for next quarter?",
        "source_message_id": "MSG-1",
    }
    obs.update(overrides)
    return obs


def test_unknown_message_id_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(source_message_id="MSG-DOES-NOT-EXIST")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert result["reason"] == "validation_failed"
    assert any("unknown message_id" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_evidence_not_found_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(evidence="this text does not appear anywhere")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("evidence not found" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_evidence_too_long_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(evidence="x" * 301)]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("evidence invalid" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_evidence_empty_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(evidence="")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("evidence invalid" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_attribute_outside_whitelist_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(attribute="favorite_color")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("not in whitelist" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_sensitive_attribute_attempt_rejected(db):
    """health_condition is never whitelisted -- rejected by construction,
    same mechanism as any other non-whitelisted attribute."""
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(attribute="health_condition", value="diabetes")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("not in whitelist" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_invented_email_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(entity_key="ghost@nowhere.example", attribute="title")]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("not in participants or evidence" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_role_address_gets_no_person_attributes(db):
    claim_id, thread_id = _claim(db)
    delta = {
        "observations": [
            _valid_observation(
                entity_key="support@customerco.example",
                evidence="reach support@customerco.example for billing questions",
            )
        ]
    }

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("role address" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_free_mail_domain_creates_no_organization(db):
    claim_id, thread_id = _claim(db)
    delta = {
        "observations": [
            _valid_observation(
                entity_type="organization",
                entity_key="gmail.com",
                attribute="industry",
                value="Email",
                evidence="Her personal email is jane@gmail.com if needed.",
            )
        ]
    }

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("free-mail domain" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_organization_domain_not_matching_participants_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {
        "observations": [
            _valid_observation(entity_type="organization", entity_key="othercorp.example", attribute="industry", value="Logistics")
        ]
    }

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("organization domain not in participants or evidence" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_value_too_long_rejected(db):
    claim_id, thread_id = _claim(db)
    delta = {"observations": [_valid_observation(value="x" * (VALUE_MAX_LENGTH + 1))]}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("value too long" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_entity_count_over_cap_rejected(db):
    claim_id, thread_id = _claim(db)
    observations = [_valid_observation(value=f"Dept-{i}") for i in range(MAX_ENTITIES_PER_DELTA + 1)]
    delta = {"observations": observations}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("too many entities" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_enum_validation_rejects_invalid_thread_status(db):
    claim_id, thread_id = _claim(db)
    delta = {"thread": {"status": "not_a_real_status"}}

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    assert any("invalid status" in e for e in result["errors"])
    _assert_nothing_written(db)


def test_all_or_nothing_one_bad_item_writes_nothing(db):
    claim_id, thread_id = _claim(db)
    delta = {
        "observations": [
            _valid_observation(),
            _valid_observation(source_message_id="MSG-DOES-NOT-EXIST"),
        ]
    }

    result = context.persist_context_delta(db, claim_id, thread_id, delta)

    assert result["ok"] is False
    _assert_nothing_written(db)
