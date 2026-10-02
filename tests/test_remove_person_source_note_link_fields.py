import mongomock
import pytest

from scripts import remove_person_source_note_link_fields as cleanup


def _person_doc(person_id, **overrides):
    doc = {
        "id": person_id,
        "name": "Someone",
        "email": f"{person_id.lower()}@example.com",
        "aliases": [],
        "org": "Acme Corp",
        "org_id": "ORG-001",
        "role": "CTO",
        "goal_pillar": None,
        "last_inbound": "2026-09-26T09:00:00Z",
        "last_outbound": None,
        "open_threads": ["THR-001"],
        "status": "active",
        "merged_into": None,
        "source": "gmail",
        "note_link": None,
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
    db.people.insert_one(_person_doc("PER-001"))
    db.people.insert_one(_person_doc("PER-002"))

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert "Documents currently carrying source and/or note_link: 2" in output
    for person_id in ("PER-001", "PER-002"):
        stored = db.people.find_one({"id": person_id})
        assert stored["source"] == "gmail"


def test_no_op_when_nothing_has_the_legacy_fields(db, capsys):
    db.people.insert_one({"id": "PER-clean", "name": "s", "email": "s@example.com"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_source_and_note_link(db, capsys):
    db.people.insert_one(_person_doc("PER-001"))
    db.people.insert_one(_person_doc("PER-002"))
    # A document that never had these fields at all -- must be left completely alone.
    db.people.insert_one({"id": "no-legacy-fields", "name": "s", "email": "s@example.com", "role": "CTO"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with source/note_link: 0" in output
    assert "Total people document count unchanged: True" in output
    assert "Failures: none" in output

    for person_id in ("PER-001", "PER-002"):
        stored = db.people.find_one({"id": person_id}, {"_id": 0})
        assert "source" not in stored
        assert "note_link" not in stored
        # Every other field preserved exactly as seeded, including role/status/
        # merged_into/goal_pillar -- the fields this script must never touch.
        expected = _person_doc(person_id)
        del expected["source"]
        del expected["note_link"]
        assert stored == expected

    untouched = db.people.find_one({"id": "no-legacy-fields"}, {"_id": 0})
    assert untouched == {"id": "no-legacy-fields", "name": "s", "email": "s@example.com", "role": "CTO"}


def test_confirmed_run_never_changes_total_document_count(db):
    for i in range(5):
        db.people.insert_one(_person_doc(f"PER-{i:03d}"))
    before = db.people.count_documents({})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert db.people.count_documents({}) == before


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.people.insert_one(_person_doc("PER-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()  # discard first run's output

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_role_status_merged_into_and_goal_pillar_are_never_touched(db):
    # The fields this cleanup must never remove or alter -- role/status/
    # merged_into are actively used (duplicate-consolidation, role backfill);
    # goal_pillar is kept because it's read by get_bounded_person_context_for_llm.
    db.people.insert_one(_person_doc("PER-001", status="merged", merged_into="PER-999"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    stored = db.people.find_one({"id": "PER-001"})
    assert stored["role"] == "CTO"
    assert stored["status"] == "merged"
    assert stored["merged_into"] == "PER-999"
    assert "goal_pillar" in stored
    assert stored["goal_pillar"] is None


def test_emails_and_threads_collections_are_never_touched(db):
    db.emails.insert_one({"message_id": "EML-001", "source": "gmail"})
    db.threads.insert_one({"thread_id": "THR-001", "source": "gmail"})
    db.people.insert_one(_person_doc("PER-001"))

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    # Untouched -- this script must never have unset source from emails/threads.
    assert db.emails.find_one({"message_id": "EML-001"})["source"] == "gmail"
    assert db.threads.find_one({"thread_id": "THR-001"})["source"] == "gmail"


def test_update_only_ever_unsets_source_and_note_link(db, monkeypatch):
    """Regression guard: the update document must be exactly
    {"$unset": {"source": "", "note_link": ""}} -- asserted structurally, not
    just by convention."""
    db.people.insert_one(_person_doc("PER-001"))

    original_update_many = db.people.update_many
    captured: list[tuple] = []

    def _spying_update_many(filter_, update, **kwargs):
        captured.append((filter_, update))
        return original_update_many(filter_, update, **kwargs)

    monkeypatch.setattr(db.people, "update_many", _spying_update_many)

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert len(captured) == 1
    _, update = captured[0]
    assert set(update.keys()) == {"$unset"}
    assert set(update["$unset"].keys()) == {"source", "note_link"}
