import mongomock
import pytest

from scripts import remove_people_legacy_fields as cleanup


def _person_doc(person_id, **overrides):
    doc = {
        "id": person_id,
        "name": "Someone",
        "email": f"{person_id.lower()}@example.com",
        "aliases": [],
        "org": "Acme",
        "org_id": "ORG-001",
        "type": None,
        "goal_pillar": None,
        "reports_to": None,
        "review_flag": False,
        "role_in_pillar": None,
        "tier": None,
        "voice_register": None,
        "preferences": {"voice_signature": "Thanks,\nSomeone"},
        "last_inbound": "2026-09-26T09:00:00Z",
        "last_outbound": None,
        "open_threads": ["thread_1"],
        "note_link": None,
        "source": "gmail",
        "status": "active",
        "merged_into": None,
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
    assert "Documents currently carrying at least one legacy field: 2" in output
    for person_id in ("PER-001", "PER-002"):
        stored = db.people.find_one({"id": person_id})
        assert stored["review_flag"] is False
        assert stored["preferences"] == {"voice_signature": "Thanks,\nSomeone"}


def test_no_op_when_nothing_has_a_legacy_field(db, capsys):
    db.people.insert_one({"id": "clean", "name": "Someone", "email": "someone@example.com"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_confirmed_run_unsets_only_the_six_legacy_fields(db, capsys):
    db.people.insert_one(_person_doc("PER-001"))
    db.people.insert_one(_person_doc("PER-002"))
    # A document that never had any of these fields at all -- must be left completely alone.
    db.people.insert_one({"id": "clean", "name": "Someone", "email": "someone@example.com"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents modified: 2" in output
    assert "Documents remaining with a legacy field: 0" in output
    assert "Total people document count unchanged: True" in output
    assert "Failures: none" in output

    for person_id in ("PER-001", "PER-002"):
        stored = db.people.find_one({"id": person_id}, {"_id": 0})
        for field in cleanup._LEGACY_FIELDS:
            assert field not in stored
        # Every other field preserved exactly as seeded.
        expected = _person_doc(person_id)
        for field in cleanup._LEGACY_FIELDS:
            del expected[field]
        assert stored == expected

    untouched = db.people.find_one({"id": "clean"}, {"_id": 0})
    assert untouched == {"id": "clean", "name": "Someone", "email": "someone@example.com"}


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


def test_update_only_ever_unsets_the_six_legacy_fields(db, monkeypatch):
    """Regression guard: the update document must be exactly
    {"$unset": {<the six legacy fields>: ""}} -- asserted structurally, not just
    by convention."""
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
    assert set(update["$unset"].keys()) == set(cleanup._LEGACY_FIELDS)


def test_a_document_with_only_one_of_the_six_fields_is_still_matched_and_cleaned(db):
    # Not every seeded document has all six -- a document with only, say,
    # review_flag set must still be matched and cleaned.
    db.people.insert_one({"id": "PER-partial", "name": "Partial", "email": "p@example.com", "review_flag": True})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    stored = db.people.find_one({"id": "PER-partial"}, {"_id": 0})
    assert stored == {"id": "PER-partial", "name": "Partial", "email": "p@example.com"}
