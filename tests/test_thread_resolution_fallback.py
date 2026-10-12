"""Tests for app/threading.py's fallback thread resolution, carried over
and adapted from cos-sales-agent/app/email/threading.py (see
docs/CARRYOVER.md). Only exercised when Gmail gives no threadId at all.
"""

from datetime import datetime, timezone

import mongomock
import pytest

from app import tools
from app.config import Settings
from app.db import RawEmailDumpRepository
from app.models import parse_email
from app.threading import build_thread_candidates, resolve_thread_id_fallback


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


# --- unit tests on resolve_thread_id_fallback directly ------------------------------


def test_resolves_by_in_reply_to():
    candidates = build_thread_candidates(
        [
            {
                "thread_id": "T-1",
                "message_id": "MSG-1",
                "subject": "Original",
                "from": {"email": "a@x.example"},
                "to": [{"email": "b@x.example"}],
                "cc": [],
                "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )
    email = parse_email(_email_doc("MSG-2", thread_id=None, in_reply_to="MSG-1", subject="Something unrelated"))

    assert resolve_thread_id_fallback(email, candidates) == "T-1"


def test_resolves_by_references_when_in_reply_to_misses():
    candidates = build_thread_candidates(
        [
            {
                "thread_id": "T-1",
                "message_id": "MSG-1",
                "subject": "Original",
                "from": {"email": "a@x.example"},
                "to": [{"email": "b@x.example"}],
                "cc": [],
                "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )
    email = parse_email(
        _email_doc("MSG-3", thread_id=None, in_reply_to="MSG-nonexistent", references=["MSG-1"], subject="Other")
    )

    assert resolve_thread_id_fallback(email, candidates) == "T-1"


def test_resolves_by_subject_and_participants_when_no_reply_headers():
    candidates = build_thread_candidates(
        [
            {
                "thread_id": "T-1",
                "message_id": "MSG-1",
                "subject": "Re: Pricing",
                "from": {"email": "a@x.example"},
                "to": [{"email": "b@x.example"}],
                "cc": [],
                "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )
    email = parse_email(
        _email_doc(
            "MSG-4",
            thread_id=None,
            subject="Fwd: Pricing",
            **{"from": {"email": "a@x.example"}, "to": [{"email": "b@x.example"}]},
        )
    )

    assert resolve_thread_id_fallback(email, candidates) == "T-1"


def test_resolves_by_participants_within_window_as_last_resort():
    candidates = build_thread_candidates(
        [
            {
                "thread_id": "T-1",
                "message_id": "MSG-1",
                "subject": "Completely different subject",
                "from": {"email": "a@x.example"},
                "to": [{"email": "b@x.example"}],
                "cc": [],
                "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )
    email = parse_email(
        _email_doc(
            "MSG-5",
            thread_id=None,
            subject="Yet another subject",
            timestamp=datetime(2026, 8, 5, tzinfo=timezone.utc),
            **{"from": {"email": "a@x.example"}, "to": [{"email": "b@x.example"}]},
        )
    )

    assert resolve_thread_id_fallback(email, candidates) == "T-1"


def test_outside_window_does_not_match():
    candidates = build_thread_candidates(
        [
            {
                "thread_id": "T-1",
                "message_id": "MSG-1",
                "subject": "Different subject",
                "from": {"email": "a@x.example"},
                "to": [{"email": "b@x.example"}],
                "cc": [],
                "timestamp": datetime(2026, 8, 1, tzinfo=timezone.utc),
            }
        ]
    )
    email = parse_email(
        _email_doc(
            "MSG-6",
            thread_id=None,
            subject="Unrelated",
            timestamp=datetime(2026, 9, 1, tzinfo=timezone.utc),  # 31 days later, outside the 14-day window
            **{"from": {"email": "a@x.example"}, "to": [{"email": "b@x.example"}]},
        )
    )

    assert resolve_thread_id_fallback(email, candidates) is None


def test_no_match_returns_none_never_synthesizes():
    email = parse_email(_email_doc("MSG-LONELY", thread_id=None))

    assert resolve_thread_id_fallback(email, []) is None


# --- integration: wired into ingest_raw_email_only -----------------------------------


def test_ingest_resolves_missing_thread_id_via_fallback(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))

    result = _ingest(
        db,
        settings,
        "MSG-2",
        thread_id=None,
        in_reply_to="MSG-1",
        subject="Something else entirely",
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    assert result["thread_id"] == "T-1"
    # Fallback-resolved thread now benefits from reopen logic like any other.
    stored = RawEmailDumpRepository(db).find_one({"message_id": "MSG-2"})
    assert stored["thread_id"] == "T-1"


def test_ingest_leaves_thread_id_none_when_nothing_matches(db):
    settings = _settings()
    result = _ingest(db, settings, "MSG-LONELY", thread_id=None)

    assert result["thread_id"] is None
    assert result["closed_in_thread"] == []
    assert result["reopened_in_thread"] == []


def test_reingest_never_recomputes_or_overwrites_a_resolved_thread_id(db):
    settings = _settings()
    _ingest(db, settings, "MSG-1", thread_id="T-1", timestamp=datetime(2026, 8, 1, tzinfo=timezone.utc))
    first = _ingest(
        db,
        settings,
        "MSG-2",
        thread_id=None,
        in_reply_to="MSG-1",
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )
    assert first["thread_id"] == "T-1"

    # A different, unrelated thread now exists too -- if re-ingest recomputed
    # the fallback, a buggy implementation could drift MSG-2 onto it.
    _ingest(db, settings, "MSG-3", thread_id="T-2", timestamp=datetime(2026, 8, 3, tzinfo=timezone.utc))

    # Re-ingest the SAME Gmail message (e.g. a sliding-window overlap re-read)
    # -- Gmail still reports no threadId for it, same as the first time.
    second = _ingest(
        db,
        settings,
        "MSG-2",
        thread_id=None,
        in_reply_to="MSG-1",
        timestamp=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )

    assert second["thread_id"] == "T-1"  # unchanged, not recomputed, not drifted to T-2
