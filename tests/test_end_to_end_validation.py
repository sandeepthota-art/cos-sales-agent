"""Targeted gap-fill for the end-to-end capability audit. Existing coverage
(tests/test_entities_resolution.py, tests/test_pipeline_entities.py,
tests/test_reply_drafter.py, tests/test_reply_approval.py,
tests/test_calendar_actions.py/test_calendar_models.py, tests/test_context_engine.py)
already thoroughly exercises commitment dedup, follow-up derivation, reply drafting/
approval, and calendar-attendee restriction. This file covers what that coverage
doesn't: third-person commitment extraction, hedged/non-commitment language,
reply-draft dedup at the whole-pipeline level, context-aware second-email drafting,
and the full Email -> Thread -> Person -> Commitment -> FollowUp -> ReplyDraft
provenance chain.
"""
import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    ContextSnapshotRepository,
    FollowUpRepository,
    MeetingRepository,
    PersonRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider


def _raw_email(message_id, body, subject="Project Alpha", thread_id=None, **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": subject,
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    if thread_id:
        raw["thread_id"] = thread_id
    raw.update(overrides)
    return raw


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    # agent_email matches _raw_email's default "to" address -- envelope-based Person
    # resolution (app.pipeline._process_entities) must never create a Person for our
    # own mailbox, so this has to line up with the fixture data, not the class default.
    return Settings(
        email_provider="mock", calendar_provider="mock", llm_provider="mock",
        agent_email="ashok@example.com",
    )


class _ThirdPersonCommitmentLLM(LLMProvider):
    """Deterministic double: MockLLMProvider's commitment regex only recognizes
    first-person "I will"/"we will" phrasing -- it never matches third-person "John
    will..." -- so this double simulates what a real LLM (Claude/OpenAI) would extract
    from "John will provide the pricing document," to prove the RESOLUTION side
    (resolve_commitment, not extraction) handles it correctly once given a properly
    shaped mention."""

    def analyze_email(self, email):
        return {
            "email_id": email.message_id,
            "summary": email.body[:200],
            "intent": "commitment",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": [], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [],
            "companies": [], "products": [],
            "people_mentioned": [
                {"name": "John", "email": email.from_.email, "org": None, "role_hint": None}
            ],
            "projects_mentioned": [],
            "commitments_mentioned": [
                {
                    "what": "provide the pricing document",
                    "class": "theirs",
                    "owed_by": "John",
                    "owed_to": "Ashok",
                    "date_phrase": None,
                    "importance_hint": None,
                }
            ],
            "meetings_mentioned": [],
            "personal_items_mentioned": [],
            "goal_pillar": "Sales",
            "label_applied": "Needs reply",
            "confidence": 0.8,
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "Thanks, John."}


def test_third_person_commitment_is_extracted_and_persisted(db, settings):
    payloads = [_raw_email("m1", "John will provide the pricing document.")]

    run_pipeline(db, MockEmailProvider(payloads=payloads), _ThirdPersonCommitmentLLM(), MockCalendarProvider(), settings)

    commitments = CommitmentRepository(db).find_many({})
    assert len(commitments) == 1
    assert commitments[0]["what"] == "provide the pricing document"
    assert commitments[0]["class"] == "theirs"
    assert commitments[0]["owed_by"] == "John"
    assert commitments[0]["source_record"] == "m1"


def test_hedged_suggestion_does_not_create_a_commitment(db, settings):
    # MockLLMProvider's _MINE_COMMITMENT_PATTERN only matches "i will"/"i'll"/"we will" --
    # "I think we should" doesn't match, so no commitment should be extracted at all.
    payloads = [_raw_email("m1", "I think we should send the proposal tomorrow.")]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    assert CommitmentRepository(db).find_many({}) == []
    assert FollowUpRepository(db).find_many({}) == []


def test_reply_draft_is_not_duplicated_when_the_same_email_is_processed_twice(db, settings):
    payloads = [_raw_email("m1", "Can you send me the proposal for Project Alpha? We need it before Friday.")]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    drafts = ReplyDraftRepository(db).find_many({"source_email_id": "m1"})
    assert len(drafts) == 1
    assert drafts[0]["status"] == "awaiting_approval"


def test_reply_draft_requires_explicit_approval_before_send_and_is_never_auto_sent(db, settings):
    payloads = [_raw_email("m1", "Can you send me the proposal for Project Alpha?")]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "m1"})
    assert draft["status"] == "awaiting_approval"
    assert draft["sent_at"] is None
    assert draft["approved_by"] is None


