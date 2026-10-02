import mongomock
import pytest

from scripts import remove_project_dead_fields as cleanup

_DEAD_FIELDS = ("cluster", "objective", "target", "collaborators", "last_movement", "note_link", "source")


def _project_doc(project_id, **overrides):
    doc = {
        "id": project_id,
        "project": "Acme Rollout",
        "entity": "Acme",
        "goal_pillar": "Sales",
        "status": "open",
        "owner": "Sandeep",
        "health": "on_track",
        "next_milestone": "Kickoff",
        "due": "2026-02-01T00:00:00Z",
        "org_id": "ORG-001",
        "person_ids": ["PER-001"],
        "cluster": None,
        "objective": None,
        "target": None,
        "collaborators": [],
        "last_movement": None,
        "note_link": None,
        "source": "gmail",
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
    db.projects.insert_one(_project_doc("PRJ-001"))
    db.projects.insert_one(_project_doc("PRJ-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying any of" in output
    stored = db.projects.find_one({"id": "PRJ-001"})
    assert stored["source"] == "gmail"


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.projects.insert_one({"id": "PRJ-clean", "project": "Clean", "status": "open"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_the_seven_dead_fields(db, capsys):
    db.projects.insert_one(_project_doc("PRJ-001"))
    db.projects.insert_one({"id": "no-legacy-fields", "project": "Clean", "status": "open"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 1" in output
    assert "Documents remaining with a dead field: 0" in output
    assert "Total projects document count unchanged: True" in output
    assert "Failures: none" in output

    stored = db.projects.find_one({"id": "PRJ-001"}, {"_id": 0})
    for field in _DEAD_FIELDS:
        assert field not in stored
    expected = _project_doc("PRJ-001")
    for field in _DEAD_FIELDS:
        del expected[field]
    assert stored == expected

    untouched = db.projects.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "project": "Clean", "status": "open"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.projects.insert_one(_project_doc(f"PRJ-{i:03d}"))
    before = db.projects.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.projects.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.projects.insert_one(_project_doc("PRJ-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_status_owner_health_milestone_due_are_never_touched(db):
    # These five ARE a real, human-managed product feature (update_project_fields) --
    # this cleanup must never remove or alter them, unlike the seven dead fields above.
    db.projects.insert_one(_project_doc("PRJ-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.projects.find_one({"id": "PRJ-001"})
    assert stored["status"] == "open"
    assert stored["owner"] == "Sandeep"
    assert stored["health"] == "on_track"
    assert stored["next_milestone"] == "Kickoff"
    assert stored["due"] == "2026-02-01T00:00:00Z"
    assert stored["entity"] == "Acme"
    assert stored["org_id"] == "ORG-001"
    assert stored["person_ids"] == ["PER-001"]


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.projects.insert_one(_project_doc("PRJ-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"
