import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    ContextSnapshotRepository,
    FollowUpRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider
from app.providers.source.live_gmail import LiveGmailSource
from app.scheduler import Scheduler, build_source


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    # agent_email matches _gmail_message's default "toRecipients" address --
    # envelope-based Person resolution (app.pipeline._process_entities) must never
    # create a Person for our own mailbox, so this has to line up with the fixture
    # data, not the class default.
    return Settings(
        email_provider="mock", calendar_provider="mock", llm_provider="mock",
        agent_email="ashok@example.com",
    )


def _gmail_message(**overrides):
    base = {
        "id": "gmail_msg_1", "threadId": "gmail_thread_1",
        "subject": "Enterprise CRM Proposal", "sender": "john@example.com",
        "toRecipients": ["ashok@example.com"], "ccRecipients": None,
        "date": "2026-09-13T10:30:00Z", "labelIds": ["INBOX"],
        "plaintextBody": "We currently use Salesforce but pricing has become a real "
                          "pain point. Could you send over pricing? I will send the "
                          "proposal tomorrow.",
    }
    base.update(overrides)
    return base


class _FakeGmailClient:
    def __init__(self, messages):
        self._messages = messages

    def list_messages(self, limit):
        return self._messages[:limit]


# --- Phase 12: end-to-end isolated integration test -----------------------------------------


def test_fake_gmail_flows_through_scheduler_and_pipeline_into_mongodb(db, settings):
    client = _FakeGmailClient([_gmail_message()])
    source = LiveGmailSource(db, client=client)
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert len(summaries) == 1
    assert summaries[0].completed == 1

    email = db.emails.find_one({"message_id": "gmail_msg_1"}, {"_id": 0})
    assert email is not None
    assert email["processing_status"]["stage"] == "COMPLETED"

    thread = ThreadRepository(db).find_one({"message_ids": "gmail_msg_1"})
    assert thread is not None
    thread_id = thread["thread_id"]

    assert ContextSnapshotRepository(db).find_one({"thread_id": thread_id}) is not None

    refs = email["entities_referenced"]
    assert len(refs["people"]) == 1
    # The body matches both commitment patterns: "Could you send..." (owed_to_me) and
    # "I will send the proposal tomorrow" (mine) -- two distinct commitments, each
    # with its own derived follow-up.
    assert len(refs["commitments"]) == 2
    assert len(refs["follow_ups"]) == 2

    commitments = CommitmentRepository(db).find_many({"id": {"$in": refs["commitments"]}})
    assert all(c["source_record"] == "gmail_msg_1" for c in commitments)

    follow_ups = FollowUpRepository(db).find_many({"id": {"$in": refs["follow_ups"]}})
    assert {f["commitment_id"] for f in follow_ups} == {c["id"] for c in commitments}

    # "Could you send over pricing?" is a question -> needs_reply() gate fires.
    draft = ReplyDraftRepository(db).find_one({"source_email_id": "gmail_msg_1"})
    assert draft is not None
    assert draft["status"] == "awaiting_approval"


def test_second_run_with_overlapping_gmail_results_creates_no_duplicates(db, settings):
    client1 = _FakeGmailClient([_gmail_message(id="A"), _gmail_message(id="B")])
    source1 = LiveGmailSource(db, client=client1)
    Scheduler(db, source1, MockLLMProvider(), MockCalendarProvider(), settings).run_once()

    client2 = _FakeGmailClient(
        [_gmail_message(id="A"), _gmail_message(id="B"), _gmail_message(id="C")]
    )
    source2 = LiveGmailSource(db, client=client2)
    summaries = Scheduler(db, source2, MockLLMProvider(), MockCalendarProvider(), settings).run_once()

    assert db.emails.count_documents({}) == 3
    assert summaries[0].processed == 1  # only C reached run_pipeline this cycle
    assert summaries[0].completed == 1


# --- batch/message failure isolation, specifically via LiveGmailSource -----------------------


def test_one_malformed_gmail_message_does_not_block_the_rest(db, settings):
    bad = _gmail_message(id="bad")
    del bad["subject"]
    client = _FakeGmailClient([bad, _gmail_message(id="good")])
    source = LiveGmailSource(db, client=client)
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert db.emails.count_documents({"message_id": "good"}) == 1
    assert db.emails.count_documents({"message_id": "bad"}) == 0
    assert summaries[0].processed == 1


def test_scheduler_stays_alive_when_gmail_source_is_not_configured(db, settings):
    # EMAIL_SOURCE=gmail with no real client wired -- the honest BLOCKED state.
    # run_once must log and continue, never propagate, never crash the loop.
    source = LiveGmailSource(db, client=None)
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()  # must not raise

    assert summaries == []


# --- Phase 7/8: source selection ------------------------------------------------------------


def test_build_source_selects_folder_source_by_default(db):
    settings = Settings()
    source = build_source(db, settings)
    from app.providers.source.folder import FolderSource

    assert isinstance(source, FolderSource)


def test_build_source_selects_live_gmail_source_when_configured(db):
    settings = Settings(email_source="gmail")
    source = build_source(db, settings)

    assert isinstance(source, LiveGmailSource)


def test_build_source_rejects_unknown_email_source(db):
    settings = Settings(email_source="carrier_pigeon")

    with pytest.raises(ValueError, match="Unknown EMAIL_SOURCE"):
        build_source(db, settings)
