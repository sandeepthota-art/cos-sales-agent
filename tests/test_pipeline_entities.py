from datetime import datetime, timedelta

import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    FollowUpRepository,
    MeetingRepository,
    PersonRepository,
)
from app.interfaces.llm_provider import LLMProvider
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
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


def test_pipeline_populates_entity_metadata_on_the_email(db, settings):
    payloads = [_raw_email("1a08090646ebaa45", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "1a08090646ebaa45"}, {"_id": 0})
    assert stored["record_id"] == "1a08090646ebaa45"
    assert stored["source_type"] == "gmail"
    assert stored["source_link"] == "https://mail.google.com/mail/u/0/#all/1a08090646ebaa45"
    assert stored["date"] == "2026-09-13"
    assert stored["goal_pillar"] == "Sales"
    assert stored["label_applied"] in {"Needs reply: ASAP", "Read only"}
    assert stored["priority"] in {"P1", "P2"}
    assert isinstance(stored["confidence"], float)

    entities_referenced = stored["entities_referenced"]
    assert len(entities_referenced["people"]) == 1
    assert len(entities_referenced["commitments"]) == 1
    assert len(entities_referenced["follow_ups"]) == 1
    assert entities_referenced["projects"] == []
    assert entities_referenced["meetings"] == []
    assert entities_referenced["personal"] == []

    person = PersonRepository(db).find_one({"id": entities_referenced["people"][0]})
    assert person["email"] == "john@example.com"

    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    assert commitment["class"] == "mine"

    follow_up = FollowUpRepository(db).find_one({"id": entities_referenced["follow_ups"][0]})
    assert follow_up["commitment_id"] == entities_referenced["commitments"][0]


