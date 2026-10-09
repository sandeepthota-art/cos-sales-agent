"""End-to-end simulation of the sync catch-up budget described in SKILL.md
(5 pages / 50 documents per scheduled run, draining a >50-document backlog
across runs without starvation).

Scope note: the 5-page-per-run STOP rule itself lives in SKILL.md prose,
executed by Claude at runtime -- there is no Python code enforcing it, so
no test can verify Claude actually stops at page 5. What IS verified here
is everything that rule depends on being correct: get_unsynced_labels's
pagination/ordering holds up across a backlog >50 docs, mark_gmail_label_synced
is idempotent, and a persistent failure (never marked synced) stays visible
indefinitely instead of silently dropping out.
"""

from datetime import datetime, timezone

import mongomock
import pytest

from app import tools
from app.config import Settings
from app.db import RawEmailDumpRepository
from app.models import parse_email

CATCH_UP_PAGE_SIZE = 10
CATCH_UP_MAX_PAGES_PER_RUN = 5


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


def _seed_unsynced(db, settings, message_id: str, labelled_at_iso: str) -> None:
    """Creates a raw-dumped, already-classified, not-yet-Gmail-synced
    document with an EXPLICIT labelled_at -- bypassing wall-clock timing so
    a 60-document backlog has a deterministic, collision-free order instead
    of depending on datetime.now() resolution across a fast loop."""
    tools.ingest_raw_email_only(db, parse_email(_email_doc(message_id)), settings)
    repo = RawEmailDumpRepository(db)
    repo.upsert_by_key(
        {"message_id": message_id},
        {
            "label_applied": "1. Needs reply",
            "labelled_at": labelled_at_iso,
            "classification_version": tools._CURRENT_LABEL_CLASSIFICATION_VERSION,
            "label_history": [
                {
                    "label_applied": "1. Needs reply",
                    "labelled_at": labelled_at_iso,
                    "classification_version": tools._CURRENT_LABEL_CLASSIFICATION_VERSION,
                }
            ],
            "gmail_label_synced": False,
        },
    )


def _run_catch_up(db, *, max_pages: int) -> list[str]:
    """Simulates one scheduled run's bounded catch-up loop: drains up to
    max_pages pages of CATCH_UP_PAGE_SIZE, applying the (simulated-successful)
    Gmail retry and marking each synced. Returns the message_ids processed
    this run, in the order get_unsynced_labels returned them."""
    processed: list[str] = []
    for _ in range(max_pages):
        page = tools.get_unsynced_labels(db, limit=CATCH_UP_PAGE_SIZE)
        if not page:
            break
        for doc in page:
            # Simulated successful Gmail re-apply of doc["label_applied"].
            tools.mark_gmail_label_synced(db, doc["message_id"])
            processed.append(doc["message_id"])
    return processed


def test_catch_up_drains_backlog_across_runs_without_starvation(db):
    settings = _settings()
    backlog_size = 60
    for i in range(backlog_size):
        _seed_unsynced(db, settings, f"MSG-{i:03d}", f"2026-01-01T00:00:00.{i:03d}Z")

    run_1 = _run_catch_up(db, max_pages=CATCH_UP_MAX_PAGES_PER_RUN)
    assert len(run_1) == CATCH_UP_MAX_PAGES_PER_RUN * CATCH_UP_PAGE_SIZE  # exactly the configured budget
    assert run_1 == [f"MSG-{i:03d}" for i in range(50)]  # oldest-first, no gaps, no repeats

    remaining = tools.get_unsynced_labels(db, limit=100)
    assert [d["message_id"] for d in remaining] == [f"MSG-{i:03d}" for i in range(50, 60)]

    run_2 = _run_catch_up(db, max_pages=CATCH_UP_MAX_PAGES_PER_RUN)
    assert run_2 == [f"MSG-{i:03d}" for i in range(50, 60)]  # continues, never restarts or skips
    assert tools.get_unsynced_labels(db, limit=100) == []


def test_repeated_gmail_label_application_is_idempotent(db):
    settings = _settings()
    _seed_unsynced(db, settings, "MSG-DUP", "2026-01-01T00:00:00.000Z")

    first = tools.mark_gmail_label_synced(db, "MSG-DUP")
    second = tools.mark_gmail_label_synced(db, "MSG-DUP")

    assert first["gmail_label_synced"] is True
    assert second["gmail_label_synced"] is True
    # Re-confirming sync is not a reclassification -- history/version untouched.
    assert first["label_history"] == second["label_history"]
    assert len(second["label_history"]) == 1
    assert second["classification_version"] == tools._CURRENT_LABEL_CLASSIFICATION_VERSION


def test_persistent_sync_failure_stays_visible_across_many_runs(db):
    settings = _settings()
    _seed_unsynced(db, settings, "MSG-STUCK", "2026-01-01T00:00:00.000Z")

    # Simulate several scheduled runs where the Gmail retry keeps failing --
    # never calling mark_gmail_label_synced, same as the skill instructs.
    for _ in range(5):
        found = tools.get_unsynced_labels(db, limit=10)
        assert [d["message_id"] for d in found] == ["MSG-STUCK"]

    final = tools.get_unsynced_labels(db, limit=10)[0]
    assert final["gmail_label_synced"] is False
    assert final["classification_version"] == tools._CURRENT_LABEL_CLASSIFICATION_VERSION
    assert final["label_applied"] == "1. Needs reply"
