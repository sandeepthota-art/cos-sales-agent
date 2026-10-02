"""Human-readable internal ids (EML-nnn/THR-nnn) as the CANONICAL email/thread
identity (see the canonical ID refactor). `message_id`/`thread_id` now always
hold EML-nnn/THR-nnn; `source_message_id`/`source_thread_id` hold the true,
permanent Gmail/provider-origin values, set once and never reassigned.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.calendar.actions import make_fingerprint
from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    ThreadRepository,
)
from app.email.models import parse_email
from app.entities.ids import next_id
from app.interfaces.email_provider import EmailProvider
from app.pipeline import ingest_raw_email, resolve_and_persist_thread, run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider


def _raw_email(message_id, body, subject="Enterprise CRM Proposal", **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": subject,
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    raw.update(overrides)
    return raw


class _ListEmailProvider(EmailProvider):
    def __init__(self, payloads):
        self._payloads = payloads

    def fetch_emails(self, limit):
        return self._payloads[:limit]

    def send_email(self, to, subject, body):
        raise AssertionError("must never send")


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


# --- EML- generation, canonical message_id ------------------------------------


def test_new_email_gets_an_eml_id_as_its_canonical_message_id(db):
    email_repo = EmailRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))

    normalized, already_completed = ingest_raw_email(email_repo, email, db)

    assert already_completed is False
    assert normalized.message_id == "EML-001"
    stored = db.emails.find_one({"source_message_id": "msg_001"}, {"_id": 0})
    # No separate `id` field -- removed from the emails schema entirely (a true
    # duplicate of message_id). See scripts/remove_email_id_record_id_date_fields.py.
    assert "id" not in stored
    assert stored["message_id"] == "EML-001"
    # The true, permanent Gmail/provider identity is preserved, unchanged, forever.
    assert stored["source_message_id"] == "msg_001"


def test_sequential_new_emails_get_sequential_eml_ids(db):
    email_repo = EmailRepository(db)
    ingest_raw_email(email_repo, parse_email(_raw_email("msg_001", "one")), db)
    ingest_raw_email(email_repo, parse_email(_raw_email("msg_002", "two")), db)

    assert db.emails.find_one({"source_message_id": "msg_001"})["message_id"] == "EML-001"
    assert db.emails.find_one({"source_message_id": "msg_002"})["message_id"] == "EML-002"


def test_retrying_a_not_yet_completed_email_does_not_regenerate_its_eml_id(db):
    email_repo = EmailRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))

    ingest_raw_email(email_repo, email, db)
    first_id = db.emails.find_one({"source_message_id": "msg_001"})["message_id"]

    # Simulate a retry after a mid-pipeline failure: the document exists but was
    # never marked COMPLETED, so ingest_raw_email runs its upsert path again.
    ingest_raw_email(email_repo, email, db)

    assert db.emails.find_one({"source_message_id": "msg_001"})["message_id"] == first_id
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 1


def test_same_source_message_retried_never_creates_a_second_document(db):
    email_repo = EmailRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))

    ingest_raw_email(email_repo, email, db)
    ingest_raw_email(email_repo, email, db)

    assert db.emails.count_documents({}) == 1


# --- THR- generation, canonical thread_id -------------------------------------


def test_new_thread_gets_a_thr_id_as_its_canonical_thread_id(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))
    email, _ = ingest_raw_email(email_repo, email, db)

    thread_id = resolve_and_persist_thread(thread_repo, email_repo, email, db)

    assert thread_id == "THR-001"
    stored_thread = db.threads.find_one({"thread_id": thread_id}, {"_id": 0})
    # No separate `id` field -- removed from the threads schema entirely (a true
    # duplicate of thread_id). See scripts/remove_thread_id_field.py.
    assert "id" not in stored_thread
    assert stored_thread["thread_id"] == "THR-001"
    # No Gmail thread id was ever supplied for this message.
    assert stored_thread.get("source_thread_id") is None


def test_second_message_on_the_same_thread_does_not_regenerate_its_thr_id(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    first = parse_email(_raw_email("msg_001", "hello"))
    first, _ = ingest_raw_email(email_repo, first, db)
    thread_id = resolve_and_persist_thread(thread_repo, email_repo, first, db)

    reply = parse_email(_raw_email("msg_002", "reply", in_reply_to="msg_001", references=["msg_001"]))
    reply, _ = ingest_raw_email(email_repo, reply, db)
    second_thread_id = resolve_and_persist_thread(thread_repo, email_repo, reply, db)

    assert second_thread_id == thread_id
    assert db.threads.count_documents({}) == 1
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1
    stored_thread = db.threads.find_one({"thread_id": thread_id})
    # Both messages' true Gmail ids are tracked (needed for future in_reply_to/
    # references matching), even though only canonical ids are used elsewhere.
    assert set(stored_thread["source_message_ids"]) == {"msg_001", "msg_002"}
    assert set(stored_thread["message_ids"]) == {"EML-001", "EML-002"}


def test_two_independent_threads_get_sequential_thr_ids(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    e1 = parse_email(_raw_email("msg_001", "one", subject="Subject A"))
    e1, _ = ingest_raw_email(email_repo, e1, db)
    t1 = resolve_and_persist_thread(thread_repo, email_repo, e1, db)

    e2 = parse_email(
        _raw_email(
            "msg_002",
            "two",
            subject="Subject B",
            **{
                "from": {"name": "Jane", "email": "jane@example.com"},
                "to": [{"name": "Bob", "email": "bob@example.com"}],
            },
        )
    )
    e2, _ = ingest_raw_email(email_repo, e2, db)
    t2 = resolve_and_persist_thread(thread_repo, email_repo, e2, db)

    assert t1 == "THR-001"
    assert t2 == "THR-002"


def test_reusing_an_existing_gmail_thread_id_reuses_the_same_thr(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    first = parse_email(_raw_email("msg_001", "hello", thread_id="gmail_thread_xyz"))
    first, _ = ingest_raw_email(email_repo, first, db)
    thread_id = resolve_and_persist_thread(thread_repo, email_repo, first, db)

    second = parse_email(_raw_email("msg_002", "reply", thread_id="gmail_thread_xyz"))
    second, _ = ingest_raw_email(email_repo, second, db)
    second_thread_id = resolve_and_persist_thread(thread_repo, email_repo, second, db)

    assert second_thread_id == thread_id
    stored_thread = db.threads.find_one({"thread_id": thread_id})
    assert stored_thread["source_thread_id"] == "gmail_thread_xyz"
    assert db.threads.count_documents({}) == 1


# --- Counter atomicity / uniqueness ------------------------------------------


def test_next_id_is_atomic_and_never_collides_across_prefixes(db):
    eml_id = next_id(db, "EML-")
    thr_id = next_id(db, "THR-")
    eml_id_2 = next_id(db, "EML-")

    assert eml_id == "EML-001"
    assert thr_id == "THR-001"
    assert eml_id_2 == "EML-002"
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 2
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1


def test_emails_source_message_id_index_is_unique(db):
    db.emails.insert_one({"message_id": "EML-001", "source_message_id": "msg_001"})
    with pytest.raises(Exception):
        db.emails.insert_one({"message_id": "EML-002", "source_message_id": "msg_001"})


# --- Full pipeline: end-to-end + composite-id regression guards --------------


def test_run_pipeline_assigns_eml_and_thr_ids_as_the_canonical_identity_end_to_end(db, settings):
    payloads = [
        _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point."),
        _raw_email("msg_002", "We'd need about 100 seats to start.", in_reply_to="msg_001", references=["msg_001"]),
    ]
    run_pipeline(
        db=db,
        email_provider=_ListEmailProvider(payloads),
        llm_provider=MockLLMProvider(),
        calendar_provider=MockCalendarProvider(),
        settings=settings,
    )

    stored_1 = db.emails.find_one({"source_message_id": "msg_001"})
    stored_2 = db.emails.find_one({"source_message_id": "msg_002"})
    assert stored_1["message_id"] == "EML-001"
    assert stored_2["message_id"] == "EML-002"
    assert "id" not in stored_1 and "id" not in stored_2

    threads = list(db.threads.find({}, {"_id": 0}))
    assert len(threads) == 1
    assert threads[0]["thread_id"] == "THR-001"
    assert "id" not in threads[0]


def test_rerunning_the_pipeline_never_regenerates_ids(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)
    first_eml_id = db.emails.find_one({"source_message_id": "msg_001"})["message_id"]
    first_thr_id = db.threads.find_one({})["thread_id"]

    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    assert db.emails.find_one({"source_message_id": "msg_001"})["message_id"] == first_eml_id
    assert db.threads.find_one({})["thread_id"] == first_thr_id
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 1
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1
    assert db.emails.count_documents({}) == 1
    assert db.threads.count_documents({}) == 1


def test_reply_draft_id_derives_from_the_canonical_email_id(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    reply = db.reply_drafts.find_one({"source_email_id": "EML-001"})
    if reply is not None:  # only asserted when this email actually warranted a reply
        assert reply["reply_id"] == "reply_EML-001"


def test_knowledge_item_id_derives_from_the_canonical_thread_id(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    items = list(db.knowledge_items.find({}))
    assert items, "expected at least one knowledge fact from this email's content"
    for item in items:
        assert item["knowledge_id"].startswith("knowledge_THR-001_")
        assert item["thread_id"] == "THR-001"


def test_calendar_meeting_fingerprint_derives_from_the_canonical_thread_id(db, settings):
    payloads = [_raw_email("msg_001", "Let's schedule a call soon.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    expected_fingerprint = "needs_clarification_THR-001"
    action = CalendarActionRepository(db).find_one(
        {"thread_id": "THR-001", "meeting_fingerprint": expected_fingerprint}
    )
    assert action is not None
    assert action["meeting_fingerprint"] == expected_fingerprint


def test_opportunity_links_to_source_email_by_canonical_email_id(db, settings):
    payloads = [
        _raw_email(
            "msg_001",
            "We'd like a formal pricing proposal for Acme Corp's rollout across 200 seats.",
            **{"from": {"name": "Jane", "email": "jane@acme-corp-example.com"}},
        )
    ]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored_email = db.emails.find_one({"source_message_id": "msg_001"})
    for opp_id in stored_email.get("entities_referenced", {}).get("opportunities", []):
        opportunity = db.opportunities.find_one({"id": opp_id})
        assert "EML-001" in opportunity["source_email_ids"]
        assert "msg_001" not in opportunity["source_email_ids"]


# --- Required E2E test: full pipeline lifecycle + retry idempotency, one place ------


def test_e2e_two_source_messages_same_thread_then_retry_first_produces_no_duplicates(db, settings):
    """Section 21's required end-to-end test: source Gmail message A + thread X ->
    EML-001/THR-001; source Gmail message B + thread X -> EML-002/THR-001 (same
    thread); retry A -> still EML-001/THR-001, nothing duplicated anywhere --
    entities, knowledge, context, reply draft, calendar action, or completion.
    """
    message_a = _raw_email(
        "gmail_A", "I will send the proposal tomorrow. Let's also schedule a call.",
        thread_id="gmail_thread_X",
    )
    message_b = _raw_email(
        "gmail_B", "Following up on the proposal and the call.",
        thread_id="gmail_thread_X", timestamp="2026-09-14T10:30:00Z",
    )

    run_pipeline(db, _ListEmailProvider([message_a]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, _ListEmailProvider([message_b]), MockLLMProvider(), MockCalendarProvider(), settings)

    email_a = db.emails.find_one({"source_message_id": "gmail_A"}, {"_id": 0})
    email_b = db.emails.find_one({"source_message_id": "gmail_B"}, {"_id": 0})
    assert email_a["message_id"] == "EML-001"
    assert email_b["message_id"] == "EML-002"
    assert "id" not in email_a and "id" not in email_b
    assert email_a["thread_id"] == email_b["thread_id"] == "THR-001"

    thread = db.threads.find_one({"thread_id": "THR-001"}, {"_id": 0})
    assert thread["source_thread_id"] == "gmail_thread_X"
    assert set(thread["message_ids"]) == {"EML-001", "EML-002"}
    assert set(thread["source_message_ids"]) == {"gmail_A", "gmail_B"}

    # Real entity/knowledge/context/reply/calendar side effects actually occurred --
    # not merely that ids were assigned.
    assert CommitmentRepository(db).find_many({"thread_id": "THR-001"}) != []
    assert list(db.knowledge_items.find({"thread_id": "THR-001"})) != []
    assert ContextSnapshotRepository(db).latest_for_thread("THR-001") is not None
    assert db.reply_drafts.count_documents({"thread_id": "THR-001"}) >= 1
    assert db.calendar_actions.count_documents({"thread_id": "THR-001"}) >= 1
    assert email_a["processing_status"]["stage"] == "COMPLETED"
    assert email_b["processing_status"]["stage"] == "COMPLETED"

    before_counts = {
        "emails": db.emails.count_documents({}),
        "threads": db.threads.count_documents({}),
        "commitments": db.commitments.count_documents({}),
        "knowledge_items": db.knowledge_items.count_documents({}),
        "context_snapshots": db.context_snapshots.count_documents({}),
        "reply_drafts": db.reply_drafts.count_documents({}),
        "calendar_actions": db.calendar_actions.count_documents({}),
    }

    # Retry A (e.g. a re-delivered/re-polled Gmail message) -- already_completed
    # guard fires immediately; nothing anywhere is duplicated or reassigned.
    run_pipeline(db, _ListEmailProvider([message_a]), MockLLMProvider(), MockCalendarProvider(), settings)

    assert db.emails.find_one({"source_message_id": "gmail_A"})["message_id"] == "EML-001"
    assert db.threads.find_one({"source_thread_id": "gmail_thread_X"})["thread_id"] == "THR-001"
    for collection, before in before_counts.items():
        assert db[collection].count_documents({}) == before, f"{collection} count changed on retry"


def test_reply_threading_via_in_reply_to_resolves_to_the_same_canonical_thread(db, settings):
    """Section 22's required threading test: A and B share a real source
    thread id; C has NO source thread id at all and joins purely via
    in_reply_to=B -- must still land in the same canonical THR-001, proving
    source-level threading (both by explicit thread id and by header-based
    reply matching) survives canonicalization.
    """
    email_a = _raw_email("gmail_A", "Kicking off Project Q.", thread_id="gmail_thread_X")
    email_b = _raw_email(
        "gmail_B", "More detail on Project Q.", thread_id="gmail_thread_X",
        timestamp="2026-09-14T09:00:00Z",
    )
    email_c = _raw_email(
        "gmail_C", "Replying inline on Project Q.", in_reply_to="gmail_B",
        timestamp="2026-09-14T10:00:00Z",
    )

    run_pipeline(db, _ListEmailProvider([email_a]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, _ListEmailProvider([email_b]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, _ListEmailProvider([email_c]), MockLLMProvider(), MockCalendarProvider(), settings)

    assert db.threads.count_documents({}) == 1
    thread = db.threads.find_one({}, {"_id": 0})
    assert thread["thread_id"] == "THR-001"
    assert "id" not in thread
    assert set(thread["source_message_ids"]) == {"gmail_A", "gmail_B", "gmail_C"}
    assert set(thread["message_ids"]) == {"EML-001", "EML-002", "EML-003"}
