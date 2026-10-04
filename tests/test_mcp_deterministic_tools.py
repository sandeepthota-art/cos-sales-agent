"""Tests for the deterministic, LLM-free MCP boundary added for the
Claude-Desktop-reasoning architecture (app.mcp.tools.ingest_email /
persist_email_analysis / persist_context_delta / create_reply_draft /
mark_email_completed).

All tests use mongomock (no production MongoDB), never construct a real
ClaudeProvider/OpenAIProvider, and never process real Gmail. Several tests
explicitly patch ProviderFactory.create_llm_provider to RAISE if called, to prove
these tools never reach the configured LLM provider even in the one place the
existing pipeline can touch an LLM downstream of analysis (the ambiguous-band
verify_same_fact call inside knowledge deduplication).

Canonical ID refactor: message_id/thread_id are always EML-nnn/THR-nnn from
ingest_email onward -- every fixture below uses a raw source id (e.g. "msg_001")
only when BUILDING the raw email, and the canonical "EML-00N"/"THR-00N" (the
Nth one allocated in that test's own fresh db) for every call afterward.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.analysis.schemas import EmailAnalysis, MentionedPerson, RawCommitment, RawMeeting
from app.config.settings import Settings
from app.context.models import ContextDelta, DeltaItem, ListFieldDelta
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.email.models import parse_email
from app.mcp import tools
from app.providers.factory import ProviderFactory


def _raw_email(message_id, body="Body.", subject="Enterprise CRM Proposal", **overrides):
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


def _analysis(message_id="EML-001", **overrides):
    base = dict(
        email_id=message_id,
        summary="a summary",
        intent="evaluation",
        goal_pillar="Sales",
        label_applied="Read only",
        confidence=0.8,
    )
    base.update(overrides)
    return EmailAnalysis.model_validate(base)


def _complete_via_granular_tools(db, settings, raw, analysis=None, delta=None):
    """Drives one email through the granular pipeline (ingest -> analyze -> context
    -> complete) to COMPLETED, for a test that just needs a fully-processed baseline
    email as setup -- not itself the thing under test. Used in place of the removed
    process_email (it depended on a server-side LLM_API_KEY that was unreliable in
    this project; every real caller had already moved to these granular tools
    instead)."""
    email = parse_email(raw)
    ingest_result = tools.ingest_email(db, email)
    message_id = ingest_result["message_id"]
    thread_id = ingest_result["thread_id"]
    tools.persist_email_analysis(db, message_id, analysis or _analysis(message_id), settings)
    tools.persist_context_delta(db, thread_id, message_id, delta or ContextDelta())
    tools.mark_email_completed(db, message_id)
    return ingest_result


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
        calendar_provider="mock", llm_provider="claude", llm_api_key="unused", timezone="UTC",
        agent_email="ashok@example.com", agent_name=None,
    )


@pytest.fixture
def no_llm_construction(monkeypatch):
    """Makes any attempt to construct the configured LLM provider fail loudly, so a
    test using this fixture proves the code under test never reaches
    ClaudeProvider/OpenAIProvider/anthropic/openai at all."""

    def _boom(settings):
        raise AssertionError("must not construct the configured LLM provider")

    monkeypatch.setattr(ProviderFactory, "create_llm_provider", staticmethod(_boom))
    return monkeypatch


# --- ingest_email -----------------------------------------------------------------


def test_ingest_email_persists_raw_email_and_resolves_thread(db):
    email = parse_email(_raw_email("msg_001"))

    result = tools.ingest_email(db, email)

    assert result["message_id"] == "EML-001"
    assert result["thread_id"] is not None
    assert result["already_completed"] is False
    assert result["previous_context"] is None

    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["processing_status"]["stage"] == "THREADED"
    assert stored["source_message_id"] == "msg_001"
    thread = ThreadRepository(db).find_one({"thread_id": result["thread_id"]})
    assert "EML-001" in thread["message_ids"]
    assert "msg_001" in thread["source_message_ids"]


def test_ingest_email_returns_human_readable_internal_ids_for_a_new_email(db):
    result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))

    # Canonical identity: message_id/thread_id ARE the internal ids. There is no
    # separate email_internal_id/thread_internal_id field anymore -- emails.id
    # and threads.id were both removed from the schema entirely (true
    # duplicates of message_id/thread_id respectively); see
    # scripts/remove_email_id_record_id_date_fields.py and
    # scripts/remove_thread_id_field.py.
    assert result["message_id"] == "EML-001"
    assert result["thread_id"] == "THR-001"
    assert "email_internal_id" not in result
    assert "thread_internal_id" not in result


def test_ingest_email_returns_internal_ids_for_an_already_completed_email_too(db, settings):
    raw = _raw_email("msg_001", "We currently use Salesforce.")
    _complete_via_granular_tools(db, settings, raw)

    result = tools.ingest_email(db, parse_email(raw))

    assert result["already_completed"] is True
    assert result["message_id"] is not None
    assert result["thread_id"] is not None


def test_ingest_email_never_calls_llm(db, no_llm_construction):
    # No settings/llm_provider parameter even exists on this tool -- this proves it
    # regardless, using the same proof mechanism as the other tools' tests.
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))


def test_ingest_email_skips_an_already_completed_email_without_reprocessing(db, settings):
    raw = _raw_email("msg_001", "We currently use Salesforce.")
    _complete_via_granular_tools(db, settings, raw)
    before_count = EmailRepository(db).find_many({}).__len__()

    result = tools.ingest_email(db, parse_email(raw))

    assert result["already_completed"] is True
    assert result["thread_id"] is not None
    after_count = EmailRepository(db).find_many({}).__len__()
    assert after_count == before_count  # no duplicate email document created
    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["processing_status"]["stage"] == "COMPLETED"  # not reset/downgraded


def test_ingest_email_returns_empty_thread_timeline_for_a_threads_first_message(db):
    result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))

    assert result["thread_timeline"] == []


def test_ingest_email_returns_prior_messages_in_thread_timeline_excluding_itself(db, settings):
    first = _raw_email("msg_001", "We currently use Salesforce.")
    _complete_via_granular_tools(db, settings, first)

    second = _raw_email(
        "msg_002", "Following up on Salesforce pricing.", timestamp="2026-09-14T10:30:00Z",
        in_reply_to="msg_001", references=["msg_001"],
    )
    result = tools.ingest_email(db, parse_email(second))

    assert [m["message_id"] for m in result["thread_timeline"]] == ["EML-001"]
    assert result["thread_timeline"][0]["body"] == "We currently use Salesforce."


def test_ingest_email_returns_previous_context_for_an_existing_thread(db, settings):
    first = _raw_email("msg_001", "We currently use Salesforce but pricing is a pain point.")
    _complete_via_granular_tools(
        db, settings, first, delta=ContextDelta(summary="They use Salesforce; pricing is a pain point.")
    )

    second = _raw_email(
        "msg_002", "Following up on Salesforce pricing.", timestamp="2026-09-14T10:30:00Z"
    )
    result = tools.ingest_email(db, parse_email(second))

    assert result["previous_context"] is not None
    assert "Salesforce" in result["previous_context"]["summary"]


# --- persist_email_analysis --------------------------------------------------------


def test_persist_email_analysis_requires_ingest_email_first(db, settings):
    with pytest.raises(ValueError, match="ingest_email"):
        tools.persist_email_analysis(db, "never_ingested", _analysis(), settings)


def test_persist_email_analysis_never_constructs_the_configured_llm_provider(db, settings, no_llm_construction):
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.persist_email_analysis(db, "EML-001", _analysis(message_id="EML-001"), settings)


def test_persist_email_analysis_resolves_people_and_stores_entities_referenced(db, settings):
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        message_id="EML-001",
        people_mentioned=[MentionedPerson(name="Jane Doe", email="jane@example.com", org=None, role_hint=None)],
    )

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    # 3, not 1: envelope-based resolution also resolves the sender (john@example.com,
    # from _raw_email's default "from") in addition to the LLM's own people_mentioned
    # (Jane) -- and the recipient (ashok@example.com == settings.agent_email in this
    # fixture) resolves to the operator's own dedicated profile, not an ordinary
    # Person, but still tracked and still referenced here.
    assert len(result["entities_referenced"]["people"]) == 3
    sender = PersonRepository(db).find_one({"email": "john@example.com"})
    mentioned = PersonRepository(db).find_one({"email": "jane@example.com"})
    assert sender is not None
    assert mentioned is not None
    assert mentioned["id"].startswith("PER-")
    operator = PersonRepository(db).find_one({"email": "ashok@example.com"})
    assert operator is not None
    # Operator identity is resolved purely by email -- `role` is never used to detect
    # or mark the operator, and is unset here since no role_hint was supplied for them.
    assert operator.get("role") is None

    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["entities_referenced"]["people"] == result["entities_referenced"]["people"]
    assert stored["processing_status"]["stage"] == "MEETING_PROCESSED"


def test_persist_email_analysis_creates_commitment_and_follow_up_with_matching_thread_id(db, settings):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        message_id="EML-001",
        commitments_mentioned=[
            RawCommitment.model_validate(
                {"what": "send the proposal", "class": "mine", "date_phrase": "tomorrow"}
            )
        ],
    )

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    commitment_id = result["entities_referenced"]["commitments"][0]
    follow_up_id = result["entities_referenced"]["follow_ups"][0]
    assert commitment_id.startswith("CMT-")
    assert follow_up_id.startswith("FUP-")

    commitment = CommitmentRepository(db).find_one({"id": commitment_id})
    follow_up = FollowUpRepository(db).find_one({"id": follow_up_id})
    assert commitment["thread_id"] == ingest["thread_id"]
    assert follow_up["thread_id"] == ingest["thread_id"]
    assert follow_up["commitment_id"] == commitment_id
    assert commitment["committed_date"] is not None  # "tomorrow" resolved deterministically


def test_persist_email_analysis_flags_possible_missed_commitment_when_body_suggests_one(db, settings):
    # Real gap found in production: an email stating two explicit, dated
    # commitments ("I'll send X by Oct 6") reached COMPLETED with
    # commitments_mentioned=[] and nothing caught it until a manual DB check
    # days later. This heuristic (reusing MockLLMProvider's own commitment-
    # language regex) surfaces it immediately instead.
    tools.ingest_email(
        db, parse_email(_raw_email("msg_001", "I'll send the proposal over by Friday."))
    )
    analysis = _analysis(message_id="EML-001", commitments_mentioned=[])

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    assert result["possible_missed_commitment"] is not None
    assert "commitment" in result["possible_missed_commitment"].lower()


def test_persist_email_analysis_does_not_flag_when_a_commitment_was_actually_extracted(db, settings):
    tools.ingest_email(
        db, parse_email(_raw_email("msg_001", "I'll send the proposal over by Friday."))
    )
    analysis = _analysis(
        message_id="EML-001",
        commitments_mentioned=[
            RawCommitment.model_validate({"what": "send the proposal", "class": "mine", "date_phrase": "Friday"})
        ],
    )

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    assert result["possible_missed_commitment"] is None


def test_persist_email_analysis_does_not_flag_when_body_has_no_commitment_language(db, settings):
    tools.ingest_email(db, parse_email(_raw_email("msg_001", "Just checking in, no action needed.")))
    analysis = _analysis(message_id="EML-001", commitments_mentioned=[])

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    assert result["possible_missed_commitment"] is None


def test_persist_email_analysis_creates_meeting_entity_and_calendar_proposal(db, settings):
    tools.ingest_email(
        db, parse_email(_raw_email("msg_001", "Let's meet Tuesday at 3 PM for 30 minutes."))
    )
    analysis = _analysis(
        message_id="EML-001",
        meetings_mentioned=[
            RawMeeting.model_validate({"date_phrase": "Tuesday", "attendees": [], "is_past": False})
        ],
    )

    result = tools.persist_email_analysis(db, "EML-001", analysis, settings)

    meeting_id = result["entities_referenced"]["meetings"][0]
    assert meeting_id.startswith("MTG-")
    assert MeetingRepository(db).find_one({"id": meeting_id}) is not None
    assert result["calendar_proposal"] is not None
    assert result["calendar_proposal"]["status"] == "awaiting_approval"
    assert CalendarActionRepository(db).find_many({}) != []


def test_persist_email_analysis_processes_knowledge_facts(db, settings):
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        message_id="EML-001",
        facts=[{"subject": "Customer", "predicate": "uses", "object": "Salesforce"}],
    )

    tools.persist_email_analysis(db, "EML-001", analysis, settings)

    items = KnowledgeRepository(db).all_for_thread(tools._thread_id_index(db)["EML-001"])
    assert any(i["current_value"] == "Salesforce" for i in items)


def test_persist_email_analysis_ambiguous_knowledge_band_never_calls_configured_llm_provider(
    db, settings, no_llm_construction
):
    # "pricing request" vs "proposal request" score ~64.5 on rapidfuzz token_sort_ratio
    # -- inside deduplication.py's ambiguous band (60-90), which is the one place
    # _process_knowledge can reach llm.verify_same_fact(). settings.llm_provider is
    # "claude" here specifically to prove it's irrelevant -- this path must use
    # MockLLMProvider internally regardless of what's configured.
    thread_email_1 = _raw_email("msg_001", "We need pricing.")
    tools.ingest_email(db, parse_email(thread_email_1))
    tools.persist_email_analysis(
        db, "EML-001", _analysis(message_id="EML-001", buying_signals=["pricing request"]), settings
    )

    thread_email_2 = _raw_email(
        "msg_002", "Re: pricing", subject="Enterprise CRM Proposal", timestamp="2026-09-14T10:30:00Z"
    )
    tools.ingest_email(db, parse_email(thread_email_2))
    result = tools.persist_email_analysis(
        db, "EML-002", _analysis(message_id="EML-002", buying_signals=["proposal request"]), settings
    )

    # Completed without raising (no_llm_construction would have raised if a real
    # provider had been constructed) -- the specific merge/no-merge outcome is
    # MockLLMProvider's own already-tested rapidfuzz-threshold behavior, not
    # re-verified here.
    assert result["thread_id"] is not None


# --- persist_context_delta ----------------------------------------------------------


def test_persist_context_delta_creates_first_version(db):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    delta = ContextDelta(
        requirements=ListFieldDelta(added=[DeltaItem(value="100 seats", basis="stated")])
    )

    result = tools.persist_context_delta(db, ingest["thread_id"], "EML-001", delta)

    assert result["context_version"] == 1
    assert result["already_persisted"] is False
    assert any(r["value"] == "100 seats" for r in result["context"]["requirements"])
    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["processing_status"]["stage"] == "CONTEXT_BUILT"


def test_persist_context_delta_never_calls_llm(db, no_llm_construction):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.persist_context_delta(db, ingest["thread_id"], "EML-001", ContextDelta())


def test_persist_context_delta_is_idempotent_on_repeat_call(db):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    delta = ContextDelta(requirements=ListFieldDelta(added=[DeltaItem(value="100 seats", basis="stated")]))

    first = tools.persist_context_delta(db, ingest["thread_id"], "EML-001", delta)
    second = tools.persist_context_delta(db, ingest["thread_id"], "EML-001", delta)

    assert first["already_persisted"] is False
    assert second["already_persisted"] is True
    assert second["context_version"] == first["context_version"]
    snapshots = ContextSnapshotRepository(db).find_many(
        {"thread_id": ingest["thread_id"], "triggering_email_id": "EML-001"}
    )
    assert len(snapshots) == 1  # no duplicate version created


def test_persist_context_delta_builds_on_the_previous_snapshot_in_the_same_thread(db):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.persist_context_delta(
        db, ingest1["thread_id"], "EML-001",
        ContextDelta(requirements=ListFieldDelta(added=[DeltaItem(value="100 seats", basis="stated")])),
    )

    email2 = _raw_email("msg_002", timestamp="2026-09-14T10:30:00Z")
    tools.ingest_email(db, parse_email(email2))
    result = tools.persist_context_delta(
        db, ingest1["thread_id"], "EML-002",
        ContextDelta(pain_points=ListFieldDelta(added=[DeltaItem(value="slow onboarding", basis="stated")])),
    )

    assert result["context_version"] == 2
    values = {r["value"] for r in result["context"]["requirements"]}
    assert "100 seats" in values  # carried forward, not lost
    assert any(r["value"] == "slow onboarding" for r in result["context"]["pain_points"])


# --- create_reply_draft --------------------------------------------------------------


def test_create_reply_draft_persists_supplied_text_verbatim(db):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))

    result = tools.create_reply_draft(
        db, "EML-001", ingest["thread_id"], "Re: Enterprise CRM Proposal", "Thanks, here is the info."
    )

    assert result["already_existed"] is False
    assert result["draft"]["subject"] == "Re: Enterprise CRM Proposal"
    assert result["draft"]["body"] == "Thanks, here is the info."
    assert result["status"] == "awaiting_approval"
    stored = ReplyDraftRepository(db).find_one({"source_email_id": "EML-001"})
    assert stored["draft"]["body"] == "Thanks, here is the info."
    email_stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert email_stored["processing_status"]["stage"] == "REPLY_PROCESSED"


def test_create_reply_draft_never_calls_llm(db, no_llm_construction):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.create_reply_draft(db, "EML-001", ingest["thread_id"], "subject", "body")


def test_create_reply_draft_never_overwrites_an_existing_draft(db):
    ingest = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.create_reply_draft(db, "EML-001", ingest["thread_id"], "First subject", "First body")

    result = tools.create_reply_draft(db, "EML-001", ingest["thread_id"], "Second subject", "Second body")

    assert result["already_existed"] is True
    stored = ReplyDraftRepository(db).find_one({"source_email_id": "EML-001"})
    assert stored["draft"]["body"] == "First body"  # not overwritten
    assert ReplyDraftRepository(db).find_many({"source_email_id": "EML-001"}).__len__() == 1


# --- mark_email_completed ------------------------------------------------------------


def test_mark_email_completed_succeeds_after_persist_email_analysis(db, settings):
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.persist_email_analysis(db, "EML-001", _analysis(message_id="EML-001"), settings)

    result = tools.mark_email_completed(db, "EML-001")

    assert result["stage"] == "COMPLETED"
    assert result["already_completed"] is False
    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["processing_status"]["stage"] == "COMPLETED"


def test_mark_email_completed_rejects_incomplete_processing(db):
    # Only ingest_email ran -- entities_referenced was never populated by
    # persist_email_analysis. Must not be markable COMPLETED on say-so alone.
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))

    with pytest.raises(ValueError, match="persist_email_analysis"):
        tools.mark_email_completed(db, "EML-001")

    stored = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored["processing_status"]["stage"] == "THREADED"  # unchanged, not silently completed


def test_mark_email_completed_is_idempotent(db, settings):
    tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    tools.persist_email_analysis(db, "EML-001", _analysis(message_id="EML-001"), settings)
    tools.mark_email_completed(db, "EML-001")

    result = tools.mark_email_completed(db, "EML-001")

    assert result["already_completed"] is True


def test_mark_email_completed_raises_for_unknown_message_id(db):
    with pytest.raises(ValueError, match="no email found"):
        tools.mark_email_completed(db, "never_ingested")


# --- Full deterministic round trip, and the 662-baseline-protection guarantee --------


def test_full_deterministic_round_trip_never_constructs_the_configured_llm_provider(
    db, settings, no_llm_construction
):
    """The complete Claude-Desktop-reasoning path: ingest -> analysis (people,
    commitment/follow-up, meeting) -> context -> reply draft -> completion, entirely
    through the new deterministic tools, with the configured LLM provider patched to
    raise if constructed at any point."""
    ingest = tools.ingest_email(
        db, parse_email(_raw_email("msg_001", "Let's meet Tuesday at 3 PM for 30 minutes."))
    )
    analysis = _analysis(
        message_id="EML-001",
        people_mentioned=[MentionedPerson(name="John", email="john@example.com", org=None, role_hint=None)],
        commitments_mentioned=[
            RawCommitment.model_validate({"what": "send the proposal", "class": "mine", "date_phrase": "tomorrow"})
        ],
        meetings_mentioned=[
            RawMeeting.model_validate({"date_phrase": "Tuesday", "attendees": [], "is_past": False})
        ],
    )
    analysis_result = tools.persist_email_analysis(db, "EML-001", analysis, settings)
    tools.persist_context_delta(
        db, ingest["thread_id"], "EML-001",
        ContextDelta(summary="Customer wants a meeting and will send a proposal."),
    )
    tools.create_reply_draft(db, "EML-001", ingest["thread_id"], "Re: Enterprise CRM Proposal", "Sounds good.")
    completion = tools.mark_email_completed(db, "EML-001")

    assert completion["stage"] == "COMPLETED"
    assert len(analysis_result["entities_referenced"]["commitments"]) == 1
    assert len(analysis_result["entities_referenced"]["meetings"]) == 1
    assert EmailRepository(db).find_one({"message_id": "EML-001"})["processing_status"]["stage"] == "COMPLETED"


def test_ingest_email_treats_a_previously_completed_email_as_a_baseline_email_to_skip(db, settings):
    """Simulates the 662-historical-email protection: an email already COMPLETED
    (however it got that way) must be reported as already_completed and never
    reprocessed by the new deterministic path, exactly like run_pipeline's own guard."""
    baseline_raw = _raw_email("baseline_msg_001", "Historical email.")
    _complete_via_granular_tools(db, settings, baseline_raw)
    stored_before = EmailRepository(db).find_one({"message_id": "EML-001"})

    result = tools.ingest_email(db, parse_email(baseline_raw))

    assert result["already_completed"] is True
    stored_after = EmailRepository(db).find_one({"message_id": "EML-001"})
    assert stored_after == stored_before  # byte-for-byte untouched


