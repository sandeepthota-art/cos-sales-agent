import mongomock
import pytest

from scripts import remove_email_source_type_and_link_fields as cleanup


def _email_doc(message_id, **overrides):
    doc = {
        "message_id": message_id,
        "thread_id": f"thread_{message_id}",
        "from": {"name": "Someone", "email": "someone@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@ourcompany.example"}],
        "cc": [],
        "subject": "Some subject",
        "body": "Some body content.",
        "timestamp": "2026-09-26T09:00:00Z",
        "labels": ["IMPORTANT"],
        "goal_pillar": "Sales",
        "label_applied": "Read only",
        "record_id": message_id,
        "source_type": "gmail",
        "source_link": f"https://mail.google.com/mail/u/0/#all/{message_id}",
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
    db.emails.insert_one(_email_doc("m1"))
    db.emails.insert_one(_email_doc("m2"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying source_type and/or source_link: 2" in output
    for message_id in ("m1", "m2"):
        stored = db.emails.find_one({"message_id": message_id})
        assert stored["source_type"] == "gmail"
        assert stored["source_link"] == f"https://mail.google.com/mail/u/0/#all/{message_id}"


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.emails.insert_one({"message_id": "clean", "subject": "s", "body": "b"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_source_type_and_source_link(db, capsys):
    db.emails.insert_one(_email_doc("m1"))
    db.emails.insert_one(_email_doc("m2"))
    # A document that never had these fields at all -- must be left completely alone.
    db.emails.insert_one(
        {"message_id": "no-legacy-fields", "subject": "s", "body": "b", "goal_pillar": "Sales"}
    )

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with source_type/source_link: 0" in output
    assert "Total emails document count unchanged: True" in output
    assert "Failures: none" in output

    for message_id in ("m1", "m2"):
        stored = db.emails.find_one({"message_id": message_id}, {"_id": 0})
        assert "source_type" not in stored
        assert "source_link" not in stored
        # Every other field preserved exactly as seeded.
        expected = _email_doc(message_id)
        del expected["source_type"]
        del expected["source_link"]
        assert stored == expected

    untouched = db.emails.find_one({"message_id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"message_id": "no-legacy-fields", "subject": "s", "body": "b", "goal_pillar": "Sales"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.emails.insert_one(_email_doc(f"m{i}"))
    before = db.emails.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.emails.insert_one(_email_doc("m1"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()  # discard first run's output

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_update_only_ever_unsets_source_type_and_source_link(db, monkeypatch):
    """Regression guard: the update document must be exactly
    {"$unset": {"source_type": "", "source_link": ""}} -- asserted structurally,
    not just by convention."""
    db.emails.insert_one(_email_doc("m1"))

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
    assert set(update["$unset"].keys()) == {"source_type", "source_link"}
