"""Chronological thread-history context passed into email classification -- the
approved design from the ID Architecture Audit's follow-up: a new, additive
signal alongside the existing rolling context_snapshot, never a replacement.
"""
import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import EmailRepository, ThreadRepository
from app.interfaces.email_provider import EmailProvider
from app.pipeline import build_thread_timeline, run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.llm.thread_history import format_thread_history


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _email_doc(message_id, timestamp, body="hi", subject="Subject"):
    return {
        "message_id": message_id,
        "timestamp": timestamp,
        "subject": subject,
        "body": body,
        "from": {"name": "John", "email": "john@example.com"},
    }


# --- build_thread_timeline ---------------------------------------------------


def test_returns_empty_list_for_a_nonexistent_thread(db):
    assert build_thread_timeline(EmailRepository(db), ThreadRepository(db), "thread_missing") == []


def test_returns_empty_list_for_a_threads_first_message(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    email_repo.upsert_by_key({"message_id": "msg_001"}, _email_doc("msg_001", "2026-01-01T00:00:00Z"))
    thread_repo.upsert_by_key(
        {"thread_id": "t1"},
        {"thread_id": "t1", "message_ids": ["msg_001"], "normalized_subject": "s", "participant_emails": [], "last_message_at": "2026-01-01T00:00:00Z"},
    )

    timeline = build_thread_timeline(email_repo, thread_repo, "t1", exclude_message_id="msg_001")

    assert timeline == []


def test_returns_prior_messages_chronologically_oldest_first(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    email_repo.upsert_by_key({"message_id": "msg_002"}, _email_doc("msg_002", "2026-01-02T00:00:00Z", body="second"))
    email_repo.upsert_by_key({"message_id": "msg_001"}, _email_doc("msg_001", "2026-01-01T00:00:00Z", body="first"))
    email_repo.upsert_by_key({"message_id": "msg_003"}, _email_doc("msg_003", "2026-01-03T00:00:00Z", body="third"))
    thread_repo.upsert_by_key(
        {"thread_id": "t1"},
        {
            "thread_id": "t1", "message_ids": ["msg_001", "msg_002", "msg_003"],
            "normalized_subject": "s", "participant_emails": [], "last_message_at": "2026-01-03T00:00:00Z",
        },
    )

    # msg_003 is the "new" message being classified -- excluded from its own history.
    timeline = build_thread_timeline(email_repo, thread_repo, "t1", exclude_message_id="msg_003")

    assert [m["message_id"] for m in timeline] == ["msg_001", "msg_002"]
    assert [m["body"] for m in timeline] == ["first", "second"]


def test_caps_at_the_most_recent_limit_messages(db):
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    message_ids = []
    for i in range(25):
        mid = f"msg_{i:03d}"
        message_ids.append(mid)
        email_repo.upsert_by_key({"message_id": mid}, _email_doc(mid, f"2026-01-{i + 1:02d}T00:00:00Z"))
    thread_repo.upsert_by_key(
        {"thread_id": "t1"},
        {
            "thread_id": "t1", "message_ids": message_ids,
            "normalized_subject": "s", "participant_emails": [], "last_message_at": "2026-01-25T00:00:00Z",
        },
    )

    timeline = build_thread_timeline(email_repo, thread_repo, "t1", exclude_message_id="msg_024", limit=20)

    assert len(timeline) == 20
    # The 20 most recent of the 24 remaining (msg_000..msg_023), i.e. msg_004..msg_023.
    assert timeline[0]["message_id"] == "msg_004"
    assert timeline[-1]["message_id"] == "msg_023"


def test_default_limit_is_20():
    import inspect

    sig = inspect.signature(build_thread_timeline)
    assert sig.parameters["limit"].default == 20


# --- format_thread_history ----------------------------------------------------


def test_format_thread_history_empty_for_none():
    assert format_thread_history(None) == ""


def test_format_thread_history_empty_for_empty_list():
    assert format_thread_history([]) == ""


def test_format_thread_history_renders_sender_subject_and_body():
    rendered = format_thread_history(
        [{"from": {"name": "John", "email": "john@example.com"}, "timestamp": "2026-01-01T00:00:00Z",
          "subject": "Pricing", "body": "Can you send pricing?"}]
    )
    assert "John" in rendered
    assert "Pricing" in rendered
    assert "Can you send pricing?" in rendered
    assert "Prior messages in this thread" in rendered


def test_format_thread_history_falls_back_to_email_when_no_name():
    rendered = format_thread_history(
        [{"from": {"email": "jane@example.com"}, "timestamp": "2026-01-01T00:00:00Z",
          "subject": "s", "body": "b"}]
    )
    assert "jane@example.com" in rendered


def test_format_thread_history_orders_multiple_messages_as_given():
    rendered = format_thread_history(
        [
            {"from": {"name": "A"}, "timestamp": "t1", "subject": "s1", "body": "first message body"},
            {"from": {"name": "B"}, "timestamp": "t2", "subject": "s2", "body": "second message body"},
        ]
    )
    assert rendered.index("first message body") < rendered.index("second message body")


# --- end-to-end: run_pipeline actually threads history into the LLM call -----


class _ListEmailProvider(EmailProvider):
    def __init__(self, payloads):
        self._payloads = payloads

    def fetch_emails(self, limit):
        return self._payloads[:limit]

    def send_email(self, to, subject, body):
        raise AssertionError("must never send")


class _SpyLLMProvider(MockLLMProvider):
    """Wraps MockLLMProvider but records what thread_history it was called with
    for each message_id, so the test can assert on it without needing a real
    Claude/OpenAI call."""

    def __init__(self):
        self.calls: dict[str, list] = {}

    def analyze_email(self, email, thread_history=None, person_context=None):
        self.calls[email.message_id] = thread_history
        return super().analyze_email(email, thread_history=thread_history)


def _raw_email(message_id, body, timestamp="2026-09-13T10:30:00Z", **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Enterprise CRM Proposal",
        "body": body,
        "timestamp": timestamp,
    }
    raw.update(overrides)
    return raw


def test_run_pipeline_passes_no_history_for_a_threads_first_message(db):
    settings = Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")
    spy = _SpyLLMProvider()
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]

    run_pipeline(db, _ListEmailProvider(payloads), spy, MockCalendarProvider(), settings)

    assert spy.calls["EML-001"] == []


def test_run_pipeline_passes_prior_message_as_history_for_a_reply(db):
    settings = Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")
    spy = _SpyLLMProvider()
    payloads = [
        _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point."),
        _raw_email(
            "msg_002", "We'd need about 100 seats to start.",
            timestamp="2026-09-14T10:30:00Z", in_reply_to="msg_001", references=["msg_001"],
        ),
    ]

    run_pipeline(db, _ListEmailProvider(payloads), spy, MockCalendarProvider(), settings)

    assert spy.calls["EML-001"] == []
    assert [m["message_id"] for m in spy.calls["EML-002"]] == ["EML-001"]
    assert spy.calls["EML-002"][0]["body"] == "We currently use Salesforce but pricing is a pain point."
