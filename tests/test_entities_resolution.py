# tests/test_entities_resolution.py
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    FollowUpRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
)
from app.entities.resolution import (
    derive_follow_up,
    resolve_commitment,
    resolve_meeting,
    resolve_organization,
    resolve_person,
    resolve_personal_item,
    resolve_project,
)

_NOW = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def test_resolve_person_merges_on_exact_email(db):
    first = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    second = resolve_person(
        db, {"name": "Jane Doe", "email": "Jane@Example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    assert first == second
    assert PersonRepository(db).find_many({}).__len__() == 1


def test_resolve_person_same_name_no_email_same_thread_reuses_person(db):
    first = resolve_person(db, {"name": "Sam Lee", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1")
    second = resolve_person(db, {"name": "Sam Lee", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1")

    assert first == second
    people = PersonRepository(db).find_many({})
    assert len(people) == 1
    assert people[0]["review_flag"] is True
    # The "email" key must be OMITTED (not stored as null) for a no-email Person -- a
    # sparse unique index on real MongoDB only excludes a document where the field is
    # entirely missing, not one where it is present with value null. Storing "email": null
    # would collide with a second no-email Person's sparse-unique index entry on real
    # MongoDB, even though mongomock's more lenient interpretation would not catch it.
    assert "email" not in people[0]


def test_resolve_person_same_name_no_email_different_thread_does_not_merge(db):
    # The exact real-world distinction the approved design requires: same name, no email,
    # is strong enough evidence to reuse a record WITHIN one conversation, but never
    # strong enough to merge across two unrelated threads.
    first = resolve_person(db, {"name": "Sam Lee", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1")
    second = resolve_person(db, {"name": "Sam Lee", "email": None}, is_sender=None, now=_NOW, thread_id="thread_2")

    assert first != second
    people = PersonRepository(db).find_many({})
    assert len(people) == 2
    assert all(p["review_flag"] is True for p in people)


def test_resolve_person_same_name_different_emails_creates_different_people(db):
    first = resolve_person(
        db, {"name": "Sam Lee", "email": "sam@acme.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    second = resolve_person(
        db, {"name": "Sam Lee", "email": "sam@othercorp.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    assert first != second


def test_resolve_person_partial_name_does_not_match_fuller_name_even_in_same_thread(db):
    # "Ashok" and "Ashok Ganapam" are different normalized strings -- the thread-scoped
    # exact-name rule must not fuzzy- or substring-match them, by design (normalization
    # only, not fuzzy matching).
    full = resolve_person(
        db, {"name": "Ashok Ganapam", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1"
    )
    partial = resolve_person(
        db, {"name": "Ashok", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1"
    )

    assert full != partial
    assert len(PersonRepository(db).find_many({})) == 2


def test_resolve_person_ambiguous_same_thread_candidates_are_never_silently_merged(db):
    # Seed two DIFFERENT no-email Person records that both already happen to be linked to
    # the same thread with the same normalized name (e.g. from upstream data conditions
    # this rule doesn't otherwise produce) -- resolve_person must never guess between
    # them; it must create a third, newly flagged record rather than pick one.
    repo = PersonRepository(db)
    for existing_id in ("PER-900", "PER-901"):
        repo.upsert_by_key(
            {"id": existing_id},
            {
                "id": existing_id, "name": "Sam Lee", "aliases": [], "org": None, "review_flag": True,
                "source": "gmail", "open_threads": ["thread_1"],
            },
        )

    resolved = resolve_person(db, {"name": "Sam Lee", "email": None}, is_sender=None, now=_NOW, thread_id="thread_1")

    assert resolved not in ("PER-900", "PER-901")
    assert len(PersonRepository(db).find_many({})) == 3


def test_resolve_person_touches_last_inbound_when_sender(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] is not None
    assert stored["last_outbound"] is None


# --- last_inbound merge (regression: PER-827 -- a later inbound email must always move
# last_inbound FORWARD to the newest timestamp, never leave an earlier one in place) ---


def test_resolve_person_last_inbound_moves_forward_on_newer_inbound(db):
    earlier = _NOW
    later = _NOW.replace(minute=45)
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=earlier, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=later, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == later.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_inbound_never_moves_backwards_on_older_inbound(db):
    # The exact observed bug: 5 inbound emails processed, and the OLDEST timestamp
    # ended up stored instead of the newest -- an older email arriving/reprocessing
    # after a newer one must never overwrite last_inbound with an earlier value.
    later = _NOW.replace(minute=45)
    earlier = _NOW
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=later, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=earlier, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == later.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_inbound_stable_on_equal_timestamp(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == _NOW.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_inbound_sets_when_previously_null(db):
    # An existing Person created from an outbound-only mention (last_inbound never
    # set) later receives a real inbound email -- last_inbound must be set, not left
    # null because "existing already has a value to compare against" logic misfires
    # on a genuinely absent value.
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_1"
    )
    assert PersonRepository(db).find_one({"id": person_id})["last_inbound"] is None

    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == _NOW.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_inbound_reflects_latest_across_out_of_order_processing(db):
    # 5 inbound emails processed out of chronological order (e.g. a batch re-run, or
    # emails arriving out of send-time order) -- last_inbound must end up as the
    # MAXIMUM of all five timestamps regardless of processing order.
    timestamps = [
        _NOW.replace(minute=m) for m in (30, 10, 45, 0, 20)
    ]  # latest (45) is neither first nor last
    person_id = None
    for ts in timestamps:
        person_id = resolve_person(
            db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=ts, thread_id="thread_1"
        )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == max(timestamps).isoformat().replace("+00:00", "Z")


# --- last_outbound merge (symmetric fix: last_outbound previously overwrote
# unconditionally on every outbound mention -- now uses the same forward-only merge
# rule as last_inbound, via the shared _forward_only_timestamp_update helper) ---


def test_resolve_person_last_outbound_sets_when_previously_null(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    assert PersonRepository(db).find_one({"id": person_id})["last_outbound"] is None

    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == _NOW.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_outbound_moves_forward_on_newer_outbound(db):
    earlier = _NOW
    later = _NOW.replace(minute=45)
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=earlier, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=later, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == later.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_outbound_never_moves_backwards_on_older_outbound(db):
    later = _NOW.replace(minute=45)
    earlier = _NOW
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=later, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=earlier, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == later.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_outbound_stable_on_equal_timestamp(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == _NOW.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_outbound_unaffected_by_a_mention_with_no_sender_role(db):
    # "new timestamp is None" in practice: a mention with is_sender=None contributes no
    # new outbound (or inbound) information at all -- neither branch in resolve_person
    # runs, so a valid existing last_outbound must never be cleared or overwritten.
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=None, now=_NOW.replace(minute=45),
        thread_id="thread_1",
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == _NOW.isoformat().replace("+00:00", "Z")


def test_resolve_person_last_outbound_reflects_latest_across_out_of_order_processing(db):
    # 5 outbound emails processed out of chronological order -- last_outbound must end
    # up as the MAXIMUM of all five timestamps regardless of processing order.
    timestamps = [
        _NOW.replace(minute=m) for m in (30, 10, 45, 0, 20)
    ]  # latest (45) is neither first nor last
    person_id = None
    for ts in timestamps:
        person_id = resolve_person(
            db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=ts, thread_id="thread_1"
        )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_outbound"] == max(timestamps).isoformat().replace("+00:00", "Z")


def test_resolve_person_inbound_and_outbound_updates_never_clobber_each_other(db):
    # Interleaved inbound/outbound mentions for the same person must each move only
    # their own field forward, never touching the other -- proves the two merges
    # (sharing the same helper) are applied independently per field.
    inbound_1 = _NOW.replace(minute=0)
    outbound_1 = _NOW.replace(minute=10)
    inbound_2 = _NOW.replace(minute=20)
    outbound_2 = _NOW.replace(minute=30)

    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=inbound_1, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=outbound_1, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=inbound_2, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=outbound_2, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["last_inbound"] == inbound_2.isoformat().replace("+00:00", "Z")
    assert stored["last_outbound"] == outbound_2.isoformat().replace("+00:00", "Z")


def test_resolve_person_populates_open_threads_on_creation(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["open_threads"] == ["thread_1"]


def test_resolve_person_appends_to_open_threads_when_reused_in_a_new_thread(db):
    first = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    second = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_2"
    )

    assert first == second
    stored = PersonRepository(db).find_one({"id": first})
    assert stored["open_threads"] == ["thread_1", "thread_2"]


def test_resolve_person_open_threads_has_no_duplicates_when_reused_in_the_same_thread(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["open_threads"] == ["thread_1"]


def test_resolve_person_never_overwrites_existing_email_with_null(db):
    # A later mention of the same person (matched by email) with a bare/incomplete
    # mention dict must never clobber the already-known email field.
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_2"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["email"] == "jane@example.com"


def test_resolve_person_does_not_lose_existing_data_when_reused(db):
    person_id = resolve_person(
        db, {"name": "Jane", "email": "jane@example.com", "org": "Acme"}, is_sender=True, now=_NOW, thread_id="thread_1"
    )
    resolve_person(
        db, {"name": "Jane", "email": "jane@example.com"}, is_sender=False, now=_NOW, thread_id="thread_2"
    )

    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["org"] == "Acme"
    assert stored["name"] == "Jane"
    assert stored["last_outbound"] is not None


def test_resolve_project_merges_on_exact_name_entity_and_goal_pillar(db):
    first = resolve_project(db, {"name": "Renewal", "org": "Acme"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "renewal", "org": "Acme"}, goal_pillar="Sales")

    assert first == second
    assert len(ProjectRepository(db).find_many({})) == 1


def test_resolve_project_does_not_merge_across_different_entities(db):
    first = resolve_project(db, {"name": "Renewal", "org": "Acme"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "Renewal", "org": "OtherCo"}, goal_pillar="Sales")

    assert first != second


def test_resolve_project_does_not_merge_without_goal_pillar_corroboration(db):
    first = resolve_project(db, {"name": "Renewal", "org": "Acme"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "Renewal", "org": "Acme"}, goal_pillar="Support")

    assert first != second


def test_resolve_project_merges_on_case_variation(db):
    first = resolve_project(db, {"name": "Renewal Deal", "org": "Acme"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "RENEWAL DEAL", "org": "acme"}, goal_pillar="Sales")

    assert first == second


def test_resolve_project_merges_hyphen_and_plus_sign_joiner_variants(db):
    # The exact real-world duplicate pair from the 100-email validation.
    first = resolve_project(
        db, {"name": "DataBeat-Speedvision introduction", "org": "Fastener"}, goal_pillar="Sales"
    )
    second = resolve_project(
        db, {"name": "DataBeat + Speedvision introduction", "org": "Fastener"}, goal_pillar="Sales"
    )

    assert first == second
    assert len(ProjectRepository(db).find_many({})) == 1


def test_resolve_project_merges_accented_and_unaccented_entity_spellings(db):
    # The exact real-world duplicate pair -- note this is an ENTITY (org) spelling
    # difference, not a project-name difference, and previously wasn't even reachable by
    # the old exact Mongo-level {"entity": entity} filter.
    first = resolve_project(db, {"name": "Predictive analytics initiative", "org": "Condé Nast"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "Predictive analytics initiative", "org": "Conde Nast"}, goal_pillar="Sales")

    assert first == second
    assert len(ProjectRepository(db).find_many({})) == 1


def test_resolve_project_still_separates_genuinely_different_names(db):
    # Regression guard against over-aggressive normalization: two real, different deals
    # with the same organization must stay separate.
    first = resolve_project(db, {"name": "Renewal discussion", "org": "Acme"}, goal_pillar="Sales")
    second = resolve_project(db, {"name": "New logo outreach", "org": "Acme"}, goal_pillar="Sales")

    assert first != second


def test_resolve_project_does_not_introduce_fuzzy_matching(db):
    # Two names that a human might guess are "close enough" but are not the same
    # normalized string after accent/joiner handling -- must NOT merge. Only exact
    # normalized-string equality is used; no similarity scoring of any kind.
    first = resolve_project(db, {"name": "Ad ops engagement", "org": "UrbanDictionary.com"}, goal_pillar="Sales")
    second = resolve_project(
        db, {"name": "Ad ops resourcing for Urban Dictionary", "org": "UrbanDictionary.com"}, goal_pillar="Sales"
    )

    assert first != second


def test_resolve_commitment_deduplicates_within_same_thread(db):
    raw = {"what": "Send updated case study", "class": "mine", "date_phrase": None}
    first = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_001", made_on=_NOW,
        resolved_date=None, date_type=None, goal_pillar="Sales", project_id=None,
    )
    second = resolve_commitment(
        db, thread_id="thread_1", raw={"what": "send updated case study", "class": "mine", "date_phrase": None},
        message_id="msg_002", made_on=_NOW, resolved_date=None, date_type=None,
        goal_pillar="Sales", project_id=None,
    )

    assert first == second
    assert len(CommitmentRepository(db).all_for_thread("thread_1")) == 1


def test_resolve_commitment_creates_new_for_different_date(db):
    raw_a = {"what": "Send case study", "class": "mine", "date_phrase": None}
    first = resolve_commitment(
        db, thread_id="thread_1", raw=raw_a, message_id="msg_001", made_on=_NOW,
        resolved_date=datetime(2026, 9, 20, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )
    second = resolve_commitment(
        db, thread_id="thread_1", raw=raw_a, message_id="msg_002", made_on=_NOW,
        resolved_date=datetime(2026, 9, 27, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )

    assert first != second


def test_resolve_commitment_stated_date_still_uses_exact_instant_matching(db):
    raw = {"what": "Chat at 10am", "class": "theirs", "date_phrase": None}
    first = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_001", made_on=_NOW,
        resolved_date=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc), date_type="stated",
        goal_pillar="Sales", project_id=None,
    )
    same_instant = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_002", made_on=_NOW,
        resolved_date=datetime(2026, 9, 20, 10, 0, tzinfo=timezone.utc), date_type="stated",
        goal_pillar="Sales", project_id=None,
    )
    assert first == same_instant

    different_instant_same_day = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_003", made_on=_NOW,
        resolved_date=datetime(2026, 9, 20, 14, 0, tzinfo=timezone.utc), date_type="stated",
        goal_pillar="Sales", project_id=None,
    )
    # Explicit/stated dates must NOT get the same-calendar-day leniency -- a different
    # exact stated time is a genuinely different commitment.
    assert first != different_instant_same_day


def test_resolve_commitment_inferred_date_merges_within_the_same_calendar_day(db):
    # The exact real-world COM-129/COM-132 case: "Chat at 10am" restated in two emails
    # sent a few hours apart on the same day, each resolving "10am" relative to that
    # email's own send time.
    raw = {"what": "Chat at 10am", "class": "theirs", "date_phrase": "10am"}
    first = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_001", made_on=_NOW,
        resolved_date=datetime(2025, 2, 7, 14, 24, 58, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )
    second = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_002", made_on=_NOW,
        resolved_date=datetime(2025, 2, 7, 16, 22, 49, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )

    assert first == second
    assert len(CommitmentRepository(db).all_for_thread("thread_1")) == 1


def test_resolve_commitment_inferred_date_never_crosses_a_calendar_day_boundary(db):
    raw = {"what": "Chat at 10am", "class": "theirs", "date_phrase": "10am"}
    first = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_001", made_on=_NOW,
        resolved_date=datetime(2025, 2, 7, 23, 59, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )
    second = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_002", made_on=_NOW,
        resolved_date=datetime(2025, 2, 8, 0, 1, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )

    assert first != second


def test_resolve_commitment_same_text_repeated_across_three_emails_stays_one_commitment(db):
    raw = {"what": "Chat at 10am", "class": "theirs", "date_phrase": "10am"}
    ids = [
        resolve_commitment(
            db, thread_id="thread_1", raw=raw, message_id=f"msg_{i}", made_on=_NOW,
            resolved_date=datetime(2025, 2, 7, 10 + i, 0, tzinfo=timezone.utc), date_type="inferred",
            goal_pillar="Sales", project_id=None,
        )
        for i in range(3)
    ]

    assert len(set(ids)) == 1
    assert len(CommitmentRepository(db).all_for_thread("thread_1")) == 1


def test_resolve_commitment_similar_but_different_text_stays_separate(db):
    first = resolve_commitment(
        db, thread_id="thread_1", raw={"what": "Send the proposal", "class": "mine", "date_phrase": None},
        message_id="msg_001", made_on=_NOW, resolved_date=None, date_type=None,
        goal_pillar="Sales", project_id=None,
    )
    second = resolve_commitment(
        db, thread_id="thread_1", raw={"what": "Send the pricing sheet", "class": "mine", "date_phrase": None},
        message_id="msg_002", made_on=_NOW, resolved_date=None, date_type=None,
        goal_pillar="Sales", project_id=None,
    )

    assert first != second


def test_reused_commitment_does_not_create_a_duplicate_follow_up(db):
    raw = {"what": "Chat at 10am", "class": "theirs", "date_phrase": "10am"}
    first_commitment = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_001", made_on=_NOW,
        resolved_date=datetime(2025, 2, 7, 14, 0, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )
    first_follow_up = derive_follow_up(db, commitment_id=first_commitment, thread_id=None)

    second_commitment = resolve_commitment(
        db, thread_id="thread_1", raw=raw, message_id="msg_002", made_on=_NOW,
        resolved_date=datetime(2025, 2, 7, 16, 0, tzinfo=timezone.utc), date_type="inferred",
        goal_pillar="Sales", project_id=None,
    )
    second_follow_up = derive_follow_up(db, commitment_id=second_commitment, thread_id=None)

    assert first_commitment == second_commitment
    assert first_follow_up == second_follow_up
    assert len(FollowUpRepository(db).find_many({})) == 1
    stored = FollowUpRepository(db).find_one({"id": first_follow_up})
    assert stored["commitment_id"] == first_commitment


def test_resolve_meeting_deduplicates_on_thread_and_date(db):
    date = datetime(2026, 9, 20, tzinfo=timezone.utc)
    first = resolve_meeting(
        db, thread_id="thread_1", date=date, raw={"attendees": [], "actions_raised": []}, actionable=True
    )
    second = resolve_meeting(
        db, thread_id="thread_1", date=date, raw={"attendees": [], "actions_raised": []}, actionable=True
    )

    assert first == second
    assert len(MeetingRepository(db).all_for_thread("thread_1")) == 1


def test_resolve_meeting_stores_actionable_flag(db):
    meeting_id = resolve_meeting(
        db, thread_id="thread_2", date=None, raw={"attendees": [], "actions_raised": []}, actionable=False
    )
    stored = MeetingRepository(db).find_one({"id": meeting_id})
    assert stored["actionable"] is False


def test_resolve_personal_item_deduplicates_on_sender_and_description(db):
    raw = {"item_type": "reminder", "description": "Renew passport"}
    first = resolve_personal_item(db, sender_email="jane@example.com", raw=raw, resolved_date=None)
    second = resolve_personal_item(db, sender_email="Jane@Example.com", raw={"item_type": "reminder", "description": "renew passport"}, resolved_date=None)

    assert first == second
    assert len(PersonalItemRepository(db).find_many({})) == 1


def test_derive_follow_up_from_commitment_is_idempotent(db):
    first = derive_follow_up(db, commitment_id="COM-001", thread_id=None)
    second = derive_follow_up(db, commitment_id="COM-001", thread_id=None)

    assert first == second
    stored = FollowUpRepository(db).find_one({"id": first})
    assert stored["commitment_id"] == "COM-001"
    assert stored["thread_id"] is None


def test_derive_follow_up_from_commitment_carries_the_given_thread_id(db):
    # Regression: thread_id was silently dropped here even when the caller passed one --
    # a FollowUp derived from a Commitment must carry the commitment's own thread_id
    # (both commitment_id and thread_id set) so it's directly queryable by thread.
    follow_up_id = derive_follow_up(db, commitment_id="COM-001", thread_id="thread_1")
    stored = FollowUpRepository(db).find_one({"id": follow_up_id})
    assert stored["commitment_id"] == "COM-001"
    assert stored["thread_id"] == "thread_1"


def test_derive_follow_up_from_thread_when_no_commitment(db):
    follow_up_id = derive_follow_up(db, commitment_id=None, thread_id="thread_1")
    stored = FollowUpRepository(db).find_one({"id": follow_up_id})
    assert stored["thread_id"] == "thread_1"
    assert stored["commitment_id"] is None


# --- Global canonical identity: Part 1 (person tier 2) / Part 4 (organization) -------


def _seed_anchor(db, thread_id="thread_anchor"):
    """PER-391-shaped fixture: an existing, email-anchored Person at DataBeat."""
    return resolve_person(
        db,
        {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True,
        now=_NOW,
        thread_id=thread_id,
    )


def test_no_email_short_name_plus_matching_company_reuses_email_anchored_person(db):
    # TEST 1: "Ashok" / DataBeat / no email -> PER-391 reused.
    anchor_id = _seed_anchor(db)

    reused_id = resolve_person(
        db, {"name": "Ashok", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="thread_other"
    )

    assert reused_id == anchor_id
    people = PersonRepository(db).find_many({})
    assert len(people) == 1
    stored = people[0]
    assert "thread_other" in stored["open_threads"]
    assert "Ashok" in stored["aliases"]


def test_no_email_bare_nickname_is_not_sufficient_evidence_alone(db):
    # TEST 2: "Ash" / DataBeat / no email -> NOT reused (no alias/token match yet) --
    # a bare 3-letter nickname is not a whole-word match against "Ashok Ganapam", so it
    # falls through to the existing thread-scoped-only path and creates its own
    # (flagged) record instead of guessing.
    anchor_id = _seed_anchor(db)

    new_id = resolve_person(
        db, {"name": "Ash", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="thread_other"
    )

    assert new_id != anchor_id
    people = PersonRepository(db).find_many({})
    assert len(people) == 2
    ash_record = next(p for p in people if p["id"] == new_id)
    assert ash_record["review_flag"] is True


def test_once_a_short_form_is_a_recorded_alias_a_repeat_mention_is_recognized(db):
    # Evidence-gated alias handling (Part 6): "Ash" alone is never sufficient evidence
    # on its own sighting (see the bare-nickname test above) -- but once it has itself
    # been recorded as a confirmed alias (via a match strong enough to justify that),
    # a LATER repeat of that exact same short form is now recognized. This never
    # happens on the first sighting, only once real evidence already exists.
    anchor_id = _seed_anchor(db)
    first_ash_id = resolve_person(
        db, {"name": "Ash", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="thread_a"
    )
    # First sighting: not sufficient evidence, matches TEST 2 -- a new record, not
    # anchor_id, and "Ash" is not yet an alias of anyone.
    assert first_ash_id != anchor_id

    # A stronger mention ("Ashok Ganapam", full name) now records "Ashok Ganapam" as an
    # alias-equivalent match (it matches directly via tier 2's name-token check, not via
    # aliases) -- aliases only grow when the matched mention's exact string differs from
    # the canonical name, so this itself doesn't create an "Ash" alias either. To get
    # real "Ash" evidence on file, a human/administrative process would need to record
    # it explicitly (out of scope here) -- simulate that evidence existing already:
    PersonRepository(db).upsert_by_key(
        {"id": anchor_id}, {**PersonRepository(db).find_one({"id": anchor_id}), "aliases": ["Ash"]}
    )

    reused_id = resolve_person(
        db, {"name": "Ash", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="thread_b"
    )
    assert reused_id == anchor_id


def test_no_email_same_name_different_company_does_not_reuse(db):
    # TEST 3: "Ashok" at a DIFFERENT company -> PER-391 (DataBeat) must not be reused.
    anchor_id = _seed_anchor(db)

    other_id = resolve_person(
        db,
        {"name": "Ashok", "email": None, "org": "Acme Corp"},
        is_sender=None,
        now=_NOW,
        thread_id="thread_other",
    )

    assert other_id != anchor_id
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_no_email_no_company_never_auto_merges_on_name_alone(db):
    # TEST 4: "Ashok" with no company and no email at all -> tier 2 can never fire
    # (org is required), so this must not silently reuse PER-391 either.
    anchor_id = _seed_anchor(db)

    other_id = resolve_person(
        db, {"name": "Ashok", "email": None, "org": None}, is_sender=None, now=_NOW, thread_id="thread_other"
    )

    assert other_id != anchor_id


def test_same_name_different_company_people_remain_permanently_separate(db):
    # TEST 5: two different real people, same first name, different real companies --
    # both get their own email-anchored canonical identity and must never collapse
    # into one record via the no-email tier either.
    person_a = resolve_person(
        db, {"name": "Jordan", "email": "jordan@alpha.example", "org": "Alpha"},
        is_sender=True, now=_NOW, thread_id="t1",
    )
    person_b = resolve_person(
        db, {"name": "Jordan", "email": "jordan@beta.example", "org": "Beta"},
        is_sender=True, now=_NOW, thread_id="t2",
    )
    assert person_a != person_b

    reused_a = resolve_person(
        db, {"name": "Jordan", "email": None, "org": "Alpha"}, is_sender=None, now=_NOW, thread_id="t3"
    )
    reused_b = resolve_person(
        db, {"name": "Jordan", "email": None, "org": "Beta"}, is_sender=None, now=_NOW, thread_id="t4"
    )
    assert reused_a == person_a
    assert reused_b == person_b
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_same_person_different_name_forms_across_threads_all_resolve_to_one_id(db):
    # TEST 6: "Ashok" then "Ashok Ganapam" then "Ashok" again, across three different
    # threads, all resolve to the same canonical PER-xxx (each form is a whole-word
    # subset/superset match against the anchor's own name -- strong evidence, no
    # nickname guessing required).
    anchor_id = _seed_anchor(db)

    id_a = resolve_person(
        db, {"name": "Ashok", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="t_a"
    )
    id_b = resolve_person(
        db, {"name": "Ashok Ganapam", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="t_b"
    )
    id_c = resolve_person(
        db, {"name": "Ashok", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="t_c"
    )

    assert id_a == anchor_id
    assert id_b == anchor_id
    assert id_c == anchor_id
    assert PersonRepository(db).find_many({}).__len__() == 1
    stored = PersonRepository(db).find_one({"id": anchor_id})
    assert {"t_a", "t_b", "t_c", "thread_anchor"} <= set(stored["open_threads"])


def test_ambiguous_tier_2_match_never_guesses(db):
    # Two different email-anchored people with the same company AND a name-compatible
    # candidate name is genuinely ambiguous -- must fall through to creating a new
    # flagged record rather than guessing which one a bare mention means.
    resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t1",
    )
    resolve_person(
        db, {"name": "Ashok Kumar", "email": "ashok.kumar@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t2",
    )

    ambiguous_id = resolve_person(
        db, {"name": "Ashok", "email": None, "org": "DataBeat"}, is_sender=None, now=_NOW, thread_id="t3"
    )

    people = PersonRepository(db).find_many({})
    assert len(people) == 3
    new_record = next(p for p in people if p["id"] == ambiguous_id)
    assert new_record["review_flag"] is True


def test_resolve_organization_creates_one_canonical_org_per_domain(db):
    # TEST 8: a company is consistently resolved to one canonical organization ID,
    # regardless of how the display name varies across mentions -- domain is the
    # identity signal, not the free-text name.
    org_a = resolve_organization(db, "ashok@databeat.io", name_hint="DataBeat")
    org_b = resolve_organization(db, "someone@databeat.io", name_hint="DataBeat Inc.")
    org_c = resolve_organization(db, "other@differentcompany.example", name_hint="Different Co")

    assert org_a == org_b
    assert org_a != org_c
    assert OrganizationRepository(db).find_many({}).__len__() == 2


def test_resolve_organization_returns_none_without_a_domain(db):
    # Company name alone is never sufficient to establish a canonical organization.
    assert resolve_organization(db, None, name_hint="DataBeat") is None
    assert resolve_organization(db, "not-an-email", name_hint="DataBeat") is None
    assert OrganizationRepository(db).find_many({}).__len__() == 0


def test_resolve_person_sets_org_id_from_email_domain(db):
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t1",
    )
    stored = PersonRepository(db).find_one({"id": person_id})
    assert stored["org_id"] is not None
    org = OrganizationRepository(db).find_one({"id": stored["org_id"]})
    assert org["domain"] == "databeat.io"


def test_resolve_person_backfills_org_id_on_reuse_of_a_pre_existing_record(db):
    # A Person resolved before org_id existed (org_id missing from the stored
    # document -- simulated here directly) gets it backfilled the next time future
    # ingestion resolves the same email, exactly like last_inbound/open_threads
    # already are -- this is ordinary incremental enrichment, not a historical
    # migration pass.
    person_id = resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t1",
    )
    repo = PersonRepository(db)
    # upsert_by_key uses $set, which only sets keys present in the given document -- it
    # never unsets a field missing from it. Simulating "a document from before org_id
    # existed" requires an actual $unset against the raw collection, not a Python dict
    # rebuild through the repository's own normal write path.
    db.people.update_one({"id": person_id}, {"$unset": {"org_id": ""}})
    assert repo.find_one({"id": person_id}).get("org_id") is None

    resolve_person(
        db, {"name": "Ashok Ganapam", "email": "ashok@databeat.io", "org": "DataBeat"},
        is_sender=True, now=_NOW, thread_id="t2",
    )

    assert repo.find_one({"id": person_id})["org_id"] is not None


# --- Phase 19.1: identity resolution must be future-safe against merged Persons -----------
#
# These tests never hard-code any current Atlas id/name -- they always build their own
# fresh, synthetic active/merged Person pair via resolve_person + a direct status/
# merged_into mutation (exactly how a real consolidation leaves data), matching Part 5's
# "must work for PER-900/901/902 tomorrow" requirement.


def _retire(db, person_id: str, merged_into: str) -> None:
    """Test helper mirroring exactly what app.duplicate_consolidation retires a
    duplicate with -- status='merged' + merged_into, nothing else changed."""
    person = PersonRepository(db).find_one({"id": person_id})
    PersonRepository(db).upsert_by_key({"id": person_id}, {**person, "status": "merged", "merged_into": merged_into})


def test_A_active_person_with_matching_email_resolves_normally(db):
    first = resolve_person(db, {"name": "Rita Cole", "email": "rita@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    second = resolve_person(db, {"name": "Rita Cole", "email": "rita@example.com"}, is_sender=True, now=_NOW, thread_id="t2")

    assert first == second
    assert PersonRepository(db).find_many({}).__len__() == 1


def test_B_merged_person_exact_email_match_resolves_to_canonical(db):
    canonical_id = resolve_person(db, {"name": "Dana Park", "email": "dana@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    duplicate_id = resolve_person(db, {"name": "Dana", "email": "dana.alt@example.com"}, is_sender=True, now=_NOW, thread_id="t2")
    _retire(db, duplicate_id, canonical_id)

    resolved = resolve_person(db, {"name": "Dana", "email": "dana.alt@example.com"}, is_sender=True, now=_NOW, thread_id="t3")

    assert resolved == canonical_id
    # No third Person was created -- the merged record's own email still exact-matches,
    # and the redirect happens instead of treating it as a fresh identity.
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_B2_merged_thread_scoped_no_email_person_resolves_to_canonical(db):
    canonical_id = resolve_person(db, {"name": "Ashok Ganapam", "email": "ashok@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    duplicate_id = resolve_person(db, {"name": "Ashok Ganapam", "email": None}, is_sender=None, now=_NOW, thread_id="t2")
    _retire(db, duplicate_id, canonical_id)

    # A later mention in the SAME thread, same name, no email -- the thread-scoped
    # tier would otherwise reuse the (now-merged) duplicate record directly.
    resolved = resolve_person(db, {"name": "Ashok Ganapam", "email": None}, is_sender=None, now=_NOW, thread_id="t2")

    assert resolved == canonical_id
    assert PersonRepository(db).find_many({}).__len__() == 2


def test_C_missing_merged_into_target_raises_controlled_failure(db):
    from app.entities.lifecycle import CanonicalResolutionError

    duplicate_id = resolve_person(db, {"name": "No Target", "email": "notarget@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    person = PersonRepository(db).find_one({"id": duplicate_id})
    PersonRepository(db).upsert_by_key({"id": duplicate_id}, {**person, "status": "merged", "merged_into": None})

    with pytest.raises(CanonicalResolutionError, match="no merged_into target"):
        resolve_person(db, {"name": "No Target", "email": "notarget@example.com"}, is_sender=True, now=_NOW, thread_id="t2")


def test_D_self_referencing_merged_into_raises_controlled_failure(db):
    from app.entities.lifecycle import CanonicalResolutionError

    duplicate_id = resolve_person(db, {"name": "Self Ref", "email": "selfref@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    _retire(db, duplicate_id, duplicate_id)

    with pytest.raises(CanonicalResolutionError, match="pointing at itself"):
        resolve_person(db, {"name": "Self Ref", "email": "selfref@example.com"}, is_sender=True, now=_NOW, thread_id="t2")


def test_E_circular_merged_into_chain_raises_controlled_failure(db):
    from app.entities.lifecycle import CanonicalResolutionError

    a = resolve_person(db, {"name": "Loop A", "email": "loopa@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    b = resolve_person(db, {"name": "Loop B", "email": "loopb@example.com"}, is_sender=True, now=_NOW, thread_id="t2")
    _retire(db, a, b)
    _retire(db, b, a)

    with pytest.raises(CanonicalResolutionError, match="circular"):
        resolve_person(db, {"name": "Loop A", "email": "loopa@example.com"}, is_sender=True, now=_NOW, thread_id="t3")


def test_F_valid_multi_hop_chain_resolves_safely(db):
    final = resolve_person(db, {"name": "Final Target", "email": "final@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    middle = resolve_person(db, {"name": "Middle Hop", "email": "middle@example.com"}, is_sender=True, now=_NOW, thread_id="t2")
    start = resolve_person(db, {"name": "Start Hop", "email": "start@example.com"}, is_sender=True, now=_NOW, thread_id="t3")
    _retire(db, middle, final)
    _retire(db, start, middle)

    resolved = resolve_person(db, {"name": "Start Hop", "email": "start@example.com"}, is_sender=True, now=_NOW, thread_id="t4")

    assert resolved == final


def test_G_resolution_never_creates_new_person_when_first_match_is_merged(db):
    canonical_id = resolve_person(db, {"name": "Group G", "email": "groupg@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    duplicate_id = resolve_person(db, {"name": "Group G Dup", "email": "groupg.dup@example.com"}, is_sender=True, now=_NOW, thread_id="t2")
    _retire(db, duplicate_id, canonical_id)
    before_count = PersonRepository(db).find_many({}).__len__()

    resolve_person(db, {"name": "Group G Dup", "email": "groupg.dup@example.com"}, is_sender=True, now=_NOW, thread_id="t3")
    resolve_person(db, {"name": "Group G Dup", "email": "groupg.dup@example.com"}, is_sender=True, now=_NOW, thread_id="t4")

    assert PersonRepository(db).find_many({}).__len__() == before_count


def test_H_name_only_matching_remains_conservative_even_near_a_merged_person(db):
    # Two DIFFERENT active people share the same short first name across unrelated
    # threads -- name-alone must still never establish identity, merge status aside.
    resolve_person(db, {"name": "Sam", "email": "sam1@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    resolve_person(db, {"name": "Sam", "email": "sam2@example.com"}, is_sender=True, now=_NOW, thread_id="t2")

    orphan_id = resolve_person(db, {"name": "Sam", "email": None, "org": None}, is_sender=None, now=_NOW, thread_id="t3")

    # A brand-new, unrelated thread with no org and no shared thread evidence must
    # create its own record, never guess between the two "Sam"s.
    stored = PersonRepository(db).find_one({"id": orphan_id})
    assert stored["review_flag"] is True
    assert PersonRepository(db).find_many({}).__len__() == 3


def test_I_email_first_hierarchy_unchanged_by_lifecycle_awareness(db):
    # Exact email match still wins outright, with no lifecycle involvement at all,
    # for two ordinary active Persons.
    first = resolve_person(db, {"name": "Hier One", "email": "hier@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    second = resolve_person(db, {"name": "Hier One Alt Name", "email": "HIER@Example.com"}, is_sender=False, now=_NOW, thread_id="t2")

    assert first == second
    assert PersonRepository(db).find_many({}).__len__() == 1


# --- Phase 19.1 Part 18: future-ingestion simulation (never writes to real Atlas -- ------
# these are synthetic mongomock fixtures only) --------------------------------------------


def test_ingestion_simulation_active_identity_resolves_to_existing_person(db):
    existing = resolve_person(db, {"name": "Active Person", "email": "active@example.com"}, is_sender=True, now=_NOW, thread_id="t1")

    resolved = resolve_person(db, {"name": "Active Person", "email": "active@example.com"}, is_sender=True, now=_NOW, thread_id="t2")

    assert resolved == existing


def test_ingestion_simulation_merged_identity_resolves_to_canonical_target(db):
    canonical_id = resolve_person(db, {"name": "Canonical Person", "email": "canonical@example.com"}, is_sender=True, now=_NOW, thread_id="t1")
    duplicate_id = resolve_person(db, {"name": "Canonical", "email": None}, is_sender=None, now=_NOW, thread_id="t2")
    _retire(db, duplicate_id, canonical_id)

    resolved = resolve_person(db, {"name": "Canonical", "email": None}, is_sender=None, now=_NOW, thread_id="t2")

    assert resolved == canonical_id


def test_ingestion_simulation_genuinely_new_identity_creates_new_person(db):
    before = PersonRepository(db).find_many({}).__len__()

    new_id = resolve_person(db, {"name": "Brand New", "email": "brandnew@example.com"}, is_sender=True, now=_NOW, thread_id="t1")

    after = PersonRepository(db).find_many({}).__len__()
    assert after == before + 1
    assert PersonRepository(db).find_one({"id": new_id})["email"] == "brandnew@example.com"


def test_ingestion_simulation_ambiguous_identity_takes_review_path(db):
    # Two name-and-org-matching anchors with no thread overlap -- ambiguous, so the
    # no-email mention must never guess and instead becomes its own flagged record.
    resolve_person(db, {"name": "Ambi Guous", "email": "ambi1@shared.com", "org": "Shared Co"}, is_sender=True, now=_NOW, thread_id="t1")
    resolve_person(db, {"name": "Ambi Guous", "email": "ambi2@shared.com", "org": "Shared Co"}, is_sender=True, now=_NOW, thread_id="t2")

    new_id = resolve_person(db, {"name": "Ambi Guous", "email": None, "org": "Shared Co"}, is_sender=None, now=_NOW, thread_id="t3")

    stored = PersonRepository(db).find_one({"id": new_id})
    assert stored["review_flag"] is True