# --- persist_organization_research / list_unresearched_organizations --------------


def test_persist_organization_research_sets_profile_fields_and_researched_at(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "databeat.io", "domain": "databeat.io", "aliases": [], "source": "gmail"},
    )

    result = tools.persist_organization_research(
        db, "ORG-001", industry="Data Analytics", description="A BI platform vendor.",
        products_services=["dashboards"], size_estimate="11-50 employees",
        headquarters="Bengaluru, India", website="https://databeat.io",
    )

    assert result["industry"] == "Data Analytics"
    assert result["research_source"] == "web_research"
    assert result["researched_at"] is not None
    stored = OrganizationRepository(db).find_one({"id": "ORG-001"})
    assert stored["industry"] == "Data Analytics"


def test_persist_organization_research_sets_researched_at_even_when_inconclusive(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "databeat.io", "domain": "databeat.io", "aliases": [], "source": "gmail"},
    )

    result = tools.persist_organization_research(db, "ORG-001")

    assert result["industry"] is None
    assert result["researched_at"] is not None
    assert result["research_source"] == "web_research"


def test_persist_organization_research_never_overwrites_a_manual_field(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {
            "id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail",
            "industry": "Hand-verified Industry", "research_source": "manual",
        },
    )

    result = tools.persist_organization_research(db, "ORG-001", industry="Auto-researched Industry")

    assert result["industry"] == "Hand-verified Industry"
    assert result["research_source"] == "manual"


