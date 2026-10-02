import mongomock
import pytest

from scripts import remove_meeting_dead_fields as cleanup

_DEAD_FIELDS = ("minutes_record", "next_meeting_date", "agenda_target", "agenda_written")


def _meeting_doc(meeting_id, **overrides):
    doc = {
        "id": meeting_id,
        "date": "2026-02-01T10:00:00Z",
        "attendees": ["Ashok"],
        "person_ids": ["PER-001"],
        "org_id": "ORG-001",
        "project_or_pillar": "Sales",
        "actions_raised": ["Send proposal"],
        "actionable": True,
        "thread_id": "THR-001",
        "minutes_record": None,
        "next_meeting_date": None,
        "agenda_target": None,
        "agenda_written": None,
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
    db.meetings.insert_one(_meeting_doc("MTG-001"))
    db.meetings.insert_one(_meeting_doc("MTG-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying any of" in output
    assert db.meetings.find_one({"id": "MTG-001"}) is not None


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.meetings.insert_one({"id": "MTG-clean", "date": "2026-02-01T10:00:00Z", "actionable": True})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_the_four_dead_fields(db, capsys):
    db.meetings.insert_one(_meeting_doc("MTG-001"))
    db.meetings.insert_one({"id": "no-legacy-fields", "date": "2026-02-01T10:00:00Z", "actionable": True})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 1" in output
    assert "Documents remaining with a dead field: 0" in output
    assert "Total meetings document count unchanged: True" in output
    assert "Failures: none" in output

    stored = db.meetings.find_one({"id": "MTG-001"}, {"_id": 0})
    for field in _DEAD_FIELDS:
        assert field not in stored
    expected = _meeting_doc("MTG-001")
    for field in _DEAD_FIELDS:
        del expected[field]
    assert stored == expected

    untouched = db.meetings.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "date": "2026-02-01T10:00:00Z", "actionable": True}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.meetings.insert_one(_meeting_doc(f"MTG-{i:03d}"))
    before = db.meetings.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.meetings.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.meetings.insert_one(_meeting_doc("MTG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_date_attendees_actionable_and_actions_raised_are_never_touched(db):
    db.meetings.insert_one(_meeting_doc("MTG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.meetings.find_one({"id": "MTG-001"})
    assert stored["date"] == "2026-02-01T10:00:00Z"
    assert stored["attendees"] == ["Ashok"]
    assert stored["actionable"] is True
    assert stored["actions_raised"] == ["Send proposal"]
    assert stored["project_or_pillar"] == "Sales"
    assert stored["person_ids"] == ["PER-001"]
    assert stored["org_id"] == "ORG-001"
    assert stored["thread_id"] == "THR-001"


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.meetings.insert_one(_meeting_doc("MTG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"
