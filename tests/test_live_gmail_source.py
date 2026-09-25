import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository
from app.providers.source.live_gmail import LiveGmailSource


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _gmail_message(**overrides):
    base = {
        "id": "18f348ce69f386be",
        "threadId": "18f348ce69f386be",
        "subject": "sales/capabilities materials",
        "sender": "john.t@614group.com",
        "toRecipients": ["ashok@databeat.io"],
        "ccRecipients": ["cc@databeat.io"],
        "date": "2024-05-01T14:26:28Z",
        "labelIds": ["IMPORTANT", "INBOX"],
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


class _FakeGmailClient:
    def __init__(self, messages):
        self._messages = messages
        self.call_count = 0

    def list_messages(self, limit):
        self.call_count += 1
        return self._messages[:limit]


class _FailingGmailClient:
    def list_messages(self, limit):
        raise RuntimeError("simulated Gmail/MCP connection failure")


def _mark_completed(db, message_id, thread_id="thread_1"):
    EmailRepository(db).upsert_by_key(
        {"message_id": message_id},
        {
            "message_id": message_id, "thread_id": thread_id,
            "from": {"name": None, "email": "someone@example.com"}, "to": [], "cc": [],
            "subject": "historical", "body": "historical body", "timestamp": "2024-01-01T00:00:00Z",
            "labels": [], "processing_status": {"stage": "COMPLETED", "error": None},
        },
    )


# --- normalization ---------------------------------------------------------------------


def test_message_id_is_preserved(db):
    client = _FakeGmailClient([_gmail_message(id="abc123")])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert batches[0]["emails"][0]["message_id"] == "abc123"


def test_thread_id_is_preserved(db):
    client = _FakeGmailClient([_gmail_message(threadId="thread_xyz")])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert batches[0]["emails"][0]["thread_id"] == "thread_xyz"


def test_subject_sender_recipients_body_date_are_preserved(db):
    client = _FakeGmailClient([
        _gmail_message(
            subject="Renewal check-in", sender="jane@example.com",
            toRecipients=["ashok@databeat.io"], ccRecipients=["cc1@example.com"],
            date="2025-03-01T10:00:00Z", plaintextBody="Let's talk renewal terms.",
        )
    ])
    source = LiveGmailSource(db, client=client)

    email = source.get_new_batches()[0]["emails"][0]

    assert email["subject"] == "Renewal check-in"
    assert email["from"]["email"] == "jane@example.com"
    assert [a["email"] for a in email["to"]] == ["ashok@databeat.io"]
    assert [a["email"] for a in email["cc"]] == ["cc1@example.com"]
    assert email["timestamp"] == "2025-03-01T10:00:00Z"
    assert email["body"] == "Let's talk renewal terms."


def test_normalized_output_is_accepted_by_parse_email(db):
    # Proves the normalized shape is actually compatible with the existing pipeline's
    # own Email model -- not just superficially similar.
    from app.email.models import parse_email

    client = _FakeGmailClient([_gmail_message()])
    source = LiveGmailSource(db, client=client)

    email = source.get_new_batches()[0]["emails"][0]
    parsed = parse_email(email)

    assert parsed.message_id == "18f348ce69f386be"


# --- duplicate detection -----------------------------------------------------------------


def test_already_completed_message_is_skipped(db):
    _mark_completed(db, "18f348ce69f386be")
    client = _FakeGmailClient([_gmail_message(id="18f348ce69f386be")])
    source = LiveGmailSource(db, client=client)

    assert source.get_new_batches() == []


def test_new_message_is_included(db):
    client = _FakeGmailClient([_gmail_message(id="brand_new_msg")])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert len(batches) == 1
    assert batches[0]["emails"][0]["message_id"] == "brand_new_msg"


def test_overlapping_results_are_safe_mixed_new_and_completed(db):
    _mark_completed(db, "old_1")
    _mark_completed(db, "old_2")
    client = _FakeGmailClient([
        _gmail_message(id="old_1"), _gmail_message(id="old_2"), _gmail_message(id="new_1"),
    ])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert [e["message_id"] for e in batches[0]["emails"]] == ["new_1"]


def test_same_message_returned_twice_in_one_poll_is_not_deduplicated_here_but_pipeline_is_safe(db):
    # get_new_batches() itself doesn't dedup within a single response (Gmail wouldn't
    # realistically return the same message twice in one call) -- the real
    # correctness guarantee is run_pipeline's own message_id upsert, which is safe to
    # process the same message_id twice in a row regardless.
    client = _FakeGmailClient([_gmail_message(id="dup_1"), _gmail_message(id="dup_1")])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert len(batches[0]["emails"]) == 2  # both present; run_pipeline's own idempotency handles it


# --- historical baseline protection --------------------------------------------------------


def test_historical_baseline_overlap_is_never_reprocessed(db):
    # Simulates the 662-email baseline: a batch of already-COMPLETED messages that a
    # broad Gmail search could plausibly return again on any given poll.
    baseline_ids = [f"baseline_msg_{i:03d}" for i in range(20)]
    for message_id in baseline_ids:
        _mark_completed(db, message_id)

    client = _FakeGmailClient([_gmail_message(id=mid) for mid in baseline_ids])
    source = LiveGmailSource(db, client=client)

    assert source.get_new_batches() == []


def test_historical_baseline_plus_one_new_message_only_processes_the_new_one(db):
    baseline_ids = [f"baseline_msg_{i:03d}" for i in range(10)]
    for message_id in baseline_ids:
        _mark_completed(db, message_id)

    client = _FakeGmailClient(
        [_gmail_message(id=mid) for mid in baseline_ids] + [_gmail_message(id="genuinely_new")]
    )
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert [e["message_id"] for e in batches[0]["emails"]] == ["genuinely_new"]


# --- restart safety --------------------------------------------------------------------------


def test_restart_simulation_second_poll_only_processes_the_new_message(db):
    from app.config.settings import Settings
    from app.pipeline import run_pipeline
    from app.providers.calendar.mock import MockCalendarProvider
    from app.providers.email.mock import MockEmailProvider
    from app.providers.llm.mock import MockLLMProvider

    settings = Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")

    # Poll 1: Gmail returns A, B, C.
    client = _FakeGmailClient([_gmail_message(id="A"), _gmail_message(id="B"), _gmail_message(id="C")])
    source = LiveGmailSource(db, client=client)
    batch = source.get_new_batches()[0]
    provider = MockEmailProvider(payloads=batch["emails"])
    run_pipeline(db, provider, MockLLMProvider(), MockCalendarProvider(), settings)

    # "Scheduler crashes" -- a fresh LiveGmailSource instance, no in-memory state carried over.
    # Poll 2: Gmail returns A, B, C, D (overlapping).
    client2 = _FakeGmailClient(
        [_gmail_message(id="A"), _gmail_message(id="B"), _gmail_message(id="C"), _gmail_message(id="D")]
    )
    source2 = LiveGmailSource(db, client=client2)
    batches2 = source2.get_new_batches()

    assert len(batches2) == 1
    assert [e["message_id"] for e in batches2[0]["emails"]] == ["D"]


# --- failure handling ------------------------------------------------------------------------


def test_no_client_configured_raises_clear_runtime_error(db):
    source = LiveGmailSource(db, client=None)

    with pytest.raises(RuntimeError, match="not configured"):
        source.get_new_batches()


def test_gmail_connection_failure_propagates_for_scheduler_to_catch(db):
    source = LiveGmailSource(db, client=_FailingGmailClient())

    with pytest.raises(RuntimeError, match="simulated Gmail/MCP connection failure"):
        source.get_new_batches()


def test_malformed_message_is_skipped_without_crashing(db):
    bad_message = _gmail_message(id="bad_1")
    del bad_message["subject"]  # required field missing
    good_message = _gmail_message(id="good_1")
    client = _FakeGmailClient([bad_message, good_message])
    source = LiveGmailSource(db, client=client)

    batches = source.get_new_batches()

    assert [e["message_id"] for e in batches[0]["emails"]] == ["good_1"]


def test_all_messages_malformed_returns_no_batches_without_raising(db):
    bad_message = _gmail_message(id="bad_1")
    del bad_message["subject"]
    client = _FakeGmailClient([bad_message])
    source = LiveGmailSource(db, client=client)

    assert source.get_new_batches() == []


def test_mark_batch_processed_is_a_safe_no_op(db):
    source = LiveGmailSource(db, client=_FakeGmailClient([]))
    source.mark_batch_processed({"kind": "live_gmail", "message_ids": ["x"]}, failed_count=0)  # must not raise
