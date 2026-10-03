import mongomock
import pytest

from scripts import remove_organization_dead_fields as cleanup


def _org_doc(org_id, **overrides):
    doc = {
        "id": org_id,
        "name": "Acme Corp",
        "domain": "acme.com",
        "aliases": [],
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
    db.organizations.insert_one(_org_doc("ORG-001"))
    db.organizations.insert_one(_org_doc("ORG-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying source: 2" in output
    for org_id in ("ORG-001", "ORG-002"):
        stored = db.organizations.find_one({"id": org_id})
        assert stored["source"] == "gmail"


def test_no_op_when_nothing_has_the_legacy_field(db, capsys):
    db.organizations.insert_one({"id": "ORG-clean", "name": "s", "domain": "s.com"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_source(db, capsys):
    db.organizations.insert_one(_org_doc("ORG-001"))
    db.organizations.insert_one(_org_doc("ORG-002"))
    db.organizations.insert_one({"id": "no-legacy-fields", "name": "s", "domain": "s.com"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with source: 0" in output
    assert "Total organizations document count unchanged: True" in output
    assert "Failures: none" in output

    for org_id in ("ORG-001", "ORG-002"):
        stored = db.organizations.find_one({"id": org_id}, {"_id": 0})
        assert "source" not in stored
        expected = _org_doc(org_id)
        del expected["source"]
        assert stored == expected

    untouched = db.organizations.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "name": "s", "domain": "s.com"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.organizations.insert_one(_org_doc(f"ORG-{i:03d}"))
    before = db.organizations.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.organizations.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.organizations.insert_one(_org_doc("ORG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_name_domain_and_aliases_are_never_touched(db):
    # aliases is deliberately NOT removed -- it's read by
    # app.query.entity_resolution's organization-name matching, unlike
    # source which has no reader anywhere.
    db.organizations.insert_one(_org_doc("ORG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.organizations.find_one({"id": "ORG-001"})
    assert stored["name"] == "Acme Corp"
    assert stored["domain"] == "acme.com"
    assert stored["aliases"] == []


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.organizations.insert_one(_org_doc("ORG-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"
