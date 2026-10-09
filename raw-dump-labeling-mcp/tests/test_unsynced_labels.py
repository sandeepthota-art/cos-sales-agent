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


def _ingest_and_label(db, settings, message_id: str, label: str = "1. Needs reply") -> None:
    tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id)), settings)
    tools.persist_raw_email_label(db, message_id, label)


def test_unsynced_label_returned_despite_current_classification_version(db):
    settings = _settings()
    _ingest_and_label(db, settings, "MSG-1")

    [result] = tools.get_unsynced_labels(db)

    assert result["message_id"] == "MSG-1"
    assert result["gmail_label_synced"] is False
    assert result["classification_version"] == tools._CURRENT_LABEL_CLASSIFICATION_VERSION
    assert result["label_applied"] == "1. Needs reply"


def test_already_synced_labels_are_excluded(db):
    settings = _settings()
    _ingest_and_label(db, settings, "MSG-SYNCED")
    tools.mark_gmail_label_synced(db, "MSG-SYNCED")

    assert tools.get_unsynced_labels(db) == []


def test_limit_is_respected(db):
    settings = _settings()
    for i in range(5):
        _ingest_and_label(db, settings, f"MSG-{i}")

    assert len(tools.get_unsynced_labels(db, limit=3)) == 3
    assert len(tools.get_unsynced_labels(db, limit=10)) == 5


def test_failed_gmail_retry_leaves_message_discoverable(db):
    settings = _settings()
    _ingest_and_label(db, settings, "MSG-RETRY")

    # Simulate a failed Gmail retry: the caller never calls
    # mark_gmail_label_synced, because the Gmail write itself failed.
    found_before = tools.get_unsynced_labels(db)
    assert [d["message_id"] for d in found_before] == ["MSG-RETRY"]

    found_after = tools.get_unsynced_labels(db)
    assert [d["message_id"] for d in found_after] == ["MSG-RETRY"]


def test_successful_retry_marks_synced_and_removes_from_results(db):
    settings = _settings()
    _ingest_and_label(db, settings, "MSG-OK")
    assert [d["message_id"] for d in tools.get_unsynced_labels(db)] == ["MSG-OK"]

    tools.mark_gmail_label_synced(db, "MSG-OK")

    assert tools.get_unsynced_labels(db) == []


def test_get_unsynced_labels_is_ordered_oldest_stuck_first_for_stable_pagination(db):
    settings = _settings()
    for i in range(5):
        _ingest_and_label(db, settings, f"MSG-{i}")

    first_page = tools.get_unsynced_labels(db, limit=2)
    assert [d["message_id"] for d in first_page] == ["MSG-0", "MSG-1"]

    for doc in first_page:
        tools.mark_gmail_label_synced(db, doc["message_id"])

    second_page = tools.get_unsynced_labels(db, limit=2)
    assert [d["message_id"] for d in second_page] == ["MSG-2", "MSG-3"]


def test_get_unsynced_labels_never_touches_classification_state(db):
    settings = _settings()
    _ingest_and_label(db, settings, "MSG-STABLE")
    before = tools.get_unsynced_labels(db)[0]

    tools.get_unsynced_labels(db)
    after = tools.get_unsynced_labels(db)[0]

    assert before["classification_version"] == after["classification_version"]
    assert before["label_applied"] == after["label_applied"]
    assert before["label_history"] == after["label_history"]