def test_persist_organization_research_raises_for_unknown_org(db):
    with pytest.raises(ValueError, match="ORG-999"):
        tools.persist_organization_research(db, "ORG-999")


def test_list_unresearched_organizations_returns_only_orgs_with_no_researched_at(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "New Co", "domain": "newco.com", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"},
        {
            "id": "ORG-002", "name": "Researched Co", "domain": "researched.com", "aliases": [], "source": "gmail",
            "researched_at": datetime.now(timezone.utc).isoformat(), "research_source": "web_research",
        },
    )

    result = tools.list_unresearched_organizations(db)

    assert [o["id"] for o in result] == ["ORG-001"]


# --- new_organizations_needing_research signal in persist_email_analysis ----------


def test_persist_email_analysis_surfaces_a_new_organization_needing_research(db, settings):
    raw = _raw_email("msg_001")
    email = parse_email(raw)
    ingest_result = tools.ingest_email(db, email)

    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    # Membership, not exact count/length: the envelope sender (john@example.com,
    # from _raw_email's own default) is independently resolved to its own
    # brand-new Organization by the pre-existing envelope-resolution loop --
    # that's correct, pre-existing, unrelated behavior, not something this
    # assertion should be thrown off by.
    surfaced_by_domain = {o["domain"]: o for o in result["new_organizations_needing_research"]}
    assert "newco.com" in surfaced_by_domain
    assert surfaced_by_domain["newco.com"]["name"] == "NewCo"


