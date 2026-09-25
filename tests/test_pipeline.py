from datetime import datetime, timezone

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import CalendarActionRepository, ReplyDraftRepository
from app.interfaces.email_provider import EmailProvider
from app.interfaces.llm_provider import LLMProvider
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider
from app.replies.models import ReplyDraft, ReplyDraftContent


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
        raise AssertionError("pipeline must never call send_email directly")


class _AlwaysBrokenLLM(LLMProvider):
    def analyze_email(self, email):
        return {"summary": "not enough fields"}

    def update_context(self, previous_context, new_analysis):
        raise AssertionError("should not be reached when analysis fails")

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise AssertionError

    def draft_reply(self, context, latest_email):
        raise AssertionError


class _RaisesOnUpdateContextForLLM(LLMProvider):
    """Wraps MockLLMProvider but raises an unexpected (non-validation) error
    from update_context for one specific email, simulating a transport/timeout
    failure from a real LLM provider partway through a batch."""

    def __init__(self, failing_email_id):
        self._base = MockLLMProvider()
        self._failing_email_id = failing_email_id

    def analyze_email(self, email):
        return self._base.analyze_email(email)

    def update_context(self, previous_context, new_analysis):
        if new_analysis.get("email_id") == self._failing_email_id:
            raise RuntimeError("simulated transport error")
        return self._base.update_context(previous_context, new_analysis)

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return self._base.verify_same_fact(existing_value, new_value, subject, predicate)

    def draft_reply(self, context, latest_email):
        return self._base.draft_reply(context, latest_email)


class _CompanyRevealingLLM(LLMProvider):
    """Wraps MockLLMProvider but injects a company name into the context
    starting from the second processed email onward, mimicking a thread
    where the company's identity only becomes known partway through."""

    def __init__(self):
        self._base = MockLLMProvider()
        self._call_count = 0

    def analyze_email(self, email):
        return self._base.analyze_email(email)

    def update_context(self, previous_context, new_analysis):
        # update_context returns a bounded ContextDelta now (see app.context.models) --
        # "company" itself isn't a delta field (it's a ThreadContext field), the patch
        # for it is "company_updates".
        delta = self._base.update_context(previous_context, new_analysis)
        self._call_count += 1
        if self._call_count >= 2:
            delta["company_updates"] = {"name": "Acme Corp"}
        return delta

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return self._base.verify_same_fact(existing_value, new_value, subject, predicate)

    def draft_reply(self, context, latest_email):
        return self._base.draft_reply(context, latest_email)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


def test_pipeline_processes_valid_emails_to_completion(db, settings):
    payloads = [
        _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point."),
        _raw_email("msg_002", "We'd need about 100 seats to start.", in_reply_to="msg_001", references=["msg_001"]),
    ]
    summary = run_pipeline(
        db=db,
        email_provider=_ListEmailProvider(payloads),
        llm_provider=MockLLMProvider(),
        calendar_provider=MockCalendarProvider(),
        settings=settings,
    )

    assert summary.processed == 2
    assert summary.completed == 2
    assert summary.failed == 0
    assert db.emails.count_documents({}) == 2
    assert db.threads.count_documents({}) == 1
    assert db.context_snapshots.count_documents({}) == 2
    assert db.knowledge_items.count_documents({}) >= 1

    stored_email = db.emails.find_one({"message_id": "msg_001"})
    assert stored_email["processing_status"]["stage"] == "COMPLETED"
    # The full email content must be persisted, not just the message_id
    # (Task 17's Email Explorer reads this collection expecting real content).
    assert stored_email["subject"] == "Enterprise CRM Proposal"
    assert stored_email["body"] == "We currently use Salesforce but pricing is a pain point."
    assert stored_email["from"]["email"] == "john@example.com"


def test_pipeline_is_idempotent_on_rerun(db, settings):
    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    email_provider = _ListEmailProvider(payloads)

    first = run_pipeline(db, email_provider, MockLLMProvider(), MockCalendarProvider(), settings)
    second = run_pipeline(db, email_provider, MockLLMProvider(), MockCalendarProvider(), settings)

    assert first.completed == 1
    assert second.skipped == 1
    assert second.completed == 0
    assert db.context_snapshots.count_documents({}) == 1
    assert db.emails.count_documents({}) == 1


