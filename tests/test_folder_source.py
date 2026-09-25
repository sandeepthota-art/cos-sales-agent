import json

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import IngestedFileRepository
from app.providers.source.folder import FolderSource


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


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
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


def _write_export(folder, name, messages):
    path = folder / name
    path.write_text(
        json.dumps({"messages": messages}),
        encoding="utf-8",
    )
    return path


def test_discovers_all_files_on_first_scan(db, tmp_path):
    _write_export(tmp_path, "a.json", [_message(id="m1", threadId="t1")])
    _write_export(tmp_path, "b.json", [_message(id="m2", threadId="t2")])
    source = FolderSource(db, str(tmp_path))

    batches = source.get_new_batches()

    assert len(batches) == 2
    all_ids = {e["message_id"] for b in batches for e in b["emails"]}
    assert all_ids == {"m1", "m2"}


def test_returns_no_batches_when_folder_empty(db, tmp_path):
    source = FolderSource(db, str(tmp_path))

    assert source.get_new_batches() == []


def test_creates_the_watched_folder_if_missing(db, tmp_path):
    missing = tmp_path / "not_yet_created"
    source = FolderSource(db, str(missing))

    batches = source.get_new_batches()

    assert batches == []
    assert missing.is_dir()


def test_skips_a_fully_ingested_unchanged_file_on_next_scan(db, tmp_path):
    _write_export(tmp_path, "a.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))

    first = source.get_new_batches()
    source.mark_batch_processed(first[0]["source_ref"], failed_count=0)
    second = source.get_new_batches()

    assert second == []


def test_detects_a_changed_file_even_with_the_same_filename(db, tmp_path):
    _write_export(tmp_path, "a.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))
    first = source.get_new_batches()
    source.mark_batch_processed(first[0]["source_ref"], failed_count=0)

    _write_export(tmp_path, "a.json", [_message(id="m1"), _message(id="m2", threadId="t2")])
    second = source.get_new_batches()

    assert len(second) == 1
    assert {e["message_id"] for e in second[0]["emails"]} == {"m1", "m2"}


def test_retries_a_file_whose_last_batch_had_failures(db, tmp_path):
    _write_export(tmp_path, "a.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))
    first = source.get_new_batches()
    source.mark_batch_processed(first[0]["source_ref"], failed_count=1)

    second = source.get_new_batches()

    assert len(second) == 1


def test_skips_malformed_json_file_without_raising(db, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"not_messages": []}), encoding="utf-8")
    _write_export(tmp_path, "good.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))

    batches = source.get_new_batches()

    assert len(batches) == 1
    assert batches[0]["emails"][0]["message_id"] == "m1"


def test_ignores_non_json_files_in_the_folder(db, tmp_path):
    (tmp_path / "readme.txt").write_text("not an export", encoding="utf-8")
    _write_export(tmp_path, "good.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))

    batches = source.get_new_batches()

    assert len(batches) == 1


def test_records_but_does_not_batch_a_file_where_every_message_is_malformed(db, tmp_path):
    bad_message = _message(id="m1")
    del bad_message["subject"]  # FileEmailProvider's own required-field pre-filter
    _write_export(tmp_path, "all_bad.json", [bad_message])
    source = FolderSource(db, str(tmp_path))

    batches = source.get_new_batches()

    assert batches == []
    record = IngestedFileRepository(db).find_one({"filename": "all_bad.json"})
    assert record is not None
    assert record["all_completed"] is True
    assert record["message_count"] == 0


def test_mark_batch_processed_persists_fingerprint_and_completion_flag_in_mongo(db, tmp_path):
    _write_export(tmp_path, "a.json", [_message(id="m1")])
    source = FolderSource(db, str(tmp_path))
    batch = source.get_new_batches()[0]

    source.mark_batch_processed(batch["source_ref"], failed_count=0)

    record = IngestedFileRepository(db).find_one({"filename": "a.json"})
    assert record["fingerprint"] == batch["source_ref"]["fingerprint"]
    assert record["all_completed"] is True