def test_persist_email_analysis_surfaces_every_new_organization_in_one_email(db, settings):
    raw = _raw_email("msg_001")
    email = parse_email(raw)
    ingest_result = tools.ingest_email(db, email)

    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[
            MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo"),
            MentionedPerson(name="Raj Patel", email="raj@othernewco.com", org="OtherNewCo"),
        ],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    surfaced_domains = {o["domain"] for o in result["new_organizations_needing_research"]}
    assert {"newco.com", "othernewco.com"} <= surfaced_domains


def test_persist_email_analysis_resurfaces_an_unresearched_org_on_a_later_email(db, settings):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis1 = _analysis(
        ingest1["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    tools.persist_email_analysis(db, ingest1["message_id"], analysis1, settings)

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    surfaced_domains = {o["domain"] for o in result2["new_organizations_needing_research"]}
    assert "newco.com" in surfaced_domains


def test_persist_email_analysis_stops_surfacing_an_org_once_researched(db, settings):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis1 = _analysis(
        ingest1["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    result1 = tools.persist_email_analysis(db, ingest1["message_id"], analysis1, settings)
    newco_entry = next(
        o for o in result1["new_organizations_needing_research"] if o["domain"] == "newco.com"
    )
    tools.persist_organization_research(db, newco_entry["org_id"], industry="Software")

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    surfaced_domains = {o["domain"] for o in result2["new_organizations_needing_research"]}
    assert "newco.com" not in surfaced_domains
