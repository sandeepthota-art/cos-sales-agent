# tests/test_phase20_1_canonical_boundary_fix.py
"""Phase 20.1: closes the canonical-resolution boundary gap Phase 20 found (a raw
`PersonRepository(db).find_one({"email": ...})` lookup in app.pipeline.run_pipeline,
app.mcp.tools, and app.entity_migration bypassing the Phase 19.1 lifecycle
redirect). All tests here are mongomock only -- no Atlas data is touched, no Person
is merged, no index is created.

Two layers of coverage:
  1. Direct unit tests of the new shared boundary,
     app.entities.resolution.resolve_canonical_person_for_email -- cheap, precise,
     one topology per test.
  2. A handful of full-pipeline integration tests proving ReplyDraft/CalendarAction
     actually pick up the fix end to end.
"""
import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import CalendarActionRepository, PersonRepository, ReplyDraftRepository
from app.entities.lifecycle import CanonicalResolutionError
from app.entities.resolution import resolve_canonical_person_for_email
from app.pipeline import run_pipeline
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


def _person(**overrides):
    person_id = overrides.get("id", "PER-1")
    doc = {
        "id": person_id, "name": "Someone", "email": f"{person_id.lower()}@example.com", "aliases": [],
        "org": None, "org_id": None, "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": [], "note_link": None, "review_flag": False, "source": "gmail",
        "status": "active", "merged_into": None,
    }
    doc.update(overrides)
    return doc


# =============================================================================================
# Layer 1: direct unit tests of resolve_canonical_person_for_email
# =============================================================================================


def test_unit_unknown_email_returns_none(db):
    # Section 11: a genuinely unknown identity is NOT force-attached to anything --
    # this boundary function creates nothing; that stays resolve_person's job.
    assert resolve_canonical_person_for_email(db, "nobody@example.com") is None


def test_unit_active_person_returns_itself(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="active@example.com"))

    found = resolve_canonical_person_for_email(db, "Active@Example.com")  # case-insensitive

    assert found["id"] == "PER-1"


def test_unit_merged_person_resolves_to_canonical(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", email="canonical@example.com"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-1"}, _person(id="PER-1", email="merged@example.com", status="merged", merged_into="PER-2")
    )

    found = resolve_canonical_person_for_email(db, "merged@example.com")

    assert found["id"] == "PER-2"


def test_unit_two_hop_chain_resolves_to_final_active(db):
    # Section 14: A -> B -> C, C active.
    PersonRepository(db).upsert_by_key({"id": "PER-C"}, _person(id="PER-C", email="c@example.com"))
    PersonRepository(db).upsert_by_key({"id": "PER-B"}, _person(id="PER-B", email="b@example.com", status="merged", merged_into="PER-C"))
    PersonRepository(db).upsert_by_key({"id": "PER-A"}, _person(id="PER-A", email="a@example.com", status="merged", merged_into="PER-B"))

    found = resolve_canonical_person_for_email(db, "a@example.com")

    assert found["id"] == "PER-C"


def test_unit_broken_merged_into_target_raises_controlled_error(db):
    # Section 13: missing merged_into target.
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="broken@example.com", status="merged", merged_into=None))

    with pytest.raises(CanonicalResolutionError, match="no merged_into target"):
        resolve_canonical_person_for_email(db, "broken@example.com")


def test_unit_missing_downstream_target_raises_controlled_error(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-1"}, _person(id="PER-1", email="dangling@example.com", status="merged", merged_into="PER-999")
    )

    with pytest.raises(CanonicalResolutionError, match="does not exist"):
        resolve_canonical_person_for_email(db, "dangling@example.com")


def test_unit_cycle_raises_controlled_error(db):
    # Section 15: A -> B -> A.
    PersonRepository(db).upsert_by_key({"id": "PER-A"}, _person(id="PER-A", email="a@example.com", status="merged", merged_into="PER-B"))
    PersonRepository(db).upsert_by_key({"id": "PER-B"}, _person(id="PER-B", email="b@example.com", status="merged", merged_into="PER-A"))

    with pytest.raises(CanonicalResolutionError, match="circular"):
        resolve_canonical_person_for_email(db, "a@example.com")