def test_pipeline_continues_after_malformed_email_with_no_message_id(db, settings):
    payloads = [
        {"subject": "broken, no message_id at all"},
        _raw_email("msg_002", "We currently use Salesforce."),
    ]
    summary = run_pipeline(
        db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings
    )

    assert summary.completed == 1
    assert summary.failed == 1
    assert db.emails.count_documents({}) == 1  # malformed item never reached the emails collection
    run_doc = db.processing_runs.find_one({"run_id": summary.run_id})
    assert any(r["final_stage"] == "FAILED" and r["message_id"] is None for r in run_doc["results"])


def test_pipeline_marks_analysis_failure_without_completing(db, settings):
    payloads = [_raw_email("msg_001", "Some body text.")]
    summary = run_pipeline(
        db, _ListEmailProvider(payloads), _AlwaysBrokenLLM(), MockCalendarProvider(), settings
    )

    assert summary.failed == 1
    assert summary.completed == 0
    stored_email = db.emails.find_one({"message_id": "msg_001"})
    assert stored_email["processing_status"]["stage"] == "FAILED"
    assert stored_email["processing_status"]["failed_stage"] == "ANALYZED"
    assert db.context_snapshots.count_documents({}) == 0


def test_pipeline_continues_batch_and_records_failure_after_unexpected_exception(db, settings):
    payloads = [
        _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point."),
        _raw_email("msg_002", "We'd need about 100 seats to start."),
    ]
    summary = run_pipeline(
        db,
        _ListEmailProvider(payloads),
        _RaisesOnUpdateContextForLLM(failing_email_id="msg_001"),
        MockCalendarProvider(),
        settings,
    )

    # msg_001 blew up mid-processing (in update_context, during CONTEXT_BUILT)
    # but the batch must continue and msg_002 must still complete normally.
    assert summary.processed == 2
    assert summary.completed == 1
    assert summary.failed == 1

    failed_email = db.emails.find_one({"message_id": "msg_001"})
    assert failed_email["processing_status"]["stage"] == "FAILED"
    assert failed_email["processing_status"]["failed_stage"] == "CONTEXT_BUILT"
    assert "simulated transport error" in failed_email["processing_status"]["error"]

    completed_email = db.emails.find_one({"message_id": "msg_002"})
    assert completed_email["processing_status"]["stage"] == "COMPLETED"

    # The run summary must exist and record both outcomes, even though one
    # email crashed mid-pipeline (the summary is only written after the loop).
    run_doc = db.processing_runs.find_one({"run_id": summary.run_id})
    assert run_doc is not None
    result_by_id = {r["message_id"]: r["final_stage"] for r in run_doc["results"]}
    assert result_by_id == {"msg_001": "FAILED", "msg_002": "COMPLETED"}


