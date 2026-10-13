"""scripts/mark_history_only.py -- synthetic data only."""

import sys
from datetime import datetime, timezone
from pathlib import Path

import mongomock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from app import tools
from app.config import Settings
from app.db import RawEmailDumpRepository
from app.models import parse_email
from mark_history_only import apply_history_only, find_docs_before_cutoff


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


def test_find_docs_before_cutoff_returns_only_older_docs(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-NEW", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)

    found = find_docs_before_cutoff(repo, "2026-09-01T00:00:00+00:00")

    assert [d["message_id"] for d in found] == ["MSG-OLD"]


def test_find_docs_before_cutoff_excludes_already_marked_docs(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)
    apply_history_only(repo, ["MSG-OLD"])

    found = find_docs_before_cutoff(repo, "2026-09-01T00:00:00+00:00")

    assert found == []


def test_apply_history_only_sets_the_flag_without_touching_label_fields(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)

    apply_history_only(repo, ["MSG-OLD"])

    doc = repo.find_one({"message_id": "MSG-OLD"})
    assert doc["history_only"] is True
    assert doc["label_applied"] is None  # untouched
    assert doc["classification_version"] == 0  # untouched


def test_dry_run_makes_no_changes(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)

    find_docs_before_cutoff(repo, "2026-09-01T00:00:00+00:00")  # dry-run equivalent: find only, no apply

    doc = repo.find_one({"message_id": "MSG-OLD"})
    assert doc.get("history_only") is not True


def test_history_only_docs_excluded_from_single_claim(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-NEW", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)
    apply_history_only(repo, ["MSG-OLD"])

    claimed = tools.get_next_unprocessed_raw_email(db)

    assert claimed["message_id"] == "MSG-NEW"  # MSG-OLD skipped despite being older


def test_history_only_docs_excluded_from_batch_claim(db):
    settings = _settings()
    _ingest(db, settings, "MSG-OLD-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-OLD-2", timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-NEW", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)
    apply_history_only(repo, ["MSG-OLD-1", "MSG-OLD-2"])

    batch = tools.get_next_unprocessed_raw_email_batch(db, batch_size=10)

    assert [d["message_id"] for d in batch] == ["MSG-NEW"]


def test_history_only_doc_excluded_even_when_it_is_the_newest(db):
    """Newest-first claim order must not override the history_only
    exclusion -- a history_only doc that is the newest by sort_key must
    still never be claimed; the next-newest non-history_only doc is."""
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    _ingest(db, settings, "MSG-NEW", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc))
    repo = RawEmailDumpRepository(db)
    apply_history_only(repo, ["MSG-NEW"])

    claimed = tools.get_next_unprocessed_raw_email(db)

    assert claimed["message_id"] == "MSG-OLD"  # MSG-NEW skipped despite being newest


def test_history_only_doc_never_claimable_even_after_a_thread_reopen(db):
    """A history_only doc should stay excluded even if it would otherwise
    become eligible again (e.g. classification_version reset by a reopen)
    -- history_only is a permanent exclusion, not just a one-time skip."""
    settings = _settings()
    _ingest(db, settings, "MSG-OLD", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    tools.persist_raw_email_label(db, "MSG-OLD", "1. Read only", claim_id=None, evidence="renewal pricing")
    repo = RawEmailDumpRepository(db)
    apply_history_only(repo, ["MSG-OLD"])

    _ingest(db, settings, "MSG-NEW", thread_id="T-1", timestamp=datetime(2026, 9, 15, tzinfo=timezone.utc))

    claimed = tools.get_next_unprocessed_raw_email(db)
    assert claimed["message_id"] == "MSG-NEW"  # MSG-OLD reopened (version=0) but still excluded
