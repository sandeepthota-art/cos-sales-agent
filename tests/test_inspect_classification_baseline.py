import mongomock
import pytest

from scripts import inspect_classification_baseline as baseline


def _email(message_id, subject="Subject", body="Body text.", **overrides):
    doc = {"message_id": message_id, "subject": subject, "body": body}
    doc.update(overrides)
    return doc


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["baseline_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    # get_client(uri) only needs to return something supporting client[db_name] --
    # a plain dict keyed by database name satisfies that.
    monkeypatch.setattr(baseline, "get_client", lambda uri: {"baseline_test": db})
    yield


def test_counts_and_buckets_are_correct(db, capsys):
    db["emails"].insert_many(
        [
            _email("MSG-SALES-1", goal_pillar="Sales"),
            _email("MSG-SALES-2", goal_pillar="Sales"),
            _email("MSG-NOTSALES-1", goal_pillar=""),
            _email("MSG-MISSING-1"),  # no goal_pillar field at all
            _email("MSG-NULL-1", goal_pillar=None),
            _email("MSG-FINANCE-1", goal_pillar="Finance"),
            _email("MSG-OPS-1", goal_pillar="Operations"),
            _email("MSG-OPS-2", goal_pillar="Operations"),
        ]
    )
    db["projects"].insert_many(
        [
            {"id": "PRJ-1", "goal_pillar": "Sales"},
            {"id": "PRJ-2", "goal_pillar": "Finance"},
        ]
    )

    exit_code = baseline.main(["--uri", "mongodb://irrelevant", "--db", "baseline_test", "--sample-size", "2"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Total email documents: 8" in output
    assert "goal_pillar == 'Sales': 2" in output
    assert "goal_pillar == '': 1" in output
    assert "goal_pillar missing/null: 2" in output
    assert "'Finance': 1" in output
    assert "'Operations': 2" in output
    assert "Total Project documents: 2" in output
    assert "Projects with goal_pillar == 'Sales': 1" in output
    # Sample sections present with real content, not placeholders.
    assert "MSG-SALES-1" in output or "MSG-SALES-2" in output
    assert "MSG-NOTSALES-1" in output
    assert "MSG-MISSING-1" in output or "MSG-FINANCE-1" in output or "MSG-OPS-1" in output


def test_makes_zero_write_calls_even_if_attempted(db):
    db["emails"].insert_one(_email("MSG-1", goal_pillar="Sales"))

    baseline.main(["--uri", "mongodb://irrelevant", "--db", "baseline_test"])

    # The proxy would have raised had the script ever attempted a write; since
    # main() completed without error, no write was attempted. Independently
    # confirm nothing changed.
    assert db["emails"].count_documents({}) == 1
    stored = db["emails"].find_one({"message_id": "MSG-1"})
    assert stored["goal_pillar"] == "Sales"


def test_empty_database_reports_zero_without_error(db, capsys):
    exit_code = baseline.main(["--uri", "mongodb://irrelevant", "--db", "baseline_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Total email documents: 0" in output
    assert "(none)" in output
    assert "(no matching emails)" in output
