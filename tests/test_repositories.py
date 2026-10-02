import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    ContextSnapshotRepository,
    EmailRepository,
    KnowledgeRepository,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


# --- find_many pagination (React/FastAPI migration, additive/backward-compatible) ---


def test_find_many_with_no_pagination_args_returns_everything_unordered(db):
    # Existing contract, must be completely unaffected: skip=0/limit=None by
    # default reproduce exactly the prior behavior.
    repo = EmailRepository(db)
    for i in range(3):
        repo.upsert_by_key({"message_id": f"m{i}"}, {"message_id": f"m{i}"})

    assert len(repo.find_many({})) == 3


def test_find_many_limit_caps_the_result_count(db):
    repo = EmailRepository(db)
    for i in range(5):
        repo.upsert_by_key({"message_id": f"m{i}"}, {"message_id": f"m{i}", "timestamp": f"2026-01-0{i + 1}"})

    assert len(repo.find_many({}, limit=2)) == 2


def test_find_many_skip_and_limit_paginate_in_sorted_order(db):
    repo = EmailRepository(db)
    for i in range(5):
        repo.upsert_by_key({"message_id": f"m{i}"}, {"message_id": f"m{i}", "timestamp": f"2026-01-0{i + 1}"})

    page_1 = repo.find_many({}, sort=[("timestamp", 1)], limit=2)
    page_2 = repo.find_many({}, sort=[("timestamp", 1)], skip=2, limit=2)

    assert [d["message_id"] for d in page_1] == ["m0", "m1"]
    assert [d["message_id"] for d in page_2] == ["m2", "m3"]


def test_find_many_sort_descending(db):
    repo = EmailRepository(db)
    for i in range(3):
        repo.upsert_by_key({"message_id": f"m{i}"}, {"message_id": f"m{i}", "timestamp": f"2026-01-0{i + 1}"})

    results = repo.find_many({}, sort=[("timestamp", -1)])

    assert [d["message_id"] for d in results] == ["m2", "m1", "m0"]


def test_email_repository_upsert_is_idempotent(db):
    repo = EmailRepository(db)
    doc = {"message_id": "msg_001", "subject": "Hi"}

    first = repo.upsert_by_key({"message_id": "msg_001"}, doc)
    second = repo.upsert_by_key({"message_id": "msg_001"}, doc)

    assert first["message_id"] == "msg_001"
    assert second["message_id"] == "msg_001"
    assert db.emails.count_documents({}) == 1


def test_email_repository_set_stage_updates_in_place(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001"})

    repo.set_stage("msg_001", "ANALYZED")
    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["processing_status"]["stage"] == "ANALYZED"
    assert doc["processing_status"]["error"] is None

    repo.set_stage("msg_001", "FAILED", error="boom", failed_stage="ANALYZED")
    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["processing_status"]["stage"] == "FAILED"
    assert doc["processing_status"]["error"] == "boom"
    assert doc["processing_status"]["failed_stage"] == "ANALYZED"


def test_email_repository_set_stage_emits_a_structured_log_record(db, caplog):
    # BRD gap-analysis C1: thread_id/duration_ms/error_type are logging-only -- none
    # of the three change the persisted document (asserted above already).
    import logging

    repo = EmailRepository(db)
    with caplog.at_level(logging.INFO, logger="app.pipeline.stage"):
        repo.set_stage(
            "msg_001", "FAILED", error="boom", failed_stage="ANALYZED",
            thread_id="t1", duration_ms=42.0, error_type="ValueError",
        )

    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.message_id == "msg_001"
    assert record.thread_id == "t1"
    assert record.stage == "FAILED"
    assert record.failed_stage == "ANALYZED"
    assert record.duration_ms == 42.0
    assert record.error_type == "ValueError"
    assert record.error_message == "boom"


def _set_entity_metadata(repo, message_id, label_applied="Needs reply"):
    repo.set_entity_metadata(
        message_id=message_id,
        entities_referenced={},
        goal_pillar="Sales",
        label_applied=label_applied,
    )


def test_set_entity_metadata_writes_label_applied_and_appends_to_labels(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": []})

    _set_entity_metadata(repo, "msg_001", label_applied="Needs reply: ASAP")

    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["label_applied"] == "Needs reply: ASAP"
    assert doc["labels"] == ["Needs reply: ASAP"]


def test_set_entity_metadata_preserves_existing_raw_gmail_labels(db):
    # `labels` can already hold real Gmail label IDs from ingestion (see
    # providers.email.file.convert_gmail_message) -- set_entity_metadata must add
    # the triage classification alongside them, never overwrite or clear them.
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": ["IMPORTANT", "STARRED"]})

    _set_entity_metadata(repo, "msg_001", label_applied="Read only")

    doc = repo.find_one({"message_id": "msg_001"})
    assert set(doc["labels"]) == {"IMPORTANT", "STARRED", "Read only"}


def test_set_entity_metadata_does_not_duplicate_labels_on_reprocessing(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": []})

    _set_entity_metadata(repo, "msg_001", label_applied="Delete")
    _set_entity_metadata(repo, "msg_001", label_applied="Delete")

    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["labels"] == ["Delete"]


def test_context_snapshot_repository_latest_for_thread(db):
    repo = ContextSnapshotRepository(db)
    repo.upsert_by_key(
        {"thread_id": "t1", "triggering_email_id": "msg_001"},
        {"thread_id": "t1", "triggering_email_id": "msg_001", "context_version": 1},
    )
    repo.upsert_by_key(
        {"thread_id": "t1", "triggering_email_id": "msg_002"},
        {"thread_id": "t1", "triggering_email_id": "msg_002", "context_version": 2},
    )
    latest = repo.latest_for_thread("t1")
    assert latest["context_version"] == 2


def test_context_snapshot_repository_reprocessing_same_email_is_noop(db):
    repo = ContextSnapshotRepository(db)
    key = {"thread_id": "t1", "triggering_email_id": "msg_001"}
    repo.upsert_by_key(key, {**key, "context_version": 1})
    repo.upsert_by_key(key, {**key, "context_version": 1})
    assert db.context_snapshots.count_documents({}) == 1


def test_knowledge_repository_all_for_thread(db):
    repo = KnowledgeRepository(db)
    repo.upsert_by_key(
        {"thread_id": "t1", "subject_key": "abc_corp", "predicate": "requires", "fact_key": "seat_count"},
        {
            "thread_id": "t1",
            "subject_key": "abc_corp",
            "predicate": "requires",
            "fact_key": "seat_count",
            "current_value": "100 seats",
        },
    )
    items = repo.all_for_thread("t1")
    assert len(items) == 1
    assert items[0]["current_value"] == "100 seats"


def test_calendar_action_repository_dedupes_on_fingerprint(db):
    repo = CalendarActionRepository(db)
    key = {"thread_id": "t1", "meeting_fingerprint": "abc_2026-09-15t15:00_2026-09-15t16:00"}
    repo.upsert_by_key(key, {**key, "status": "awaiting_approval"})
    repo.upsert_by_key(key, {**key, "status": "approved"})
    assert db.calendar_actions.count_documents({}) == 1
    stored = repo.find_one(key)
    assert stored["status"] == "approved"
