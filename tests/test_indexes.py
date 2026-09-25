import mongomock

from app.database.indexes import initialize_indexes


def test_initialize_indexes_creates_expected_unique_indexes():
    client = mongomock.MongoClient()
    db = client["cos_sales_test"]

    initialize_indexes(db)
    initialize_indexes(db)  # must be safe to call twice

    def index_keys(collection_name):
        return {
            tuple(spec["key"]): spec.get("unique", False)
            for spec in db[collection_name].index_information().values()
            if spec["key"] != [("_id", 1)] and spec.get("unique", False)
        }

    assert index_keys("emails") == {(("message_id", 1),): True}
    assert index_keys("threads") == {(("thread_id", 1),): True}
    assert index_keys("context_snapshots") == {
        (("thread_id", 1), ("triggering_email_id", 1)): True
    }
    assert index_keys("knowledge_items") == {
        (("thread_id", 1), ("subject_key", 1), ("predicate", 1), ("fact_key", 1)): True
    }
    assert index_keys("reply_drafts") == {(("source_email_id", 1),): True}
    assert index_keys("calendar_actions") == {
        (("thread_id", 1), ("meeting_fingerprint", 1)): True
    }
    assert index_keys("ingested_files") == {(("filename", 1),): True}


def test_initialize_indexes_creates_people_open_threads_index():
    # Supports resolve_person's no-email thread-scoped lookup
    # (db.people.find({"open_threads": thread_id})) -- non-unique, since many People can
    # share a thread.
    client = mongomock.MongoClient()
    db = client["cos_sales_test"]

    initialize_indexes(db)

    all_keys = {tuple(spec["key"]) for spec in db["people"].index_information().values()}
    assert (("open_threads", 1),) in all_keys