def test_pipeline_does_not_overwrite_already_sent_reply_draft(db, settings):
    reply_repo = ReplyDraftRepository(db)
    reply_repo.upsert_by_key(
        {"source_email_id": "msg_001"},
        ReplyDraft(
            reply_id="reply_msg_001",
            thread_id="thread_msg_001",
            source_email_id="msg_001",
            status="simulated_sent",
            draft=ReplyDraftContent(subject="Re: Enterprise CRM Proposal", body="Already sent to the customer."),
            created_at=datetime.now(timezone.utc),
        ).model_dump(mode="json"),
    )

    payloads = [_raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.reply_drafts.find_one({"source_email_id": "msg_001"})
    assert stored["status"] == "simulated_sent"
    assert stored["draft"]["body"] == "Already sent to the customer."
    assert db.reply_drafts.count_documents({}) == 1


def test_pipeline_injects_recipient_preferences_into_the_generated_reply_draft(db, settings):
    # End-to-end: a Person's own preferences (app.entities.models.Person.preferences)
    # reach the actual generated draft body, via app.pipeline.run_pipeline's
    # sender_person lookup -> app.replies.drafter.draft_reply's recipient_preferences
    # param -> MockLLMProvider.draft_reply's voice_signature/remove_long_dash handling.
    from app.database.repositories import PersonRepository
    from app.entities.models import Person

    PersonRepository(db).upsert_by_key(
        {"id": "PER-EXISTING"},
        Person(
            id="PER-EXISTING", name="John", email="john@example.com",
            preferences={"voice_signature": "Thanks,\nJohn's Team", "remove_long_dash": True},
        ).model_dump(mode="json"),
    )

    payloads = [_raw_email("msg_001", "Can you send pricing? — thanks")]
    run_pipeline(db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft["draft"]["body"].endswith("Thanks,\nJohn's Team")
    assert "—" not in draft["draft"]["body"]


def test_pipeline_does_not_overwrite_already_scheduled_calendar_action(db, settings):
    payloads_first = [_raw_email("msg_001", "Let's schedule a call soon.")]
    run_pipeline(db, _ListEmailProvider(payloads_first), MockLLMProvider(), MockCalendarProvider(), settings)

    thread_id = "thread_msg_001"
    fingerprint = f"needs_clarification_{thread_id}"
    calendar_repo = CalendarActionRepository(db)
    action_doc = calendar_repo.find_one({"thread_id": thread_id, "meeting_fingerprint": fingerprint})
    assert action_doc is not None
    assert action_doc["status"] == "needs_clarification"

    # Simulate a human approving and scheduling the meeting after clarifying
    # out of band.
    calendar_repo.upsert_by_key(
        {"thread_id": thread_id, "meeting_fingerprint": fingerprint},
        {**action_doc, "status": "scheduled"},
    )

    # A second, unrelated ambiguous meeting email in the SAME thread maps to
    # the SAME thread-constant meeting_fingerprint (needs_clarification_<thread_id>).
    payloads_second = [
        _raw_email(
            "msg_002",
            "Following up, could we set up a call sometime this week?",
            in_reply_to="msg_001",
            references=["msg_001"],
        )
    ]
    run_pipeline(db, _ListEmailProvider(payloads_second), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = calendar_repo.find_one({"thread_id": thread_id, "meeting_fingerprint": fingerprint})
    assert stored["status"] == "scheduled"
    assert db.calendar_actions.count_documents({}) == 1


def test_pipeline_never_drafts_reply_for_email_from_agents_own_address(db, settings):
    # Regression: a two-sided thread (customer + our own sales rep both sending emails)
    # must never produce a reply draft addressed back to our own rep for our own outbound
    # email. settings.agent_email defaults to Task 14's demo sales rep address.
    agent_settings = settings.model_copy(update={"agent_email": "ashok@oursalesagent-demo.example"})
    payloads = [
        _raw_email(
            "msg_001",
            "Happy to walk you through the enterprise plan. Could you tell me what's not "
            "working well with your current process?",
            **{"from": {"name": "Ashok Kumar", "email": "Ashok@OurSalesAgent-Demo.example"}},
        ),
    ]
    run_pipeline(
        db, _ListEmailProvider(payloads), MockLLMProvider(), MockCalendarProvider(), agent_settings
    )

    stored_email = db.emails.find_one({"message_id": "msg_001"})
    assert stored_email["processing_status"]["stage"] == "COMPLETED"
    assert db.reply_drafts.count_documents({}) == 0


def test_pipeline_keeps_knowledge_subject_key_stable_as_company_name_becomes_known(db, settings):
    payloads = [
        _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point."),
        _raw_email(
            "msg_002",
            "Our pricing concerns are still a pain point.",
            in_reply_to="msg_001",
            references=["msg_001"],
        ),
    ]
    run_pipeline(db, _ListEmailProvider(payloads), _CompanyRevealingLLM(), MockCalendarProvider(), settings)

    # Both emails raise the same pain point. If the subject_key used for
    # knowledge fact identity fragmented once the company name became known
    # after msg_002, this would be 2 separate items instead of 1 merged one.
    pain_point_items = list(db.knowledge_items.find({"predicate": "has_pain_point"}))
    assert len(pain_point_items) == 1
    assert set(pain_point_items[0]["source_emails"]) == {"msg_001", "msg_002"}
