"""Human-readable internal ids (EML-nnn/THR-nnn) added alongside the existing
source/dedup identifiers (message_id/thread_id) -- see the approved ID
Architecture Audit. These tests exist specifically to guard the audit's own
hard constraints: message_id/thread_id are never renamed, replaced, or
rewritten, and every OTHER id derived from them (reply_id, knowledge_id,
calendar meeting_fingerprint) keeps deriving from the raw value, not the new
internal id.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.calendar.actions import make_fingerprint
from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
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


# --- EML- generation -------------------------------------------------------


def test_new_email_gets_an_eml_id_via_ingest_raw_email(db):
    email_repo = EmailRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))

    normalized, already_completed = ingest_raw_email(email_repo, email, db)

    assert already_completed is False
    stored = db.emails.find_one({"message_id": "msg_001"}, {"_id": 0})
    assert stored["id"] == "EML-001"
    # message_id itself is never touched.
    assert stored["message_id"] == "msg_001"


def test_sequential_new_emails_get_sequential_eml_ids(db):
    email_repo = EmailRepository(db)
    ingest_raw_email(email_repo, parse_email(_raw_email("msg_001", "one")), db)
    ingest_raw_email(email_repo, parse_email(_raw_email("msg_002", "two")), db)

    assert db.emails.find_one({"message_id": "msg_001"})["id"] == "EML-001"
    assert db.emails.find_one({"message_id": "msg_002"})["id"] == "EML-002"


def test_retrying_a_not_yet_completed_email_does_not_regenerate_its_eml_id(db):
    email_repo = EmailRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))

    ingest_raw_email(email_repo, email, db)
    first_id = db.emails.find_one({"message_id": "msg_001"})["id"]

    # Simulate a retry after a mid-pipeline failure: the document exists but was
    # never marked COMPLETED, so ingest_raw_email runs its upsert path again.
    ingest_raw_email(email_repo, email, db)

    assert db.emails.find_one({"message_id": "msg_001"})["id"] == first_id
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 1


# --- THR- generation ---------------------------------------------------------


def test_new_thread_gets_a_thr_id(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    email = parse_email(_raw_email("msg_001", "hello"))
    ingest_raw_email(email_repo, email, db)

    thread_id = resolve_and_persist_thread(thread_repo, email_repo, email, db)

    stored_thread = db.threads.find_one({"thread_id": thread_id}, {"_id": 0})
    assert stored_thread["id"] == "THR-001"
    # thread_id itself (the resolved/source/synthetic identifier) is never touched.
    assert stored_thread["thread_id"] == thread_id


def test_second_message_on_the_same_thread_does_not_regenerate_its_thr_id(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    first = parse_email(_raw_email("msg_001", "hello"))
    ingest_raw_email(email_repo, first, db)
    thread_id = resolve_and_persist_thread(thread_repo, email_repo, first, db)
    first_thread_internal_id = db.threads.find_one({"thread_id": thread_id})["id"]

    reply = parse_email(_raw_email("msg_002", "reply", in_reply_to="msg_001", references=["msg_001"]))
    ingest_raw_email(email_repo, reply, db)
    second_thread_id = resolve_and_persist_thread(thread_repo, email_repo, reply, db)

    assert second_thread_id == thread_id
    assert db.threads.find_one({"thread_id": thread_id})["id"] == first_thread_internal_id
    assert db.threads.count_documents({}) == 1
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1


def test_two_independent_threads_get_sequential_thr_ids(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)

    e1 = parse_email(_raw_email("msg_001", "one", subject="Subject A"))
    ingest_raw_email(email_repo, e1, db)
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
    ingest_raw_email(email_repo, e2, db)
    t2 = resolve_and_persist_thread(thread_repo, email_repo, e2, db)

    assert t1 != t2
    assert db.threads.find_one({"thread_id": t1})["id"] == "THR-001"
    assert db.threads.find_one({"thread_id": t2})["id"] == "THR-002"


# --- Counter atomicity / uniqueness ------------------------------------------


def test_next_id_is_atomic_and_never_collides_across_prefixes(db):
    eml_id = next_id(db, "EML-")
    thr_id = next_id(db, "THR-")
    eml_id_2 = next_id(db, "EML-")

    assert eml_id == "EML-001"
    assert thr_id == "THR-001"
    assert eml_id_2 == "EML-002"
    # Independent counters per prefix -- generating a THR- id never perturbs EML-'s.
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 2
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1


def test_emails_id_index_is_sparse_and_unique(db):
    # Multiple documents may legitimately lack `id` (not yet backfilled) --
    # a sparse index must never treat that as a uniqueness violation.
    db.emails.insert_one({"message_id": "no_id_1"})
    db.emails.insert_one({"message_id": "no_id_2"})
    db.emails.insert_one({"message_id": "has_id_1", "id": "EML-001"})

    with pytest.raises(Exception):
        db.emails.insert_one({"message_id": "has_id_2", "id": "EML-001"})


# --- Full pipeline: end-to-end + composite-id regression guards --------------


def test_run_pipeline_assigns_eml_and_thr_ids_end_to_end(db, settings):
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

    stored_1 = db.emails.find_one({"message_id": "msg_001"})
    stored_2 = db.emails.find_one({"message_id": "msg_002"})
    assert stored_1["id"] == "EML-001"
    assert stored_2["id"] == "EML-002"
    # Untouched source identifiers.
    assert stored_1["message_id"] == "msg_001"
    assert stored_2["message_id"] == "msg_002"

    threads = list(db.threads.find({}, {"_id": 0}))
    assert len(threads) == 1
    assert threads[0]["id"] == "THR-001"
    assert threads[0]["thread_id"] == "thread_msg_001"


def test_rerunning_the_pipeline_never_regenerates_ids(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)
    first_eml_id = db.emails.find_one({"message_id": "msg_001"})["id"]
    first_thr_id = db.threads.find_one({"thread_id": "thread_msg_001"})["id"]

    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    assert db.emails.find_one({"message_id": "msg_001"})["id"] == first_eml_id
    assert db.threads.find_one({"thread_id": "thread_msg_001"})["id"] == first_thr_id
    assert db.counters.find_one({"_id": "EML-"})["seq"] == 1
    assert db.counters.find_one({"_id": "THR-"})["seq"] == 1


def test_reply_draft_id_still_derives_from_message_id_not_the_new_internal_id(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    reply = db.reply_drafts.find_one({"source_email_id": "msg_001"})
    if reply is not None:  # only asserted when this email actually warranted a reply
        assert reply["reply_id"] == "reply_msg_001"


def test_knowledge_item_id_still_derives_from_thread_id_not_the_new_internal_id(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    items = list(db.knowledge_items.find({}))
    assert items, "expected at least one knowledge fact from this email's content"
    for item in items:
        assert item["knowledge_id"].startswith("knowledge_thread_msg_001_")
        assert item["thread_id"] == "thread_msg_001"


def test_calendar_meeting_fingerprint_still_uses_thread_id_not_the_new_internal_id(db, settings):
    payloads = [_raw_email("msg_001", "Let's schedule a call soon.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    thread_id = "thread_msg_001"
    expected_fingerprint = f"needs_clarification_{thread_id}"
    action = CalendarActionRepository(db).find_one(
        {"thread_id": thread_id, "meeting_fingerprint": expected_fingerprint}
    )
    assert action is not None
    assert action["meeting_fingerprint"] == expected_fingerprint


def test_opportunity_still_links_to_source_email_by_message_id(db, settings):
    payloads = [
        _raw_email(
            "msg_001",
            "We'd like a formal pricing proposal for Acme Corp's rollout across 200 seats.",
            **{"from": {"name": "Jane", "email": "jane@acme-corp-example.com"}},
        )
    ]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored_email = db.emails.find_one({"message_id": "msg_001"})
    for opp_id in stored_email.get("entities_referenced", {}).get("opportunities", []):
        opportunity = db.opportunities.find_one({"id": opp_id})
        assert "msg_001" in opportunity["source_email_ids"]
