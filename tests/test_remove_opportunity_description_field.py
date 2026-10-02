import mongomock
import pytest

from scripts import remove_opportunity_description_field as cleanup


def _opportunity_doc(opportunity_id, **overrides):
    doc = {
        "id": opportunity_id,
        "name": "Acme Deal",
        "entity": "Acme",
        "org_id": "ORG-001",
        "status": "open",
        "stage": "Discovery",
        "owner": "Sandeep",
        "description": None,
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
    db.opportunities.insert_one(_opportunity_doc("OPP-001"))
    db.opportunities.insert_one(_opportunity_doc("OPP-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying description: 2" in output
    assert db.opportunities.find_one({"id": "OPP-001"}) is not None


def test_no_op_when_nothing_has_the_legacy_field(db, capsys):
    db.opportunities.insert_one({"id": "OPP-clean", "name": "Clean", "status": "open"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_description(db, capsys):
    db.opportunities.insert_one(_opportunity_doc("OPP-001"))
    db.opportunities.insert_one({"id": "no-legacy-fields", "name": "Clean", "status": "open"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 1" in output
    assert "Documents remaining with description: 0" in output
    assert "Total opportunities document count unchanged: True" in output
    assert "Failures: none" in output

    stored = db.opportunities.find_one({"id": "OPP-001"}, {"_id": 0})
    assert "description" not in stored
    expected = _opportunity_doc("OPP-001")
    del expected["description"]
    assert stored == expected

    untouched = db.opportunities.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "name": "Clean", "status": "open"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.opportunities.insert_one(_opportunity_doc(f"OPP-{i:03d}"))
    before = db.opportunities.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.opportunities.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.opportunities.insert_one(_opportunity_doc("OPP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_status_stage_owner_are_never_touched(db):
    db.opportunities.insert_one(_opportunity_doc("OPP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.opportunities.find_one({"id": "OPP-001"})
    assert stored["status"] == "open"
    assert stored["stage"] == "Discovery"
    assert stored["owner"] == "Sandeep"
    assert stored["entity"] == "Acme"
    assert stored["org_id"] == "ORG-001"


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.opportunities.insert_one(_opportunity_doc("OPP-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"