def test_follow_up_derived_from_commitment_carries_the_commitment_thread_id(db, settings):
    # Regression: a FollowUp derived from a Commitment previously always stored
    # thread_id=None, even though the Commitment it links to (and the email it came
    # from) had a real, known thread_id -- see app.entities.resolution.derive_follow_up.
    from app.database.repositories import ThreadRepository

    payloads = [_raw_email("m1", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m1"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]

    thread = ThreadRepository(db).find_one({"message_ids": "m1"})
    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    follow_up = FollowUpRepository(db).find_one({"id": entities_referenced["follow_ups"][0]})

    assert follow_up["commitment_id"] == commitment["id"]
    assert follow_up["thread_id"] == thread["thread_id"]
    assert commitment["thread_id"] == thread["thread_id"]


def test_pipeline_sets_source_link_to_none_for_non_gmail_shaped_message_id(db, settings):
    payloads = [_raw_email("not-a-gmail-hex-id", "Just checking in.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "not-a-gmail-hex-id"}, {"_id": 0})
    assert stored["source_link"] is None


def test_pipeline_reuses_same_person_across_two_emails_in_same_thread(db, settings):
    payloads = [
        _raw_email("1111111111111111", "I will send the proposal on Friday."),
        _raw_email(
            "2222222222222222",
            "Following up on my earlier note.",
            in_reply_to="1111111111111111",
            references=["1111111111111111"],
        ),
    ]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    assert PersonRepository(db).find_many({}).__len__() == 1


def test_pipeline_detects_relative_date_meeting_with_no_commitment_and_no_follow_up(db, settings):
    from app.database.repositories import FollowUpRepository, MeetingRepository

    payloads = [_raw_email("1a08090646ebaa45", "Sounds good. We will meet in 2 weeks.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "1a08090646ebaa45"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]
    assert entities_referenced["commitments"] == []
    assert len(entities_referenced["meetings"]) == 1

    meeting = MeetingRepository(db).find_one({"id": entities_referenced["meetings"][0]})
    assert meeting["actionable"] is True
    # 2026-09-13 (this email's timestamp) + 14 days = 2026-09-27
    assert meeting["date"].startswith("2026-09-27")

    # The core corrected rule: a Meeting must never trigger a FollowUp by itself.
    assert entities_referenced["follow_ups"] == []
    assert FollowUpRepository(db).find_many({}) == []


# --- Envelope-based Person resolution (app.pipeline._process_entities) --------------
# Every real From/To/CC address on the email now gets resolved through resolve_person,
# independent of whether the LLM's own people_mentioned happened to include it. The
# agent's own mailbox (settings.agent_email) is always excluded. These tests use a
# double that returns an EMPTY people_mentioned so the sender is genuinely "unsigned"
# by the LLM -- isolating the envelope path from the pre-existing LLM-mention path.


class _NoPeopleLLM(LLMProvider):
    """Never mentions anyone in people_mentioned, regardless of the email -- proves
    envelope-based resolution works entirely independently of what the LLM chooses to
    report."""

    def analyze_email(self, email):
        return {
            "email_id": email.message_id, "summary": email.body[:200], "intent": "evaluation",
            "entities": [], "facts": [], "requirements": [], "pain_points": [],
            "buying_signals": [], "objections": [], "competitors": [], "pricing_mentions": [],
            "commitments": [], "action_items": [], "meetings": [], "people": [],
            "companies": [], "products": [],
            "people_mentioned": [],
            "projects_mentioned": [], "commitments_mentioned": [], "meetings_mentioned": [],
            "personal_items_mentioned": [], "goal_pillar": "Sales", "label_applied": "Read only",
            "confidence": 0.8,
        }

    def update_context(self, previous_context, new_analysis):
        return {}

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        return False

    def draft_reply(self, context, latest_email):
        return {"subject": f"Re: {latest_email.subject}", "body": "noop"}


class _MentionsSenderLLM(_NoPeopleLLM):
    """Also mentions the sender explicitly in people_mentioned -- the same address
    envelope resolution independently resolves -- to prove the two paths converge on
    one Person, never two."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["people_mentioned"] = [
            {"name": "John Explicit", "email": email.from_.email, "org": None, "role_hint": None}
        ]
        return result


def test_envelope_resolves_sender_even_when_llm_never_mentions_them_reuses_existing_person(db, settings):
    from datetime import datetime, timezone

    from app.entities.resolution import resolve_person

    existing_id = resolve_person(
        db, {"name": "John", "email": "john@example.com"}, is_sender=True,
        now=datetime(2026, 9, 1, tzinfo=timezone.utc), thread_id="thread_prior",
    )

    payloads = [_raw_email("msg_001", "Just checking in.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _NoPeopleLLM(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "msg_001"}, {"_id": 0})
    assert stored["entities_referenced"]["people"] == [existing_id]
    assert PersonRepository(db).find_many({}).__len__() == 1  # reused, not duplicated


def test_envelope_resolves_sender_creating_a_new_person_when_llm_never_mentions_them(db, settings):
    payloads = [_raw_email("msg_001", "Just checking in.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _NoPeopleLLM(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "msg_001"}, {"_id": 0})
    people = stored["entities_referenced"]["people"]
    assert len(people) == 1
    person = PersonRepository(db).find_one({"id": people[0]})
    assert person["email"] == "john@example.com"


def test_envelope_resolution_never_creates_a_person_for_the_agent_email(db, settings):
    # settings.agent_email == "ashok@example.com", the default "to" address in
    # _raw_email -- envelope resolution must skip it entirely.
    payloads = [_raw_email("msg_001", "Just checking in.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _NoPeopleLLM(), MockCalendarProvider(), settings)

    assert PersonRepository(db).find_one({"email": "ashok@example.com"}) is None


def test_envelope_and_llm_mention_of_the_same_address_do_not_create_a_duplicate_person(db, settings):
    payloads = [_raw_email("msg_001", "Just checking in.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _MentionsSenderLLM(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "msg_001"}, {"_id": 0})
    people = stored["entities_referenced"]["people"]
    assert len(people) == 1  # deduped, not one per resolution path
    assert PersonRepository(db).find_many({"email": "john@example.com"}).__len__() == 1


def test_envelope_resolution_moves_last_inbound_forward_across_separate_emails(db, settings):
    earlier = [_raw_email("msg_001", "First.", timestamp="2026-09-13T10:00:00Z")]
    run_pipeline(db, MockEmailProvider(payloads=earlier), _NoPeopleLLM(), MockCalendarProvider(), settings)

    later = [_raw_email("msg_002", "Second.", timestamp="2026-09-14T10:00:00Z")]
    run_pipeline(db, MockEmailProvider(payloads=later), _NoPeopleLLM(), MockCalendarProvider(), settings)

    person = PersonRepository(db).find_one({"email": "john@example.com"})
    assert person["last_inbound"] == "2026-09-14T10:00:00Z"


def test_pipeline_does_not_create_a_meeting_for_a_mere_reference_mtg_384_regression(db, settings):
    # Regression: MTG-384 was wrongly created from an email that only REFERENCED an
    # already-established meeting ("our sprint review on Friday"), not one requesting/
    # proposing/scheduling/confirming a new one. End-to-end through run_pipeline (not
    # just MockLLMProvider directly), proving no Meeting entity reaches MongoDB.
    from app.database.repositories import MeetingRepository

    payloads = [_raw_email("1a0c5642a4225edd", "Following our meeting, here's the recap you asked for.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "1a0c5642a4225edd"}, {"_id": 0})
    assert stored["entities_referenced"]["meetings"] == []
    assert MeetingRepository(db).find_many({}) == []


def test_pipeline_does_not_mark_historical_meeting_mention_as_actionable(db, settings):
    payloads = [_raw_email("1a08090646ebaa45", "We met last week and it went well.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "1a08090646ebaa45"}, {"_id": 0})
    # MockLLMProvider never recognizes "met" (past tense) as a meeting mention at all --
    # see test_mock_llm_does_not_detect_historical_meeting_mention_as_a_meeting (Task 5).
    assert stored["entities_referenced"]["meetings"] == []


def test_pipeline_marks_failed_at_entities_processed_without_losing_prior_stage_data(
    db, settings, monkeypatch
):
    # A shape-invalid EmailAnalysis field would fail pydantic validation during the
    # existing ANALYZED stage (inside analyze_email_with_validation), not the new
    # ENTITIES_PROCESSED stage -- that would test the wrong stage entirely. To reach a
    # failure specifically inside the new stage, the EmailAnalysis must validate
    # successfully; the failure must come from entity resolution itself. Monkeypatching
    # the resolver app/pipeline.py actually calls is the precise way to do that.
    import app.pipeline as pipeline_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated entity resolution failure")

    monkeypatch.setattr(pipeline_module, "resolve_person", _boom)

    payloads = [_raw_email("1a08090646ebaa45", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "1a08090646ebaa45"}, {"_id": 0})
    assert stored["processing_status"]["stage"] == "FAILED"
    assert stored["processing_status"]["failed_stage"] == "ENTITIES_PROCESSED"
    assert "simulated entity resolution failure" in stored["processing_status"]["error"]
    # Knowledge/context from earlier stages must still be persisted, not rolled back.
    assert db.context_snapshots.count_documents({}) == 1
    assert db.knowledge_items.count_documents({}) >= 1


# --- BRD 6.3: only "mine"/"owed_to_me" commitments are chased -------------------------
# MockLLMProvider's own regex triggers only ever produce "mine" ("I will"/"I'll") or
# "owed_to_me" ("could you"/"can you") -- there is no email-body phrasing that makes it
# emit "theirs" or "recap", so those two classes need a small fake provider that reports
# exactly one commitment of the requested class. Everything else about the analysis is
# the same empty/neutral shape as _NoPeopleLLM above.


class _SingleCommitmentLLM(_NoPeopleLLM):
    """Reports exactly one commitment of `commitment_class`, nothing else -- isolates
    the commitment-class -> follow-up decision from every other extraction path."""

    def __init__(self, commitment_class):
        self._commitment_class = commitment_class

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["commitments_mentioned"] = [
            {
                "what": "some action",
                "class": self._commitment_class,
                "owed_by": None,
                "owed_to": None,
                "date_phrase": None,
                "importance_hint": None,
            }
        ]
        return result


def test_pipeline_mine_commitment_creates_a_follow_up(db, settings):
    payloads = [_raw_email("m_mine", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_mine"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]
    assert len(entities_referenced["commitments"]) == 1
    assert len(entities_referenced["follow_ups"]) == 1

    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    assert commitment["class"] == "mine"
    follow_up = FollowUpRepository(db).find_one({"id": entities_referenced["follow_ups"][0]})
    assert follow_up["commitment_id"] == commitment["id"]


def test_pipeline_owed_to_me_commitment_creates_a_follow_up(db, settings):
    payloads = [_raw_email("m_owed", "Could you send over pricing by Friday?")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_owed"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]
    assert len(entities_referenced["commitments"]) == 1
    assert len(entities_referenced["follow_ups"]) == 1

    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    assert commitment["class"] == "owed_to_me"
    follow_up = FollowUpRepository(db).find_one({"id": entities_referenced["follow_ups"][0]})
    assert follow_up["commitment_id"] == commitment["id"]


def test_pipeline_theirs_commitment_creates_commitment_but_no_follow_up(db, settings):
    payloads = [_raw_email("m_theirs", "Just looping you in on their agreement.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _SingleCommitmentLLM("theirs"), MockCalendarProvider(), settings
    )

    stored = db.emails.find_one({"message_id": "m_theirs"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]
    assert len(entities_referenced["commitments"]) == 1
    assert entities_referenced["follow_ups"] == []

    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    assert commitment["class"] == "theirs"
    assert FollowUpRepository(db).find_many({}) == []


def test_pipeline_recap_commitment_creates_commitment_but_no_follow_up(db, settings):
    payloads = [_raw_email("m_recap", "Just restating what we already agreed on.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _SingleCommitmentLLM("recap"), MockCalendarProvider(), settings
    )

    stored = db.emails.find_one({"message_id": "m_recap"}, {"_id": 0})
    entities_referenced = stored["entities_referenced"]
    assert len(entities_referenced["commitments"]) == 1
    assert entities_referenced["follow_ups"] == []

    commitment = CommitmentRepository(db).find_one({"id": entities_referenced["commitments"][0]})
    assert commitment["class"] == "recap"
    assert FollowUpRepository(db).find_many({}) == []


def test_pipeline_theirs_commitment_stays_duplicate_safe_on_rerun(db, settings):
    # Re-running the same email a second time must not create a second Commitment (the
    # pre-existing message_id/COMPLETED idempotency guard) and must still create zero
    # FollowUps, exactly like the first run.
    payloads = [_raw_email("m_theirs_rerun", "Just looping you in on their agreement.")]
    email_provider = MockEmailProvider(payloads=payloads)
    llm = _SingleCommitmentLLM("theirs")

    first = run_pipeline(db, email_provider, llm, MockCalendarProvider(), settings)
    second = run_pipeline(db, email_provider, llm, MockCalendarProvider(), settings)

    assert first.completed == 1
    assert second.skipped == 1
    assert CommitmentRepository(db).find_many({}).__len__() == 1
    assert FollowUpRepository(db).find_many({}) == []


# --- BRD Commitment + Follow-Up system: ownership, project linkage, dedup, dates ------


def test_pipeline_does_not_create_a_commitment_from_purely_informational_text(db, settings):
    # No "I will"/"I'll"/"could you"/"can you" trigger anywhere -- MockLLMProvider must
    # report zero commitments_mentioned, and the pipeline must create nothing.
    payloads = [_raw_email("m_info", "Here is the deck we discussed. No action needed.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_info"}, {"_id": 0})
    assert stored["entities_referenced"]["commitments"] == []
    assert CommitmentRepository(db).find_many({}) == []
    assert FollowUpRepository(db).find_many({}) == []


class _ThirdPersonCommitmentLLM(_NoPeopleLLM):
    """Simulates what a real LLM extracts from third-person phrasing ("Priya will send
    the proposal Friday") -- MockLLMProvider's own regex only ever recognizes
    first-person "I will"/"I'll", so this double is needed to prove ownership
    (commitment_class="theirs", person_id resolved to the named third party, never the
    sender/user) independent of extraction-pattern limitations. Deliberately a
    different name than _raw_email's default sender ("John") so the two never collide
    into an ambiguous same-name match (see match_resolved_person_by_name)."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["people_mentioned"] = [
            {"name": "Priya", "email": "priya@example.com", "org": None, "role_hint": None}
        ]
        result["commitments_mentioned"] = [
            {
                "what": "send the proposal",
                "class": "theirs",
                "owed_by": "Priya",
                "owed_to": None,
                "date_phrase": "Friday",
                "importance_hint": None,
            }
        ]
        return result


def test_pipeline_third_person_commitment_is_owned_by_the_named_person_not_the_sender(db, settings):
    # "Priya will send the proposal Friday" -- Priya is a third party, not the email's
    # actual sender (John, per _raw_email's default), so the resolved Commitment's
    # person_id must point at the explicitly-mentioned Priya, never the sender, and
    # its class must stay "theirs" (never "mine").
    payloads = [_raw_email("m_third_person", "Quick update: Priya will send the proposal Friday.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _ThirdPersonCommitmentLLM(), MockCalendarProvider(), settings
    )

    stored = db.emails.find_one({"message_id": "m_third_person"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    assert commitment["class"] == "theirs"

    priya = PersonRepository(db).find_one({"email": "priya@example.com"})
    assert commitment["person_id"] == priya["id"]
    # theirs is never chased.
    assert stored["entities_referenced"]["follow_ups"] == []


class _TwoDistinctCommitmentsLLM(_NoPeopleLLM):
    """Two commitments with different `what` text in one email -- must resolve to two
    distinct Commitment records, never merged into one."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["commitments_mentioned"] = [
            {
                "what": "send the proposal", "class": "mine", "owed_by": None, "owed_to": None,
                "date_phrase": "tomorrow", "importance_hint": None,
            },
            {
                "what": "send the signed NDA", "class": "mine", "owed_by": None, "owed_to": None,
                "date_phrase": "Friday", "importance_hint": None,
            },
        ]
        return result


def test_pipeline_creates_two_distinct_commitments_from_one_email(db, settings):
    payloads = [_raw_email("m_two", "I will send the proposal tomorrow and the signed NDA by Friday.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _TwoDistinctCommitmentsLLM(), MockCalendarProvider(), settings
    )

    stored = db.emails.find_one({"message_id": "m_two"}, {"_id": 0})
    commitment_ids = stored["entities_referenced"]["commitments"]
    assert len(commitment_ids) == 2
    assert len(set(commitment_ids)) == 2  # genuinely distinct, not the same id twice

    whats = {CommitmentRepository(db).find_one({"id": cid})["what"] for cid in commitment_ids}
    assert whats == {"send the proposal", "send the signed NDA"}
    # Each distinct commitment gets its own FollowUp -- never merged either.
    assert len(stored["entities_referenced"]["follow_ups"]) == 2


def test_pipeline_merges_the_same_commitment_restated_in_a_later_reply_same_thread(db, settings):
    # Same what/class/date_phrase repeated in a second email of the same thread must
    # reuse the existing Commitment and FollowUp, never create a second pair -- the
    # thread-scoped dedup already in app.entities.resolution.resolve_commitment.
    first_payload = [_raw_email("m_restate_1", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=first_payload), MockLLMProvider(), MockCalendarProvider(), settings)
    first_stored = db.emails.find_one({"message_id": "m_restate_1"}, {"_id": 0})
    first_thread_id = first_stored["thread_id"]

    second_payload = [
        _raw_email(
            "m_restate_2", "Just confirming: I will send the proposal on Friday.",
            thread_id=first_thread_id, in_reply_to="m_restate_1",
        )
    ]
    run_pipeline(db, MockEmailProvider(payloads=second_payload), MockLLMProvider(), MockCalendarProvider(), settings)
    second_stored = db.emails.find_one({"message_id": "m_restate_2"}, {"_id": 0})

    assert first_stored["entities_referenced"]["commitments"] == second_stored["entities_referenced"]["commitments"]
    assert first_stored["entities_referenced"]["follow_ups"] == second_stored["entities_referenced"]["follow_ups"]
    assert CommitmentRepository(db).find_many({}).__len__() == 1
    assert FollowUpRepository(db).find_many({}).__len__() == 1


class _CommitmentWithProjectEvidenceLLM(_NoPeopleLLM):
    """One email that mentions both a project and a commitment whose counterparty's
    org matches that project's org -- the only evidence app.pipeline is allowed to use
    to link Commitment.project_id (never a fresh/guessed scan)."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["people_mentioned"] = [
            {"name": "Jane", "email": "jane@acme.com", "org": "Acme", "role_hint": None}
        ]
        result["projects_mentioned"] = [{"name": "Acme Renewal", "org": "Acme", "objective_hint": None}]
        result["commitments_mentioned"] = [
            {
                "what": "send the signed SOW", "class": "owed_to_me", "owed_by": "Jane", "owed_to": None,
                "date_phrase": "Friday", "importance_hint": None,
            }
        ]
        return result


def test_pipeline_links_commitment_to_project_when_counterparty_org_matches(db, settings):
    payloads = [_raw_email("m_project_link", "Jane will send the signed SOW for the Acme renewal by Friday.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _CommitmentWithProjectEvidenceLLM(), MockCalendarProvider(), settings
    )

    stored = db.emails.find_one({"message_id": "m_project_link"}, {"_id": 0})
    project_id = stored["entities_referenced"]["projects"][0]
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})

    assert commitment["project_id"] == project_id

    # Query/MCP visibility: the existing project-scoped query now actually returns it.
    from app.query.commitments import get_commitments_for_project

    results = get_commitments_for_project(db, project_id)
    assert [r["id"] for r in results] == [commitment["id"]]


def test_pipeline_commitment_project_id_stays_unset_without_matching_project_evidence(db, settings):
    # Regression guard: a "mine" commitment with no projects_mentioned in the same
    # email must not have a project_id invented for it.
    payloads = [_raw_email("m_no_project", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_no_project"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    assert commitment["project_id"] is None


class _CommitmentWithAmbiguousProjectEvidenceLLM(_NoPeopleLLM):
    """Two DIFFERENT projects mentioned for the SAME organization in one email, plus a
    commitment whose only linking evidence (the counterparty's org) matches both --
    genuine ambiguity: the current extraction schema (RawCommitment) carries no
    project-specific reference of its own, so nothing distinguishes which of the two
    the commitment actually belongs to."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["people_mentioned"] = [
            {"name": "Jane", "email": "jane@acme.com", "org": "Acme", "role_hint": None}
        ]
        result["projects_mentioned"] = [
            {"name": "Acme Renewal", "org": "Acme", "objective_hint": None},
            {"name": "Acme Onboarding", "org": "Acme", "objective_hint": None},
        ]
        result["commitments_mentioned"] = [
            {
                "what": "send the signed SOW", "class": "owed_to_me", "owed_by": "Jane", "owed_to": None,
                "date_phrase": "Friday", "importance_hint": None,
            }
        ]
        return result


def test_pipeline_commitment_project_id_stays_unset_when_org_has_multiple_projects(db, settings):
    # The bug this fixes: project_id_by_org_id used to be a single value per org_id, so
    # the SECOND project mentioned would silently overwrite the first and get linked to
    # the commitment regardless of which one it actually concerned. Now: more than one
    # candidate for the same org is genuine ambiguity, never guessed.
    payloads = [_raw_email("m_ambiguous_project", "Jane will send the signed SOW for Acme by Friday.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _CommitmentWithAmbiguousProjectEvidenceLLM(),
        MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_ambiguous_project"}, {"_id": 0})
    assert len(stored["entities_referenced"]["projects"]) == 2  # both projects still created
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    assert commitment["project_id"] is None


def test_pipeline_ambiguous_project_commitment_stays_idempotent_on_rerun(db, settings):
    payloads = [_raw_email("m_ambiguous_rerun", "Jane will send the signed SOW for Acme by Friday.")]
    email_provider = MockEmailProvider(payloads=payloads)
    llm = _CommitmentWithAmbiguousProjectEvidenceLLM()

    first = run_pipeline(db, email_provider, llm, MockCalendarProvider(), settings)
    second = run_pipeline(db, email_provider, llm, MockCalendarProvider(), settings)

    assert first.completed == 1
    assert second.skipped == 1
    assert CommitmentRepository(db).find_many({}).__len__() == 1
    commitment = CommitmentRepository(db).find_one({})
    assert commitment["project_id"] is None


def test_pipeline_resolves_an_explicit_date_commitment_as_stated(db, settings):
    payloads = [_raw_email("m_stated", "I will send the contract by June 5th.", timestamp="2026-01-01T10:00:00Z")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_stated"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    assert commitment["date_type"] == "stated"
    assert commitment["committed_date"].startswith("2026-06-05")


class _UnresolvedDatePhraseLLM(_NoPeopleLLM):
    """A commitment whose date phrase carries no recognizable date signal at all --
    the phrase itself is still preserved as evidence, but no date may be invented."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["commitments_mentioned"] = [
            {
                "what": "circle back", "class": "mine", "owed_by": None, "owed_to": None,
                "date_phrase": "once things settle down", "importance_hint": None,
            }
        ]
        return result


def test_pipeline_leaves_commitment_date_unresolved_for_an_unrecognized_phrase(db, settings):
    payloads = [_raw_email("m_unresolved", "I'll circle back once things settle down.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _UnresolvedDatePhraseLLM(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_unresolved"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    assert commitment["committed_date"] is None
    assert commitment["date_type"] == "window"
    # The original phrase must remain visible somewhere as evidence -- it does, on the
    # raw email body itself (source_record points back to this exact message).
    assert commitment["source_record"] == "m_unresolved"


def test_pipeline_new_follow_up_starts_at_escalation_level_one_unsurfaced_active(db, settings):
    payloads = [_raw_email("m_escalation_default", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_escalation_default"}, {"_id": 0})
    follow_up = FollowUpRepository(db).find_one({"id": stored["entities_referenced"]["follow_ups"][0]})
    assert follow_up["escalation_level"] == 1
    assert follow_up["surfaced"] is False
    assert follow_up["status"] == "active"


def test_pipeline_commitment_and_follow_up_use_the_brd_canonical_id_prefixes(db, settings):
    payloads = [_raw_email("m_prefixes", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_prefixes"}, {"_id": 0})
    assert stored["entities_referenced"]["commitments"][0].startswith("CMT-")
    assert stored["entities_referenced"]["follow_ups"][0].startswith("FUP-")


# --- BRD 6.4: audience classification + timing on chased ("owed_to_me") follow-ups ----
# _raw_email's default sender is john@example.com and settings.agent_email (the
# `settings` fixture) is ashok@example.com -- SAME domain, so the default fixture data
# is already an "internal" scenario. Tests needing an external/client counterparty use
# a different domain (acme.com) explicitly.


class _OwedToMeFromLLM(_NoPeopleLLM):
    """A single owed_to_me commitment from a named counterparty with a given email
    (and therefore org/domain) and date phrase."""

    def __init__(self, counterparty_email, date_phrase):
        self._counterparty_email = counterparty_email
        self._date_phrase = date_phrase

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["people_mentioned"] = [
            {"name": "Counterparty", "email": self._counterparty_email, "org": None, "role_hint": None}
        ]
        result["commitments_mentioned"] = [
            {
                "what": "send the signed contract", "class": "owed_to_me",
                "owed_by": "Counterparty", "owed_to": None,
                "date_phrase": self._date_phrase, "importance_hint": None,
            }
        ]
        return result


def test_pipeline_internal_owed_to_me_commitment_gets_internal_audience_and_timing(db, settings):
    # Counterparty shares the agent's own domain (example.com) -- BRD 6.4: "Internal --
    # Chase one to two days after the stated deadline."
    payloads = [_raw_email("m_internal", "Colleague will send the signed contract tomorrow.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads),
        _OwedToMeFromLLM("colleague@example.com", "tomorrow"), MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_internal"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    follow_up = FollowUpRepository(db).find_one({"id": stored["entities_referenced"]["follow_ups"][0]})

    assert follow_up["audience"] == "internal"
    committed = datetime.fromisoformat(commitment["committed_date"].replace("Z", "+00:00"))
    earliest = datetime.fromisoformat(follow_up["follow_up_earliest_at"].replace("Z", "+00:00"))
    latest = datetime.fromisoformat(follow_up["follow_up_latest_at"].replace("Z", "+00:00"))
    assert earliest == committed + timedelta(days=1)
    assert latest == committed + timedelta(days=2)


def test_pipeline_external_owed_to_me_commitment_with_fixed_date_gets_client_fixed_date_timing(db, settings):
    # Counterparty's domain (acme.com) differs from the agent's own (example.com) --
    # BRD 6.4: "Client, fixed date -- Wait two to three business days past the date."
    payloads = [_raw_email("m_client_fixed", "Acme contact will send the signed contract by June 5th.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads),
        _OwedToMeFromLLM("buyer@acme.com", "June 5th"), MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_client_fixed"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    follow_up = FollowUpRepository(db).find_one({"id": stored["entities_referenced"]["follow_ups"][0]})

    assert commitment["date_type"] == "stated"
    assert follow_up["audience"] == "client_fixed_date"

    from app.entities.dates import _add_business_days

    committed = datetime.fromisoformat(commitment["committed_date"].replace("Z", "+00:00"))
    earliest = datetime.fromisoformat(follow_up["follow_up_earliest_at"].replace("Z", "+00:00"))
    latest = datetime.fromisoformat(follow_up["follow_up_latest_at"].replace("Z", "+00:00"))
    assert earliest == _add_business_days(committed, 2)
    assert latest == _add_business_days(committed, 3)


def test_pipeline_external_owed_to_me_commitment_with_no_date_gets_client_open_window_and_no_invented_timing(db, settings):
    # BRD 6.4: "Client, open window -- Nudge at the edge of the window, asking for a
    # time." No fixed anchor exists (the date phrase is unrecognized), so no timestamp
    # may be invented.
    payloads = [_raw_email("m_client_window", "Acme contact will reconnect once things settle down.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads),
        _OwedToMeFromLLM("buyer@acme.com", "once things settle down"), MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_client_window"}, {"_id": 0})
    commitment = CommitmentRepository(db).find_one({"id": stored["entities_referenced"]["commitments"][0]})
    follow_up = FollowUpRepository(db).find_one({"id": stored["entities_referenced"]["follow_ups"][0]})

    assert commitment["date_type"] == "window"
    assert commitment["committed_date"] is None
    assert follow_up["audience"] == "client_open_window"
    assert follow_up["follow_up_earliest_at"] is None
    assert follow_up["follow_up_latest_at"] is None


def test_pipeline_mine_commitment_never_gets_audience_or_timing(db, settings):
    # BRD 6.4's audience/timing rules describe chasing SOMEONE ELSE -- a "mine"
    # commitment (his own task) gets a FollowUp (existing chase-restriction behavior,
    # unchanged by this task) but no audience classification.
    payloads = [_raw_email("m_mine_no_audience", "I will send the proposal on Friday.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_mine_no_audience"}, {"_id": 0})
    follow_up = FollowUpRepository(db).find_one({"id": stored["entities_referenced"]["follow_ups"][0]})

    assert follow_up["audience"] is None
    assert follow_up["follow_up_earliest_at"] is None
    assert follow_up["follow_up_latest_at"] is None


# --- CalendarAction.meeting_id: link only when unambiguous ----------------------------
# _raw_email's default timestamp (2026-09-13T10:30:00Z, a Sunday) combined with
# settings.timezone's default (Asia/Kolkata, no day-boundary crossing at that time)
# means resolve_date_phrase (used for meetings_mentioned) and detect_meeting's own
# regex-based weekday resolution agree on which calendar date "Friday" refers to.


class _SingleMeetingLLM(_NoPeopleLLM):
    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["meetings_mentioned"] = [
            {"date_phrase": "Friday", "attendees": [], "is_past": False, "actions_raised": []}
        ]
        return result


def test_pipeline_links_calendar_action_to_the_single_meeting(db, settings):
    payloads = [_raw_email("m_single_meeting", "Let's meet Friday at 3 PM for 30 minutes.")]
    run_pipeline(db, MockEmailProvider(payloads=payloads), _SingleMeetingLLM(), MockCalendarProvider(), settings)

    stored = db.emails.find_one({"message_id": "m_single_meeting"}, {"_id": 0})
    assert len(stored["entities_referenced"]["meetings"]) == 1
    meeting_id = stored["entities_referenced"]["meetings"][0]

    action = CalendarActionRepository(db).find_one({})  # exactly one calendar action created in this test
    assert action["meeting_id"] == meeting_id
    assert action["event"]["attendees"] == []


class _TwoMeetingsOneMatchingDetectedDateLLM(_NoPeopleLLM):
    """Two meetings mentioned with different dates -- Friday (matching the real,
    regex-detected calendar action) and next Monday (which doesn't)."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["meetings_mentioned"] = [
            {"date_phrase": "Friday", "attendees": [], "is_past": False, "actions_raised": []},
            {"date_phrase": "next Monday", "attendees": [], "is_past": False, "actions_raised": []},
        ]
        return result


def test_pipeline_links_calendar_action_to_the_matching_meeting_among_several(db, settings):
    # The bug this fixes: meeting_id used to be "whichever meeting id happened to be
    # first in entities_referenced['meetings']", regardless of whether it was the one
    # the calendar action actually concerns. Now: the calendar action's own detected
    # date (Friday) is matched against each candidate meeting's own resolved date, and
    # only the genuinely matching one is linked.
    payloads = [_raw_email("m_two_meetings_match", "Let's meet Friday at 3 PM for 30 minutes.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _TwoMeetingsOneMatchingDetectedDateLLM(),
        MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_two_meetings_match"}, {"_id": 0})
    assert len(stored["entities_referenced"]["meetings"]) == 2
    meetings = MeetingRepository(db).find_many({"id": {"$in": stored["entities_referenced"]["meetings"]}})
    friday_meeting = next(m for m in meetings if datetime.fromisoformat(m["date"]).date().isoformat() == "2026-09-18")

    action = CalendarActionRepository(db).find_one({})  # exactly one calendar action created in this test
    assert action["meeting_id"] == friday_meeting["id"]


class _TwoMeetingsAmbiguousLLM(_NoPeopleLLM):
    """Two meetings mentioned, but the email body itself is too vague for
    detect_meeting to pin down a real date/time (needs_clarification) -- there is no
    real detected date to match either candidate against."""

    def analyze_email(self, email):
        result = super().analyze_email(email)
        result["meetings_mentioned"] = [
            {"date_phrase": "Friday", "attendees": [], "is_past": False, "actions_raised": []},
            {"date_phrase": "next Monday", "attendees": [], "is_past": False, "actions_raised": []},
        ]
        return result


def test_pipeline_calendar_action_meeting_id_stays_unset_when_ambiguous(db, settings):
    # "Let's connect soon" triggers detect_meeting's own ambiguous-phrase path
    # (needs_clarification=True, ​no real date ever parsed) while two DIFFERENT
    # meetings were still extracted by the LLM -- there is no evidence connecting the
    # placeholder calendar action to either specific one, so it must stay unset rather
    # than guessing "the first one".
    payloads = [_raw_email("m_ambiguous_meeting", "Let's connect soon to discuss next steps.")]
    run_pipeline(
        db, MockEmailProvider(payloads=payloads), _TwoMeetingsAmbiguousLLM(), MockCalendarProvider(), settings,
    )

    stored = db.emails.find_one({"message_id": "m_ambiguous_meeting"}, {"_id": 0})
    assert len(stored["entities_referenced"]["meetings"]) == 2

    action = CalendarActionRepository(db).find_one({})  # exactly one calendar action created in this test
    assert action["status"] == "needs_clarification"
    assert action["meeting_id"] is None
    assert action["event"]["attendees"] == []
