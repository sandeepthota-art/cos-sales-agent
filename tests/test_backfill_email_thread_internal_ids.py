import mongomock
import pytest

from scripts import backfill_email_thread_internal_ids as backfill

# This script is fully retired -- both of its former targets (emails, threads)
# had their `id` field removed from the schema entirely during their own
# collection-by-collection cleanup passes (see
# scripts/remove_email_id_record_id_date_fields.py and
# scripts/remove_thread_id_field.py). `_TARGETS` is now empty by design; these
# tests only verify it stays a safe, inert no-op.


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["backfill_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    monkeypatch.setattr(backfill, "get_client", lambda uri: {"backfill_test": db})
    yield


def test_targets_is_empty():
    assert backfill._TARGETS == ()


def test_dry_run_does_nothing_and_touches_no_collection(db, capsys):
    db.emails.insert_one({"message_id": "EML-001", "timestamp": "2026-01-01T00:00:00Z"})
    db.threads.insert_one({"thread_id": "THR-001", "last_message_at": "2026-01-01T00:00:00Z"})

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test"])

    assert exit_code == 0
    assert db.emails.find_one({"message_id": "EML-001"}).get("id") is None
    assert db.threads.find_one({"thread_id": "THR-001"}).get("id") is None
    assert db.counters.count_documents({}) == 0


def test_confirmed_run_does_nothing_and_touches_no_collection(db):
    db.emails.insert_one({"message_id": "EML-001", "timestamp": "2026-01-01T00:00:00Z"})
    db.threads.insert_one({"thread_id": "THR-001", "last_message_at": "2026-01-01T00:00:00Z"})

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert exit_code == 0
    assert db.emails.find_one({"message_id": "EML-001"}).get("id") is None
    assert db.threads.find_one({"thread_id": "THR-001"}).get("id") is None
    assert db.counters.count_documents({}) == 0