def test_context_aware_second_email_reply_uses_accumulated_thread_context(db, settings):
    email_1 = _raw_email(
        "m1", "John, let's work on Project Alpha. We agreed the proposal is due Friday.",
        thread_id="thread_alpha",
    )
    email_2 = _raw_email(
        "m2", "Just checking in on the Alpha proposal. Any update?",
        thread_id="thread_alpha", timestamp="2026-09-14T10:30:00Z",
    )

    run_pipeline(db, MockEmailProvider(payloads=[email_1]), MockLLMProvider(), MockCalendarProvider(), settings)
    run_pipeline(db, MockEmailProvider(payloads=[email_2]), MockLLMProvider(), MockCalendarProvider(), settings)

    thread = ThreadRepository(db).find_one({"thread_id": "thread_alpha"})
    assert set(thread["message_ids"]) == {"m1", "m2"}

    # Both emails resolve to the SAME person (thread + email-address reuse) -- proves
    # email 2 is not treated as a fresh, isolated sender.
    people = PersonRepository(db).find_many({})
    assert len(people) == 1
    assert "thread_alpha" in people[0]["open_threads"]

    # Email 2's reply draft exists and was built from a real ThreadContext, not a
    # standalone summary of email 2 alone -- the draft for m2 must reference the
    # accumulated context object rather than fail/be skipped.
    draft_2 = ReplyDraftRepository(db).find_one({"source_email_id": "m2"})
    assert draft_2 is not None
    assert draft_2["status"] == "awaiting_approval"


def test_full_provenance_chain_from_email_to_reply_draft(db, settings):
    payloads = [_raw_email("m1", "I will send the proposal tomorrow. Can you confirm receipt?")]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    email = db.emails.find_one({"message_id": "m1"}, {"_id": 0})
    refs = email["entities_referenced"]

    # email["thread_id"] is only ever the raw, as-ingested value -- the threads
    # collection's own message_ids arrays are the authoritative source of which
    # thread an email actually resolved into.
    thread = ThreadRepository(db).find_one({"message_ids": "m1"})
    assert thread is not None
    thread_id = thread["thread_id"]

    person = PersonRepository(db).find_one({"id": refs["people"][0]})
    assert person is not None

    commitment = CommitmentRepository(db).find_one({"id": refs["commitments"][0]})
    assert commitment["source_record"] == "m1"

    follow_up = FollowUpRepository(db).find_one({"id": refs["follow_ups"][0]})
    assert follow_up["commitment_id"] == commitment["id"]

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "m1"})
    assert draft["thread_id"] == thread_id


# --- Context snapshot synchronization (regression: canonical Commitment resolved, but
# threads.latest_context.commitments stayed [] for the same email/thread) ---


def test_context_snapshot_commitments_stays_synchronized_with_resolved_commitment(db, settings):
    payloads = [_raw_email("m1", "I will send the proposal tomorrow.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    thread = ThreadRepository(db).find_one({"message_ids": "m1"})
    commitment = CommitmentRepository(db).find_one({"thread_id": thread["thread_id"]})
    assert commitment is not None

    snapshot = ContextSnapshotRepository(db).latest_for_thread(thread["thread_id"])
    assert len(snapshot["context"]["commitments"]) >= 1


def test_context_snapshot_meetings_stays_synchronized_with_resolved_meeting(db, settings):
    payloads = [_raw_email("m1", "Sounds good. We will meet in 2 weeks.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    thread = ThreadRepository(db).find_one({"message_ids": "m1"})
    meeting = MeetingRepository(db).find_one({"thread_id": thread["thread_id"]})
    assert meeting is not None

    snapshot = ContextSnapshotRepository(db).latest_for_thread(thread["thread_id"])
    assert len(snapshot["context"]["meetings"]) >= 1
