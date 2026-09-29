import mongomock
import pytest

from scripts import backfill_email_thread_internal_ids as backfill


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    return client["backfill_test"]


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch, db):
    monkeypatch.setattr(backfill, "get_client", lambda uri: {"backfill_test": db})
    yield


def _email(message_id, timestamp, **overrides):
    doc = {"message_id": message_id, "timestamp": timestamp, "subject": "s", "body": "b"}
    doc.update(overrides)
    return doc


def _thread(thread_id, last_message_at, **overrides):
    doc = {"thread_id": thread_id, "last_message_at": last_message_at, "message_ids": [thread_id]}
    doc.update(overrides)
    return doc


def test_dry_run_writes_nothing(db, capsys):
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z"))
    db.threads.insert_one(_thread("thread_msg_001", "2026-01-01T00:00:00Z"))

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "DRY RUN" in output
    assert "Dry run only -- no write performed" in output
    assert db.emails.find_one({"message_id": "msg_001"}).get("id") is None
    assert db.threads.find_one({"thread_id": "thread_msg_001"}).get("id") is None
    assert db.counters.count_documents({}) == 0


def test_no_op_when_nothing_is_missing_an_id(db, capsys):
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z", id="EML-001"))
    db.threads.insert_one(_thread("thread_msg_001", "2026-01-01T00:00:00Z", id="THR-001"))

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert exit_code == 0
    assert capsys.readouterr().out.count("Nothing to do") == 2


def test_confirmed_run_assigns_ids_oldest_first(db, capsys):
    # Inserted out of chronological order -- assignment must still be oldest-first.
    db.emails.insert_one(_email("msg_002", "2026-01-02T00:00:00Z"))
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z"))
    db.threads.insert_one(_thread("thread_b", "2026-01-02T00:00:00Z"))
    db.threads.insert_one(_thread("thread_a", "2026-01-01T00:00:00Z"))

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Failures: none" in output

    assert db.emails.find_one({"message_id": "msg_001"})["id"] == "EML-001"
    assert db.emails.find_one({"message_id": "msg_002"})["id"] == "EML-002"
    assert db.threads.find_one({"thread_id": "thread_a"})["id"] == "THR-001"
    assert db.threads.find_one({"thread_id": "thread_b"})["id"] == "THR-002"


def test_message_id_and_thread_id_values_are_never_touched(db):
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z"))
    db.threads.insert_one(_thread("thread_msg_001", "2026-01-01T00:00:00Z"))

    backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert db.emails.find_one({"message_id": "msg_001"})["message_id"] == "msg_001"
    assert db.threads.find_one({"thread_id": "thread_msg_001"})["thread_id"] == "thread_msg_001"


def test_never_overwrites_an_existing_id(db):
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z", id="EML-999"))
    db.emails.insert_one(_email("msg_002", "2026-01-02T00:00:00Z"))

    backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert db.emails.find_one({"message_id": "msg_001"})["id"] == "EML-999"
    # The new document still gets a fresh sequential id, unaffected by the
    # pre-existing out-of-band value above.
    assert db.emails.find_one({"message_id": "msg_002"})["id"] == "EML-001"


def test_rerunning_after_a_full_backfill_is_a_safe_no_op(db, capsys):
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z"))
    db.threads.insert_one(_thread("thread_msg_001", "2026-01-01T00:00:00Z"))

    backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])
    capsys.readouterr()  # discard first run's output
    first_eml_id = db.emails.find_one({"message_id": "msg_001"})["id"]
    first_thr_id = db.threads.find_one({"thread_id": "thread_msg_001"})["id"]

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert exit_code == 0
    assert capsys.readouterr().out.count("Nothing to do") == 2
    assert db.emails.find_one({"message_id": "msg_001"})["id"] == first_eml_id
    assert db.threads.find_one({"thread_id": "thread_msg_001"})["id"] == first_thr_id


def test_limit_stops_after_n_and_a_second_run_finishes_the_rest(db):
    for i in range(5):
        db.emails.insert_one(_email(f"msg_{i:03d}", f"2026-01-0{i + 1}T00:00:00Z"))

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm", "--limit", "2"])

    assert exit_code == 0  # a limited run is not itself a failure
    assigned = [d for d in db.emails.find({}) if "id" in d]
    assert len(assigned) == 2
    assert {d["message_id"] for d in assigned} == {"msg_000", "msg_001"}  # oldest two

    # Structural resumability: re-running (still --limit 2, or omitted) picks up
    # exactly where the first run left off.
    backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])
    assert db.emails.count_documents({"id": {"$exists": False}}) == 0
    all_ids = sorted(d["id"] for d in db.emails.find({}))
    assert all_ids == ["EML-001", "EML-002", "EML-003", "EML-004", "EML-005"]


def test_uses_the_same_atomic_counter_as_the_live_pipeline(db):
    # Simulates the counter already having advanced (e.g. the live pipeline
    # assigned EML-001 to a brand-new email before this backfill ran) -- the
    # backfill must continue the sequence, never restart or collide with it.
    from app.entities.ids import next_id

    live_id = next_id(db, "EML-")
    assert live_id == "EML-001"

    db.emails.insert_one(_email("msg_backfilled", "2026-01-01T00:00:00Z"))
    backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert db.emails.find_one({"message_id": "msg_backfilled"})["id"] == "EML-002"


def test_detects_duplicate_ids_as_a_failure(db, capsys):
    # A pre-existing duplicate (never created by this script, but the report
    # must still surface it rather than silently ignore it).
    db.emails.insert_one(_email("msg_001", "2026-01-01T00:00:00Z", id="EML-001"))
    db.emails.insert_one(_email("msg_002", "2026-01-02T00:00:00Z", id="EML-001"))

    exit_code = backfill.main(["--uri", "mongodb://irrelevant", "--db", "backfill_test", "--confirm"])

    assert exit_code == 1
    assert "1 duplicate id value" in capsys.readouterr().out
