import mongomock
import pytest

from scripts import remove_email_id_record_id_date_fields as cleanup


def _email_doc(message_id, **overrides):
    doc = {
        "message_id": message_id,
        "thread_id": f"THR-{message_id}",
        "source_message_id": f"gmail_{message_id}",
        "source_thread_id": f"gmail_thread_{message_id}",
        "from": {"name": "Someone", "email": "someone@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@ourcompany.example"}],
        "cc": [],
        "subject": "Some subject",
        "body": "Some body content.",
        "timestamp": "2026-09-26T09:00:00Z",
        "labels": ["IMPORTANT"],
        "goal_pillar": "Sales",
        "label_applied": "1. Read only",
        "id": message_id,
        "record_id": message_id,
        "date": "2026-09-26",
        "entities_referenced": {"people": [], "projects": [], "commitments": [], "meetings": []},
        "processing_status": {"stage": "COMPLETED", "error": None},
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
    db.emails.insert_one(_email_doc("EML-001"))
    db.emails.insert_one(_email_doc("EML-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying id, record_id, and/or date: 2" in output
    for message_id in ("EML-001", "EML-002"):
        stored = db.emails.find_one({"message_id": message_id})
        assert stored["id"] == message_id
        assert stored["record_id"] == message_id
        assert stored["date"] == "2026-09-26"


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.emails.insert_one({"message_id": "EML-clean", "subject": "s", "body": "b"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_id_record_id_and_date(db, capsys):
    db.emails.insert_one(_email_doc("EML-001"))
    db.emails.insert_one(_email_doc("EML-002"))
    # A document that never had these fields at all -- must be left completely alone.
    db.emails.insert_one(
        {"message_id": "no-legacy-fields", "subject": "s", "body": "b", "goal_pillar": "Sales"}
    )

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with id/record_id/date: 0" in output
    assert "Total emails document count unchanged: True" in output
    assert "Failures: none" in output

    for message_id in ("EML-001", "EML-002"):
        stored = db.emails.find_one({"message_id": message_id}, {"_id": 0})
        assert "id" not in stored
        assert "record_id" not in stored
        assert "date" not in stored
        # Every other field preserved exactly as seeded, including
        # source_message_id/source_thread_id -- the genuinely distinct fields
        # this script must never touch.
        expected = _email_doc(message_id)
        del expected["id"]
        del expected["record_id"]
        del expected["date"]
        assert stored == expected

    untouched = db.emails.find_one({"message_id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"message_id": "no-legacy-fields", "subject": "s", "body": "b", "goal_pillar": "Sales"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.emails.insert_one(_email_doc(f"EML-{i:03d}"))
    before = db.emails.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.emails.insert_one(_email_doc("EML-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()  # discard first run's output

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_a_document_with_only_one_of_the_three_legacy_fields_is_still_matched(db):
    # record_id/date already cleaned up previously (or never written for some
    # other reason), but id is still there -- must still be caught and cleaned.
    db.emails.insert_one(
        {"message_id": "EML-partial", "subject": "s", "body": "b", "id": "EML-partial"}
    )

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    stored = db.emails.find_one({"message_id": "EML-partial"}, {"_id": 0})
    assert stored == {"message_id": "EML-partial", "subject": "s", "body": "b"}


def test_source_message_id_and_source_thread_id_are_never_touched(db):
    # The genuinely distinct fields this cleanup must never remove or alter.
    db.emails.insert_one(_email_doc("EML-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.emails.find_one({"message_id": "EML-001"})
    assert stored["source_message_id"] == "gmail_EML-001"
    assert stored["source_thread_id"] == "gmail_thread_EML-001"
    assert stored["thread_id"] == "THR-EML-001"


def test_update_only_ever_unsets_id_record_id_and_date(db, monkeypatch):
    """Regression guard: the update document must be exactly
    {"$unset": {"id": "", "record_id": "", "date": ""}} -- asserted
    structurally, not just by convention."""
    db.emails.insert_one(_email_doc("EML-001"))

    original_update_many = db.emails.update_many
    captured: list[tuple] = []

    def _spying_update_many(filter_, update, **kwargs):
        captured.append((filter_, update))
        return original_update_many(filter_, update, **kwargs)

    monkeypatch.setattr(db.emails, "update_many", _spying_update_many)

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert len(captured) == 1
    _, update = captured[0]
    assert set(update.keys()) == {"$unset"}
    assert set(update["$unset"].keys()) == {"id", "record_id", "date"}
