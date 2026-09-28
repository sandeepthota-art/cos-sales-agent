import mongomock
import pytest

from scripts import delete_test_diagnostic_records as cleanup


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["cleanup_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    monkeypatch.setattr(cleanup, "get_client", lambda uri: {"cleanup_test": db})
    yield


def test_dry_run_writes_nothing(db, capsys):
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003", "goal_pillar": "Sales"})
    db.commitments.insert_one({"id": "CMT-002", "source_record": "18f2a9c0b1e4d7aa"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no delete performed" in output
    assert "Documents matching test_diag_* across all collections: 1" in output
    assert db.commitments.count_documents({}) == 2


def test_no_op_when_nothing_matches(db, capsys):
    db.commitments.insert_one({"id": "CMT-002", "source_record": "18f2a9c0b1e4d7aa"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out
    assert db.commitments.count_documents({}) == 1


def test_confirmed_run_deletes_only_matching_documents(db, capsys):
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})
    db.commitments.insert_one({"id": "CMT-002", "source_record": "18f2a9c0b1e4d7aa"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents deleted: 1" in output
    assert "Failures: none" in output

    assert db.commitments.count_documents({}) == 1
    remaining = db.commitments.find_one({}, {"_id": 0})
    assert remaining == {"id": "CMT-002", "source_record": "18f2a9c0b1e4d7aa"}


def test_matches_across_multiple_collections(db, capsys):
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})
    db.emails.insert_one({"message_id": "msg-1", "source_record": "test_diag_001"})
    db.people.insert_one({"id": "PER-001", "source_record": "18f2a9c0b1e4d7aa"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents deleted: 2" in output
    assert "Collections touched: 2" in output

    assert db.commitments.count_documents({}) == 0
    assert db.emails.count_documents({}) == 0
    assert db.people.count_documents({}) == 1


def test_matches_via_thread_id_for_entities_without_source_record(db, capsys):
    # FollowUp has no source_record field -- only thread_id.
    db.follow_ups.insert_one({
        "id": "FUP-001", "commitment_id": "CMT-001", "thread_id": "test_diag_003",
        "escalation_level": 1, "status": "active", "surfaced": False,
    })
    db.follow_ups.insert_one({"id": "FUP-002", "commitment_id": "CMT-050", "thread_id": "18f2a9c0b1e4d7aa"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Documents deleted: 1" in output

    assert db.follow_ups.count_documents({}) == 1
    remaining = db.follow_ups.find_one({}, {"_id": 0})
    assert remaining["id"] == "FUP-002"


def test_a_document_without_source_record_is_never_touched(db):
    db.commitments.insert_one({"id": "CMT-003"})

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert db.commitments.count_documents({}) == 1


def test_rerunning_after_cleanup_is_a_safe_no_op(db, capsys):
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])
    capsys.readouterr()  # discard first run's output

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 0
    assert "Nothing to do" in capsys.readouterr().out


def test_reports_failure_when_documents_remain_after_delete(db, monkeypatch, capsys):
    """Regression guard: if delete_many reports success but the collection still
    matches the filter afterward (e.g. a driver-level inconsistency), the script
    must detect and report it as a failure -- never silently claim success."""
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})

    class _FakeResult:
        deleted_count = 1

    monkeypatch.setattr(db.commitments, "delete_many", lambda filter_, **kwargs: _FakeResult())

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 1
    output = capsys.readouterr().out
    # The fake delete_many performs no real deletion, so both the "still
    # matches" check and the total-count check are expected to fire together.
    assert "1 document(s) still match test_diag_* after the delete" in output
    assert "document count mismatch: before=1 after=1 expected=0" in output
    assert "Failures (2):" in output
    # The document was never actually removed by the fake delete_many.
    assert db.commitments.count_documents({}) == 1


def test_reports_failure_when_total_count_changes_unexpectedly(db, monkeypatch, capsys):
    """Regression guard: if the collection's total document count doesn't drop
    by exactly the number deleted, the script must report a count mismatch
    rather than silently proceeding."""
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})
    db.commitments.insert_one({"id": "CMT-002", "source_record": "18f2a9c0b1e4d7aa"})

    original_count_documents = db.commitments.count_documents
    empty_filter_calls = {"count": 0}

    def _spying_count_documents(filter_, *args, **kwargs):
        if filter_ == {}:
            empty_filter_calls["count"] += 1
            if empty_filter_calls["count"] == 2:  # the post-delete "total_after" call
                return 0  # pretend an extra, unrelated document vanished too
        return original_count_documents(filter_, *args, **kwargs)

    monkeypatch.setattr(db.commitments, "count_documents", _spying_count_documents)

    exit_code = cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "document count mismatch: before=2 after=0 expected=1" in output
    assert "Failures (1):" in output


def test_filter_only_ever_targets_source_record_or_thread_id(db, monkeypatch):
    """Regression guard: the delete filter must be exactly
    {"$or": [{"source_record": {"$regex": "^test_diag_"}}, {"thread_id": {"$regex": "^test_diag_"}}]}
    -- asserted structurally."""
    db.commitments.insert_one({"id": "CMT-001", "source_record": "test_diag_003"})

    original_delete_many = db.commitments.delete_many
    captured: list[dict] = []

    def _spying_delete_many(filter_, **kwargs):
        captured.append(filter_)
        return original_delete_many(filter_, **kwargs)

    monkeypatch.setattr(db.commitments, "delete_many", _spying_delete_many)

    cleanup.main(["--uri", "mongodb://irrelevant", "--db", "cleanup_test", "--confirm"])

    assert len(captured) == 1
    assert set(captured[0].keys()) == {"$or"}
    by_field = {tuple(clause.keys())[0]: clause for clause in captured[0]["$or"]}
    assert set(by_field.keys()) == {"source_record", "thread_id"}
    assert by_field["source_record"] == {"source_record": {"$regex": "^test_diag_"}}
    assert by_field["thread_id"] == {"thread_id": {"$regex": "^test_diag_"}}
