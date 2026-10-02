import mongomock
import pytest

from scripts import remove_follow_up_dead_fields as cleanup


def _follow_up_doc(follow_up_id, **overrides):
    doc = {
        "id": follow_up_id,
        "commitment_id": "CMT-001",
        "thread_id": "THR-001",
        "person_id": "PER-001",
        "org_id": "ORG-001",
        "escalation_level": 1,
        "surfaced": False,
        "status": "active",
        "audience": "internal",
        "follow_up_earliest_at": "2026-02-01T00:00:00Z",
        "follow_up_latest_at": "2026-02-08T00:00:00Z",
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
    db.follow_ups.insert_one(_follow_up_doc("FUP-001"))
    db.follow_ups.insert_one(_follow_up_doc("FUP-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying escalation_level and/or surfaced: 2" in output
    stored = db.follow_ups.find_one({"id": "FUP-001"})
    assert stored["escalation_level"] == 1


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.follow_ups.insert_one({"id": "FUP-clean", "commitment_id": "CMT-001", "status": "active"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_escalation_level_and_surfaced(db, capsys):
    db.follow_ups.insert_one(_follow_up_doc("FUP-001"))
    db.follow_ups.insert_one({"id": "no-legacy-fields", "commitment_id": "CMT-001", "status": "active"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 1" in output
    assert "Documents remaining with escalation_level/surfaced: 0" in output
    assert "Total follow_ups document count unchanged: True" in output
    assert "Failures: none" in output

    stored = db.follow_ups.find_one({"id": "FUP-001"}, {"_id": 0})
    assert "escalation_level" not in stored
    assert "surfaced" not in stored
    expected = _follow_up_doc("FUP-001")
    del expected["escalation_level"]
    del expected["surfaced"]
    assert stored == expected

    untouched = db.follow_ups.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "commitment_id": "CMT-001", "status": "active"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.follow_ups.insert_one(_follow_up_doc(f"FUP-{i:03d}"))
    before = db.follow_ups.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.follow_ups.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.follow_ups.insert_one(_follow_up_doc("FUP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_status_audience_and_windows_are_never_touched(db):
    db.follow_ups.insert_one(_follow_up_doc("FUP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.follow_ups.find_one({"id": "FUP-001"})
    assert stored["status"] == "active"
    assert stored["audience"] == "internal"
    assert stored["follow_up_earliest_at"] == "2026-02-01T00:00:00Z"
    assert stored["follow_up_latest_at"] == "2026-02-08T00:00:00Z"
    assert stored["person_id"] == "PER-001"
    assert stored["org_id"] == "ORG-001"


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.follow_ups.insert_one(_follow_up_doc("FUP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"
