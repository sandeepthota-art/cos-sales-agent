import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    ContextSnapshotRepository,
    EmailRepository,
    KnowledgeRepository,
    ThreadRepository,
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


def _set_entity_metadata(repo, message_id, label_applied="1. Needs reply"):
    repo.set_entity_metadata(
        message_id=message_id,
        entities_referenced={},
        goal_pillar="Sales",
        label_applied=label_applied,
    )


def test_set_entity_metadata_writes_label_applied_and_appends_to_labels(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": []})

    _set_entity_metadata(repo, "msg_001", label_applied="1. Needs reply: ASAP")

    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["label_applied"] == "1. Needs reply: ASAP"
    assert doc["labels"] == ["1. Needs reply: ASAP"]


def test_set_entity_metadata_preserves_existing_raw_gmail_labels(db):
    # `labels` can already hold real Gmail label IDs from ingestion (see
    # providers.email.file.convert_gmail_message) -- set_entity_metadata must add
    # the triage classification alongside them, never overwrite or clear them.
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": ["IMPORTANT", "STARRED"]})

    _set_entity_metadata(repo, "msg_001", label_applied="1. Read only")

    doc = repo.find_one({"message_id": "msg_001"})
    assert set(doc["labels"]) == {"IMPORTANT", "STARRED", "1. Read only"}


def test_set_entity_metadata_does_not_duplicate_labels_on_reprocessing(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "labels": []})

    _set_entity_metadata(repo, "msg_001", label_applied="1. Delete")
    _set_entity_metadata(repo, "msg_001", label_applied="1. Delete")

    doc = repo.find_one({"message_id": "msg_001"})
    assert doc["labels"] == ["1. Delete"]


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


# --- update_existing (Finding #7, Priority E: never creates a document) -----------------------


def test_update_existing_updates_a_present_document(db):
    repo = EmailRepository(db)
    repo.upsert_by_key({"message_id": "msg_001"}, {"message_id": "msg_001", "subject": "Original"})

    result = repo.update_existing({"message_id": "msg_001"}, {"message_id": "msg_001", "subject": "Updated"})

    assert result == {"message_id": "msg_001", "subject": "Updated"}
    stored = repo.find_one({"message_id": "msg_001"})
    assert stored["subject"] == "Updated"


def test_update_existing_matching_but_unchanged_values_is_still_a_successful_match(db):
    """modified_count can be 0 when the written values are byte-identical to
    what's already stored -- that must still count as a successful match
    (matched_count == 1), not be confused with the target being absent."""
    repo = EmailRepository(db)
    document = {"message_id": "msg_001", "subject": "Same"}
    repo.upsert_by_key({"message_id": "msg_001"}, document)

    raw_result = db.emails.update_one({"message_id": "msg_001"}, {"$set": document}, upsert=False)
    assert raw_result.matched_count == 1
    assert raw_result.modified_count == 0  # confirms this scenario actually exercises the distinction

    result = repo.update_existing({"message_id": "msg_001"}, document)

    assert result == document  # reported as a successful match, not as missing


def test_update_existing_returns_none_and_writes_nothing_for_a_missing_target(db):
    repo = EmailRepository(db)

    result = repo.update_existing({"message_id": "does-not-exist"}, {"message_id": "does-not-exist", "subject": "X"})

    assert result is None
    assert db.emails.count_documents({}) == 0  # never created a document


# --- upsert_preserving_on_insert (concurrency-safety audit) -----------------------------------


def test_upsert_preserving_on_insert_sets_both_field_groups_on_first_insert(db):
    repo = EmailRepository(db)

    result = repo.upsert_preserving_on_insert(
        {"message_id": "msg_001"}, set_fields={"subject": "Hi"}, set_on_insert_fields={"created_by": "worker-a"},
    )

    assert result == {"message_id": "msg_001", "subject": "Hi", "created_by": "worker-a"}


def test_upsert_preserving_on_insert_never_overwrites_the_on_insert_fields_on_update(db):
    repo = EmailRepository(db)
    repo.upsert_preserving_on_insert(
        {"message_id": "msg_001"}, set_fields={"subject": "Hi"}, set_on_insert_fields={"created_by": "worker-a"},
    )

    result = repo.upsert_preserving_on_insert(
        {"message_id": "msg_001"}, set_fields={"subject": "Updated"}, set_on_insert_fields={"created_by": "worker-b"},
    )

    assert result["subject"] == "Updated"  # plain $set fields always apply
    assert result["created_by"] == "worker-a"  # $setOnInsert fields never overwritten by a later caller


# --- ThreadRepository.merge_message_atomic (concurrency-safety audit) -------------------------


def test_merge_message_atomic_adds_a_message_without_dropping_existing_ones(db):
    repo = ThreadRepository(db)
    repo.upsert_by_key(
        {"thread_id": "THR-001"},
        {
            "thread_id": "THR-001", "source_thread_id": "g1", "normalized_subject": "hello",
            "participant_emails": ["a@x.com"], "message_ids": ["EML-001"], "source_message_ids": ["s1"],
            "last_message_at": "2026-01-01T00:00:00",
        },
    )

    repo.merge_message_atomic(
        thread_id="THR-001", participant_emails=["b@x.com"], message_id="EML-002",
        source_message_id="s2", last_message_at_iso="2026-01-02T00:00:00",
    )

    stored = repo.find_one({"thread_id": "THR-001"})
    assert set(stored["message_ids"]) == {"EML-001", "EML-002"}
    assert set(stored["participant_emails"]) == {"a@x.com", "b@x.com"}
    assert stored["last_message_at"] == "2026-01-02T00:00:00"


def test_merge_message_atomic_never_regresses_last_message_at(db):
    repo = ThreadRepository(db)
    repo.upsert_by_key(
        {"thread_id": "THR-001"},
        {
            "thread_id": "THR-001", "source_thread_id": "g1", "normalized_subject": "hello",
            "participant_emails": ["a@x.com"], "message_ids": ["EML-001"], "source_message_ids": ["s1"],
            "last_message_at": "2026-01-05T00:00:00",
        },
    )

    repo.merge_message_atomic(
        thread_id="THR-001", participant_emails=["a@x.com"], message_id="EML-002",
        source_message_id="s2", last_message_at_iso="2026-01-01T00:00:00",  # earlier than what's stored
    )

    assert repo.find_one({"thread_id": "THR-001"})["last_message_at"] == "2026-01-05T00:00:00"


def test_merge_message_atomic_is_idempotent_for_the_same_message(db):
    repo = ThreadRepository(db)
    repo.upsert_by_key(
        {"thread_id": "THR-001"},
        {
            "thread_id": "THR-001", "source_thread_id": "g1", "normalized_subject": "hello",
            "participant_emails": ["a@x.com"], "message_ids": ["EML-001"], "source_message_ids": ["s1"],
            "last_message_at": "2026-01-01T00:00:00",
        },
    )

    repo.merge_message_atomic(
        thread_id="THR-001", participant_emails=["a@x.com"], message_id="EML-001",
        source_message_id="s1", last_message_at_iso="2026-01-01T00:00:00",
    )

    stored = repo.find_one({"thread_id": "THR-001"})
    assert stored["message_ids"] == ["EML-001"]  # no duplicate entry from a resubmitted/retried message
