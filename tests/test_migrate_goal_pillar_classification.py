import mongomock
import mongomock.collection
import pytest

from scripts import migrate_goal_pillar_classification as migrate


@pytest.fixture(autouse=True)
def _mongomock_bulk_write_sort_kwarg_shim(monkeypatch):
    """Version-skew shim, this test file only: installed pymongo (4.18.1)
    unconditionally passes sort=... from UpdateOne._add_to_bulk into
    BulkOperationBuilder.add_update, but installed mongomock (4.3.0) doesn't
    accept that kwarg yet. Real MongoDB/pymongo bulk_write is unaffected by
    this -- this only patches mongomock's test double so bulk_write can be
    exercised at all in this suite. Never touches global dependencies."""
    original = mongomock.collection.BulkOperationBuilder.add_update

    def _add_update_ignoring_sort(self, selector, doc, multi=False, upsert=False, collation=None, array_filters=None, hint=None, sort=None):
        return original(self, selector, doc, multi=multi, upsert=upsert, collation=collation, array_filters=array_filters, hint=hint)

    monkeypatch.setattr(mongomock.collection.BulkOperationBuilder, "add_update", _add_update_ignoring_sort)
    yield


def _full_email_doc(message_id, goal_pillar, **overrides):
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
        "goal_pillar": goal_pillar,
        "label_applied": "Read only",
        "priority": "P2",
        "confidence": 0.8,
        "entities_referenced": {"people": [], "projects": [], "commitments": [], "meetings": []},
        "processing_status": {"stage": "COMPLETED", "error": None},
    }
    doc.update(overrides)
    return doc


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["migration_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    monkeypatch.setattr(migrate, "get_client", lambda uri: {"migration_test": db})
    yield


def _seed_all_14(db):
    for message_id, entry in migrate.MIGRATION_PLAN.items():
        db.emails.insert_one(_full_email_doc(message_id, entry["expected_old"]))


def test_dry_run_writes_nothing(db, capsys):
    _seed_all_14(db)

    exit_code = migrate.main(["--uri", "mongodb://irrelevant", "--db", "migration_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    for message_id, entry in migrate.MIGRATION_PLAN.items():
        stored = db.emails.find_one({"message_id": message_id})
        assert stored["goal_pillar"] == entry["expected_old"]  # unchanged


def test_refuses_when_a_document_is_missing(db, capsys):
    _seed_all_14(db)
    db.emails.delete_one({"message_id": "1a0cf65bb5178a67"})

    exit_code = migrate.main(["--uri", "mongodb://irrelevant", "--db", "migration_test", "--confirm"])

    assert exit_code == 1
    assert "not found" in capsys.readouterr().out
    # Nothing written -- goal_pillar values for the remaining 13 unchanged.
    for message_id, entry in migrate.MIGRATION_PLAN.items():
        stored = db.emails.find_one({"message_id": message_id})
        if stored:
            assert stored["goal_pillar"] == entry["expected_old"]


def test_refuses_when_current_goal_pillar_does_not_match_expected(db, capsys):
    _seed_all_14(db)
    # Simulate the document having changed since the audit.
    db.emails.update_one({"message_id": "1a0d953d5b99c2d8"}, {"$set": {"goal_pillar": "Sales"}})

    exit_code = migrate.main(["--uri", "mongodb://irrelevant", "--db", "migration_test", "--confirm"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "CURRENT goal_pillar that doesn't match" in output
    assert "1a0d953d5b99c2d8" in output
    # Confirm no bulk_write happened -- the changed document keeps its new value,
    # and none of the others were touched either.
    stored = db.emails.find_one({"message_id": "1a0d953d5b99c2d8"})
    assert stored["goal_pillar"] == "Sales"


def test_confirmed_migration_updates_only_goal_pillar_and_reports_correctly(db, capsys):
    _seed_all_14(db)
    # An unrelated document that must never be touched.
    db.emails.insert_one(_full_email_doc("UNRELATED-MSG", "Sales"))

    exit_code = migrate.main(["--uri", "mongodb://irrelevant", "--db", "migration_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Total documents migrated: 14 / 14" in output
    assert "Classified Sales: 0" in output
    assert "Classified Not Sales: 14" in output
    assert "Old Sales -> New Sales: 0" in output
    assert "Old Sales -> New Not Sales: 11" in output
    assert "Old non-Sales -> New Sales: 0" in output
    assert "Old non-Sales -> New Not Sales: 3" in output
    assert "Failures: none" in output

    for message_id in migrate.MIGRATION_PLAN:
        stored = db.emails.find_one({"message_id": message_id}, {"_id": 0})
        assert stored["goal_pillar"] == ""
        # Every other field preserved exactly as seeded.
        expected = _full_email_doc(message_id, migrate.MIGRATION_PLAN[message_id]["expected_old"])
        expected["goal_pillar"] = ""
        assert stored == expected

    # The unrelated document is completely untouched.
    unrelated = db.emails.find_one({"message_id": "UNRELATED-MSG"}, {"_id": 0})
    assert unrelated["goal_pillar"] == "Sales"


def test_migration_plan_has_no_duplicate_message_ids():
    assert len(migrate.MIGRATION_PLAN) == len(set(migrate.MIGRATION_PLAN)) == 14


def test_bulk_write_operations_are_exactly_14_targeted_goal_pillar_only_updates(db, monkeypatch):
    """Regression guard: every operation must filter on message_id alone and
    $set exactly {"goal_pillar": ...} -- never a blanket filter, never a
    second field -- asserted structurally, not just by convention."""
    _seed_all_14(db)

    original_bulk_write = db.emails.bulk_write
    captured_ops = []

    def _spying_bulk_write(operations, **kwargs):
        captured_ops.extend(operations)
        return original_bulk_write(operations, **kwargs)

    monkeypatch.setattr(db.emails, "bulk_write", _spying_bulk_write)

    migrate.main(["--uri", "mongodb://irrelevant", "--db", "migration_test", "--confirm"])

    assert len(captured_ops) == 14
    seen_message_ids = set()
    for op in captured_ops:
        assert set(op._filter.keys()) == {"message_id"}
        seen_message_ids.add(op._filter["message_id"])
        assert set(op._doc.keys()) == {"$set"}
        assert set(op._doc["$set"].keys()) == {"goal_pillar"}
    assert seen_message_ids == set(migrate.MIGRATION_PLAN.keys())
