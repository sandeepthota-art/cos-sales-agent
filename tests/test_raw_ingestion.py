import json

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository, ThreadRepository
from app.raw_ingestion import run_raw_file_ingestion


def _message(**overrides):
    base = {
        "id": "18f348ce69f386be",
        "threadId": "18f348ce69f386be",
        "subject": "sales/capabilities materials",
        "sender": "john.t@614group.com",
        "toRecipients": ["ashok@databeat.io"],
        "ccRecipients": None,
        "date": "2024-05-01T14:26:28Z",
        "labelIds": ["IMPORTANT", "INBOX"],
        "snippet": "Hi Ashok...",
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


def _write(tmp_path, messages, name="export.json"):
    path = tmp_path / name
    path.write_text(
        json.dumps({"label": "BD", "labelId": "L1", "threadCount": 1, "messageCount": len(messages), "messages": messages}),
        encoding="utf-8",
    )
    return str(path)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


# --- 1. Valid raw JSON ingestion ---


def test_valid_raw_ingestion_inserts_the_email(db, tmp_path):
    path = _write(tmp_path, [_message()])

    summary = run_raw_file_ingestion(db, path)

    assert summary.input_messages == 1
    assert summary.inserted == 1
    assert summary.already_existed == 0
    assert summary.skipped == 0
    assert summary.failed == 0
    assert summary.unique_threads == 1
    assert EmailRepository(db).find_many({})[0]["message_id"] == "18f348ce69f386be"


# --- 2. 662-message structure (generated test data) ---


def test_handles_a_662_message_export(db, tmp_path):
    messages = [
        _message(
            id=f"msg_{i:04d}",
            threadId=f"thread_{i // 5:04d}",  # groups of 5 messages per thread -> 133 threads
        )
        for i in range(662)
    ]
    path = _write(tmp_path, messages)

    summary = run_raw_file_ingestion(db, path)

    assert summary.input_messages == 662
    assert summary.inserted == 662
    assert summary.failed == 0
    assert summary.skipped == 0
    assert len(EmailRepository(db).find_many({})) == 662
    assert summary.unique_threads == 133  # 662 // 5 rounded up == 133 distinct thread ids


# --- 3. Message -> MongoDB field mapping ---


def test_message_to_mongodb_field_mapping_is_exact(db, tmp_path):
    message = _message(
        id="abc123",
        threadId="thread_xyz",
        subject="Renewal check-in",
        sender="jane@example.com",
        toRecipients=["ashok@databeat.io"],
        ccRecipients=["cc1@example.com"],
        date="2025-03-01T10:00:00Z",
        labelIds=["IMPORTANT", "STARRED"],
        plaintextBody="Let's talk renewal terms.",
    )
    path = _write(tmp_path, [message])

    run_raw_file_ingestion(db, path)

    stored = EmailRepository(db).find_one({"message_id": "abc123"})
    assert stored["message_id"] == "abc123"
    assert stored["thread_id"] == "thread_xyz"
    assert stored["subject"] == "Renewal check-in"
    assert stored["from"]["email"] == "jane@example.com"
    assert [a["email"] for a in stored["to"]] == ["ashok@databeat.io"]
    assert [a["email"] for a in stored["cc"]] == ["cc1@example.com"]
    assert stored["timestamp"].startswith("2025-03-01T10:00:00")
    assert stored["labels"] == ["IMPORTANT", "STARRED"]
    assert stored["body"] == "Let's talk renewal terms."


# --- 4. Thread creation/update ---


def test_thread_created_with_correct_participants_and_message_ids(db, tmp_path):
    messages = [
        _message(id="m1", threadId="t1", sender="a@x.com", toRecipients=["b@x.com"]),
        _message(id="m2", threadId="t1", sender="b@x.com", toRecipients=["a@x.com"]),
    ]
    path = _write(tmp_path, messages)

    run_raw_file_ingestion(db, path)

    thread = ThreadRepository(db).find_one({"thread_id": "t1"})
    assert thread is not None
    assert set(thread["message_ids"]) == {"m1", "m2"}
    assert set(thread["participant_emails"]) == {"a@x.com", "b@x.com"}


# --- 5. Idempotent second run ---


def test_second_run_of_the_same_file_is_a_pure_no_op(db, tmp_path):
    messages = [_message(id=f"m{i}", threadId="t1") for i in range(5)]
    path = _write(tmp_path, messages)

    first = run_raw_file_ingestion(db, path)
    assert first.inserted == 5
    assert first.already_existed == 0

    emails_after_first = EmailRepository(db).find_many({})
    threads_after_first = ThreadRepository(db).find_many({})

    second = run_raw_file_ingestion(db, path)
    assert second.inserted == 0
    assert second.already_existed == 5

    assert EmailRepository(db).find_many({}) == emails_after_first  # byte-identical, nothing rewritten
    assert ThreadRepository(db).find_many({}) == threads_after_first
    assert len(EmailRepository(db).find_many({})) == 5  # no duplicates
    assert len(ThreadRepository(db).find_many({})) == 1  # no duplicate thread


def test_raw_ingestion_never_overwrites_an_already_enriched_email(db, tmp_path):
    # Simulates an email already processed by the full AI `file` mode (has fields raw
    # ingestion never writes, like processing_status/entities_referenced) -- a later
    # raw-file run over the same dataset must never touch or downgrade it.
    EmailRepository(db).upsert_by_key(
        {"message_id": "18f348ce69f386be"},
        {
            "message_id": "18f348ce69f386be", "thread_id": "18f348ce69f386be", "subject": "old",
            "from": {"name": None, "email": "john.t@614group.com"}, "to": [], "cc": [],
            "body": "old body", "timestamp": "2024-05-01T14:26:28Z", "labels": [],
            "processing_status": {"stage": "COMPLETED", "error": None},
            "entities_referenced": {"people": ["PER-001"]},
        },
    )
    path = _write(tmp_path, [_message()])

    summary = run_raw_file_ingestion(db, path)

    assert summary.already_existed == 1
    assert summary.inserted == 0
    stored = EmailRepository(db).find_one({"message_id": "18f348ce69f386be"})
    assert stored["processing_status"]["stage"] == "COMPLETED"
    assert stored["entities_referenced"] == {"people": ["PER-001"]}
    assert stored["subject"] == "old"  # untouched, not overwritten with the raw file's "subject"


# --- 6. Missing required fields ---


def test_message_missing_a_required_raw_field_is_counted_as_skipped(db, tmp_path):
    good = _message(id="good_id")
    bad = _message(id="bad_id")
    del bad["subject"]  # FileEmailProvider's own required-field pre-filter
    path = _write(tmp_path, [good, bad])

    summary = run_raw_file_ingestion(db, path)

    assert summary.input_messages == 2
    assert summary.inserted == 1
    assert summary.skipped == 1
    assert summary.failed == 0


def test_message_with_invalid_email_address_is_counted_as_failed(db, tmp_path):
    good = _message(id="good_id")
    bad = _message(id="bad_id", sender="not-a-valid-email")
    path = _write(tmp_path, [good, bad])

    summary = run_raw_file_ingestion(db, path)

    assert summary.inserted == 1
    assert summary.failed == 1
    assert summary.failed_message_ids == ["bad_id"]


def test_invalid_top_level_structure_raises_a_clear_error(db, tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not_messages": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="messages"):
        run_raw_file_ingestion(db, str(path))


# --- 7. No LLM provider instantiated/called ---


def test_no_llm_provider_module_is_imported_by_raw_ingestion():
    # Direct check: the module's own source never references a concrete LLM provider
    # class or the provider factory at all -- so no LLM provider can ever be
    # instantiated on this path, by construction, not just "didn't happen to be called
    # in this test."
    import inspect

    import app.raw_ingestion

    source = inspect.getsource(app.raw_ingestion)
    for forbidden in ("ClaudeProvider", "OpenAIProvider", "ProviderFactory", "anthropic", "openai"):
        assert forbidden not in source


def test_raw_ingestion_runs_successfully_with_no_llm_api_key_configured(db, tmp_path, monkeypatch):
    # No LLM_API_KEY set at all -- if raw ingestion ever touched a real provider,
    # constructing it would fail loudly (ClaudeProvider/OpenAIProvider both require a
    # key). Successfully completing here is itself proof no provider was constructed.
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path = _write(tmp_path, [_message()])

    summary = run_raw_file_ingestion(db, path)

    assert summary.inserted == 1
