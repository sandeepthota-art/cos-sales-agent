import mongomock
import pytest

from scripts import remove_thread_id_field as cleanup


def _thread_doc(thread_id, **overrides):
    doc = {
        "thread_id": thread_id,
        "source_thread_id": f"gmail_thread_{thread_id}",
        "normalized_subject": "some subject",
        "participant_emails": ["someone@example.com"],
        "message_ids": [f"EML-{thread_id}"],
        "source_message_ids": [f"gmail_{thread_id}"],
        "last_message_at": "2026-09-26T09:00:00Z",
        "id": thread_id,
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["cleanup_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    monkeypatch.setattr(cleanup, "get_client", lambda uri: {"cleanup_test": db})
    yield


def test_dry_run_writes_nothing(db, capsys):
    db.threads.insert_one(_thread_doc("THR-001"))
    db.threads.insert_one(_thread_doc("THR-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying id: 2" in output
    for thread_id in ("THR-001", "THR-002"):
        stored = db.threads.find_one({"thread_id": thread_id})
        assert stored["id"] == thread_id


def test_no_op_when_nothing_has_the_legacy_field(db, capsys):
    db.threads.insert_one({"thread_id": "THR-clean", "normalized_subject": "s"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_id(db, capsys):
    db.threads.insert_one(_thread_doc("THR-001"))
    db.threads.insert_one(_thread_doc("THR-002"))
    # A document that never had id at all -- must be left completely alone.
    db.threads.insert_one({"thread_id": "no-legacy-field", "normalized_subject": "s"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with id: 0" in output
    assert "Total threads document count unchanged: True" in output
    assert "Failures: none" in output

    for thread_id in ("THR-001", "THR-002"):
        stored = db.threads.find_one({"thread_id": thread_id}, {"_id": 0})
        assert "id" not in stored
        # Every other field preserved exactly as seeded, including
        # source_thread_id -- the genuinely distinct field this script must
        # never touch.
        expected = _thread_doc(thread_id)
        del expected["id"]
        assert stored == expected

    untouched = db.threads.find_one({"thread_id": "no-legacy-field"}, {"_id": 0})
    assert untouched == {"thread_id": "no-legacy-field", "normalized_subject": "s"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.threads.insert_one(_thread_doc(f"THR-{i:03d}"))
    before = db.threads.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.threads.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.threads.insert_one(_thread_doc("THR-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()  # discard first run's output

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_source_thread_id_is_never_touched(db):
    db.threads.insert_one(_thread_doc("THR-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.threads.find_one({"thread_id": "THR-001"})
    assert stored["source_thread_id"] == "gmail_thread_THR-001"


def test_emails_collection_is_never_touched(db):
    # This script is threads-only -- must never read/write the emails
    # collection, even incidentally.
    db.emails.insert_one({"message_id": "EML-001", "id": "EML-001"})
    db.threads.insert_one(_thread_doc("THR-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    # Untouched -- this script must never have unset emails.id.
    assert db.emails.find_one({"message_id": "EML-001"})["id"] == "EML-001"


def test_update_only_ever_unsets_id(db, monkeypatch):
    """Regression guard: the update document must be exactly
    {"$unset": {"id": ""}} -- asserted structurally, not just by convention."""
    db.threads.insert_one(_thread_doc("THR-001"))

    original_update_many = db.threads.update_many
    captured: list[tuple] = []

    def _spying_update_many(filter_, update, **kwargs):
        captured.append((filter_, update))
        return original_update_many(filter_, update, **kwargs)

    monkeypatch.setattr(db.threads, "update_many", _spying_update_many)

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert len(captured) == 1
    _, update = captured[0]
    assert set(update.keys()) == {"$unset"}
    assert set(update["$unset"].keys()) == {"id"}