def test_unit_never_writes_to_atlas(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", email="canonical@example.com"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-1"}, _person(id="PER-1", email="merged@example.com", status="merged", merged_into="PER-2")
    )
    before = list(db.people.find({}, {"_id": 0}))

    resolve_canonical_person_for_email(db, "merged@example.com")

    after = list(db.people.find({}, {"_id": 0}))
    assert before == after


# =============================================================================================
# Layer 2: full-pipeline integration -- ReplyDraft/CalendarAction invariants (Sections 7-11)
# =============================================================================================


def _payload(sender_email: str, body: str, agent_email: str) -> dict:
    return {
        "message_id": "msg_001", "from": {"name": "Sender", "email": sender_email},
        "to": [{"name": "Agent", "email": agent_email}], "subject": "Enterprise CRM Proposal",
        "body": body, "timestamp": "2026-09-13T10:30:00Z",
    }


def test_active_person_reply_draft_and_calendar_action_unchanged(db, settings):
    # Section 10: normal, unmerged behavior must be exactly as before.
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="active@example.com", open_threads=[]))
    payloads = [_payload("active@example.com", "We currently use Salesforce but pricing is a pain point. Let's schedule a call soon.", settings.agent_email)]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft["person_id"] == "PER-1"
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_msg_001"})
    assert action["person_id"] == "PER-1"
    assert PersonRepository(db).find_many({}).__len__() == 1  # no duplicate created


def test_unknown_sender_still_creates_a_new_person_and_reply_draft(db, settings):
    # Section 11: this fix must not prevent legitimate new-identity creation.
    payloads = [_payload("brandnew@example.com", "We currently use Salesforce but pricing is a pain point.", settings.agent_email)]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    people = PersonRepository(db).find_many({"email": "brandnew@example.com"})
    assert len(people) == 1
    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft["person_id"] == people[0]["id"]


def test_merged_person_end_to_end_reply_draft_and_calendar_action(db, settings):
    # Section 9: the focused regression fixture the task specifies, using synthetic
    # (non-production) ids.
    PersonRepository(db).upsert_by_key({"id": "PER-TEST-B"}, _person(id="PER-TEST-B", email="canonical@example.com"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-TEST-A"}, _person(id="PER-TEST-A", email="person@example.com", status="merged", merged_into="PER-TEST-B")
    )
    payloads = [_payload("person@example.com", "We currently use Salesforce but pricing is a pain point. Let's schedule a call soon.", settings.agent_email)]

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft["person_id"] == "PER-TEST-B"
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_msg_001"})
    assert action["person_id"] == "PER-TEST-B"
    assert action["event"]["attendees"] == []  # Section 8: hard safety invariant preserved
    # No third Person was created.
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_ambiguous_no_email_mentions_do_not_affect_the_concrete_sender_boundary(db, settings):
    # Section 12: the sender of an email always has a concrete address -- exact-email
    # resolution has no ambiguity tier. Two org-matching, non-overlapping-thread
    # anchors sharing a first name (an unrelated ambiguous no-email mention scenario)
    # must not perturb the sender's own deterministic reply-draft/calendar linking.
    PersonRepository(db).upsert_by_key({"id": "PER-AMBI-1"}, _person(id="PER-AMBI-1", name="Sam One", email="samone@shared.com", org="Shared Co", open_threads=["other_thread_1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-AMBI-2"}, _person(id="PER-AMBI-2", name="Sam Two", email="samtwo@shared.com", org="Shared Co", open_threads=["other_thread_2"]))
    payloads = [_payload("active@example.com", "We currently use Salesforce but pricing is a pain point.", settings.agent_email)]
    PersonRepository(db).upsert_by_key({"id": "PER-SENDER"}, _person(id="PER-SENDER", email="active@example.com"))

    run_pipeline(db, MockEmailProvider(payloads=payloads), MockLLMProvider(), MockCalendarProvider(), settings)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "msg_001"})
    assert draft["person_id"] == "PER-SENDER"
    # The two ambiguous, unrelated anchors are untouched -- no merge, no new record.
    assert PersonRepository(db).find_many({}).__len__() == 3
