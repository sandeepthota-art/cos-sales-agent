"""Tests for Milestone 2: app.entities.person_story (versioned person-story
history) and its integration into app.mcp.tools.persist_person_profile.

No transactions are used anywhere (mongomock raises NotImplementedError for
sessions -- confirmed directly, see app/entities/person_story.py's module
docstring). Consistency is instead tested via: pending/committed state,
write ordering, and the race-retry path.
"""
import logging
from datetime import datetime, timedelta, timezone

import mongomock
import pytest
from pymongo.errors import DuplicateKeyError

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    PersonContextSnapshotRepository,
    PersonRepository,
    PersonStoryVersionRepository,
)
from app.entities.person_story import persist_story_version
from app.mcp import tools


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _make_person(db, person_id="PER-001", **overrides):
    person = {
        "id": person_id, "name": "Khyati Mehra", "email": f"{person_id.lower()}@example.com",
        "aliases": [], "org": "Google", "org_id": "ORG-001", "status": "active",
        "profile_summary": None, "recent_context": None, "key_topics": [],
        "profile_updated_at": None,
    }
    person.update(overrides)
    PersonRepository(db).upsert_by_key({"id": person_id}, person)
    return person


def _add_snapshot(db, person_id, source_email_id, thread_id="THR-001"):
    PersonContextSnapshotRepository(db).upsert_by_key(
        {"person_id": person_id, "source_email_id": source_email_id},
        {
            "id": f"PCS-{source_email_id}", "person_id": person_id, "thread_id": thread_id,
            "source_email_id": source_email_id, "created_at": datetime.now(timezone.utc).isoformat(),
            "entries": [],
        },
    )


# --- 1-3: sequential version creation ------------------------------------------------------


def test_first_profile_creates_version_1(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = persist_story_version(db, "PER-001", "Khyati leads Google's developer ecosystem.", ["EML-001"])

    # Finding #5: attempt_id is a fresh, deterministic ATT- id (this is the
    # first ATT-/PSV- allocation in this fresh db) -- asserted explicitly
    # rather than loosened away, so the full return contract stays pinned.
    assert result == {
        "version_created": True, "version": 1,
        "change_reason": "First version (1 source email(s)).", "attempt_id": "ATT-001",
    }


def test_meaningful_update_creates_version_2(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Khyati leads Google's developer ecosystem.", ["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    result = persist_story_version(db, "PER-001", "Khyati now also runs the AI Studio community.", ["EML-001", "EML-002"])

    assert result["version_created"] is True
    assert result["version"] == 2
    assert "EML-002" in result["change_reason"]


def test_third_meaningful_update_creates_version_3(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")
    persist_story_version(db, "PER-001", "Summary v2.", ["EML-001", "EML-002"])
    _add_snapshot(db, "PER-001", "EML-003")

    result = persist_story_version(db, "PER-001", "Summary v3.", ["EML-001", "EML-002", "EML-003"])

    assert result["version"] == 3


# --- 4: previous committed versions remain unchanged ----------------------------------------


def test_previous_versions_remain_unchanged(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")
    persist_story_version(db, "PER-001", "Summary v2.", ["EML-001", "EML-002"])

    versions = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")

    assert len(versions) == 2
    v1 = next(v for v in versions if v["version"] == 1)
    v2 = next(v for v in versions if v["version"] == 2)
    assert v1["summary"] == "Summary v1."
    assert v1["superseded"] is True
    assert v2["summary"] == "Summary v2."
    assert v2["superseded"] is False


# --- 5-6: meaningful-change detection --------------------------------------------------------


def test_identical_resubmission_does_not_create_duplicate_version(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])

    result = persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])

    assert result["version_created"] is False
    assert result["version"] == 1
    assert len(PersonStoryVersionRepository(db).all_committed_for_person("PER-001")) == 1


def test_new_evidence_with_unchanged_text_does_not_create_version(db):
    """A new email that adds nothing to the narrative (Claude resubmits the
    exact same summary text) must not version -- both new evidence AND
    changed text are required."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    result = persist_story_version(db, "PER-001", "Summary v1.", ["EML-001", "EML-002"])

    assert result["version_created"] is False
    assert len(PersonStoryVersionRepository(db).all_committed_for_person("PER-001")) == 1


def test_changed_text_with_no_new_evidence_does_not_create_version(db):
    """Rewording without any new source email is not meaningful either."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Summary v1.", ["EML-001"])

    result = persist_story_version(db, "PER-001", "A differently-worded v1 summary.", ["EML-001"])

    assert result["version_created"] is False


# --- 7: dedup of source email/snapshot IDs ---------------------------------------------------


def test_source_email_ids_are_deduplicated(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = persist_story_version(db, "PER-001", "Summary.", ["EML-001", "EML-001", "EML-001"])

    stored = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")[0]
    assert stored["source_email_ids"] == ["EML-001"]
    assert stored["source_snapshot_ids"] == ["PCS-EML-001"]


# --- 8: missing source IDs handled safely ----------------------------------------------------


def test_missing_snapshot_for_an_email_id_is_excluded_not_invented(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    # EML-999 has no corresponding snapshot at all.

    persist_story_version(db, "PER-001", "Summary.", ["EML-001", "EML-999"])

    stored = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")[0]
    assert stored["source_email_ids"] == ["EML-001", "EML-999"]
    assert stored["source_snapshot_ids"] == ["PCS-EML-001"]  # EML-999 silently excluded, never invented


def test_no_source_email_ids_still_creates_first_version(db):
    _make_person(db)

    result = persist_story_version(db, "PER-001", "Summary with no evidence list supplied.", None)

    assert result["version_created"] is True
    assert result["version"] == 1
    stored = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")[0]
    assert stored["source_email_ids"] == []
    assert stored["source_snapshot_ids"] == []


# --- 9: existing profiles without history preserved ------------------------------------------


def test_existing_profile_without_history_is_preserved(db):
    """A person who already has profile_summary from before Milestone 2
    existed must not have a fabricated version 1 invented for them merely
    by reading their record -- only an actual persist_story_version call
    establishes history."""
    _make_person(db, profile_summary="Pre-Milestone-2 profile text.")

    assert PersonStoryVersionRepository(db).all_committed_for_person("PER-001") == []
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Pre-Milestone-2 profile text."


def test_first_versioned_update_establishes_history_for_a_pre_existing_profile(db):
    _make_person(db, profile_summary="Pre-Milestone-2 profile text.")
    _add_snapshot(db, "PER-001", "EML-001")

    result = persist_story_version(db, "PER-001", "Updated profile with real versioning.", ["EML-001"])

    assert result["version_created"] is True
    assert result["version"] == 1


# --- 10: concurrent updates / race handling ---------------------------------------------------


def test_race_retries_by_rereading_fresh_state_not_just_bumping_the_counter(db, monkeypatch):
    """Finding #4 fix regression. A genuine concurrent race: another writer's
    call wins version 1 and fully commits it for real -- in the database --
    in the window between this call's first (rejected) insert attempt and
    its retry. The retry must re-read that real committed state and re-run
    the meaningful-change decision against it, landing on version 2 only
    because the FRESH state still renders this update meaningful; a retry
    that merely bumped a local counter without re-reading (the pre-fix bug)
    would not know the real current summary/evidence had changed at all."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    _add_snapshot(db, "PER-001", "EML-002")

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    version_repo = PersonStoryVersionRepository(db)
    calls: list[int] = []

    def competing_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            # A genuine competing writer wins version 1 for real, committed
            # in the DB, before our own retry re-reads state.
            real_insert_pending(
                version_repo,
                {
                    "id": "PSV-COMPETITOR-1", "person_id": "PER-001", "version": 1,
                    "summary": "Competing writer's version.", "source_email_ids": ["EML-001"],
                    "source_snapshot_ids": ["PCS-EML-001"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "change_reason": "First version.", "superseded": False, "commit_status": "pending",
                },
            )
            version_repo.set_commit_status("PER-001", 1, "committed")
            raise DuplicateKeyError("E11000 simulated real race on version 1")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", competing_insert_pending)

    result = persist_story_version(db, "PER-001", "My own summary.", ["EML-002"])

    assert result["version_created"] is True
    assert result["version"] == 2
    assert calls == [1, 2]
    committed = {v["version"]: v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001")}
    assert committed[1]["summary"] == "Competing writer's version."
    assert committed[1]["superseded"] is True
    assert committed[2]["summary"] == "My own summary."
    assert committed[2]["superseded"] is False


def test_race_retry_becomes_a_no_op_when_the_competing_commit_already_covers_it(db, monkeypatch):
    """Finding #4 fix regression. If, by the time of the retry, a competing
    writer already committed the exact same (text, evidence) this call was
    trying to add, re-reading must recognize the update is no longer
    meaningful and return a no-new-version result -- never force a duplicate
    version into existence purely because the original (now-stale) decision
    said it was meaningful."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    version_repo = PersonStoryVersionRepository(db)
    calls: list[int] = []

    def competing_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            real_insert_pending(
                version_repo,
                {
                    "id": "PSV-COMPETITOR-1", "person_id": "PER-001", "version": 1,
                    "summary": "Converged summary.", "source_email_ids": ["EML-001"],
                    "source_snapshot_ids": ["PCS-EML-001"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "change_reason": "First version.", "superseded": False, "commit_status": "pending",
                },
            )
            version_repo.set_commit_status("PER-001", 1, "committed")
            raise DuplicateKeyError("E11000 simulated real race on version 1")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", competing_insert_pending)

    result = persist_story_version(db, "PER-001", "Converged summary.", ["EML-001"])

    assert result["version_created"] is False
    assert result["version"] == 1
    assert result["change_reason"] is None
    assert calls == [1]  # recognized as a no-op on re-evaluation -- no second insert attempted
    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1  # no duplicate version created


def test_repeated_conflicts_exhaust_bounded_retries_and_raise_explicitly(db, monkeypatch):
    """Persistent contention across every retry attempt must stay bounded
    and be surfaced explicitly -- never retried forever, and never silently
    swallowed into a false 'no version created' result."""
    _make_person(db)

    def always_conflicting_insert_pending(self, document):
        raise DuplicateKeyError("E11000 simulated persistent conflict")

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", always_conflicting_insert_pending)

    with pytest.raises(DuplicateKeyError):
        persist_story_version(db, "PER-001", "My own summary.", None)


def test_unique_index_rejects_duplicate_person_version_at_db_level(db):
    doc_a = {
        "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "a",
        "source_email_ids": [], "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
        "change_reason": None, "superseded": False, "commit_status": "committed",
    }
    doc_b = {**doc_a, "id": "PSV-002", "summary": "b"}
    db.person_story_versions.insert_one(doc_a)

    with pytest.raises(DuplicateKeyError):
        db.person_story_versions.insert_one(doc_b)


# --- 11: failure recovery ----------------------------------------------------------------------


def test_pending_row_with_matching_attempt_id_is_promoted_even_when_text_no_longer_matches(db):
    """Finding #5 fix: a genuinely interrupted write, safely reconciled.
    Promotion is now proven by attempt_id, never by text -- the Person's
    profile_story_attempt_id already points at this pending row's attempt_id
    (simulating a crash after that link was recorded), so it is promoted
    even though its text does NOT match the Person's current
    profile_summary, proving the decision no longer depends on text
    comparison at all."""
    _make_person(db, profile_summary="Some unrelated text.", profile_story_attempt_id="ATT-001")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Interrupted summary.",
            "source_email_ids": ["EML-001"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
            "attempt_id": "ATT-001",
        }
    )

    persist_story_version(db, "PER-001", "Some unrelated text.", None)

    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1
    assert committed[0]["version"] == 1
    assert committed[0]["summary"] == "Interrupted summary."  # promoted as-is, never rewritten
    assert PersonStoryVersionRepository(db).pending_for_person("PER-001") == []


def test_pending_row_with_matching_text_but_unlinked_attempt_id_is_never_promoted(db):
    """THE Finding #5 regression: a pending row whose text happens to equal
    the Person's current profile_summary, but whose attempt_id does NOT
    match the Person's recorded attempt_id, must never be promoted on text
    alone. Being fresh (not stale) and unlinked, it is left untouched rather
    than guessed about -- which means it still occupies its version slot, so
    a concurrent attempt to create a version collides and (Finding #4)
    exhausts its bounded retries, a known and acceptable residual cost of
    never promoting on coincidence."""
    _make_person(db, profile_summary="Coincidentally identical text.", profile_story_attempt_id="ATT-999")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1,
            "summary": "Coincidentally identical text.",
            "source_email_ids": ["EML-001"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),  # fresh, not stale
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
            "attempt_id": "ATT-001",  # does not match person's profile_story_attempt_id
        }
    )

    with pytest.raises(DuplicateKeyError):
        persist_story_version(db, "PER-001", "Coincidentally identical text.", ["EML-001"])

    pending = PersonStoryVersionRepository(db).pending_for_person("PER-001")
    assert len(pending) == 1
    assert pending[0]["commit_status"] == "pending"
    assert pending[0]["attempt_id"] == "ATT-001"  # untouched, never silently promoted


def test_legacy_pending_row_without_attempt_id_is_never_promoted_but_reclaimed_once_stale(db):
    """A pending row from before this field existed carries no attempt_id at
    all -- it can never be promoted (no causal proof exists, and none is
    invented), but once old enough to be confidently abandoned it must still
    be reclaimed so it doesn't block this person's version slot forever."""
    _make_person(db, profile_summary="Legacy abandoned text.")
    stale_created_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Legacy abandoned text.",
            "source_email_ids": [], "source_snapshot_ids": [], "created_at": stale_created_at,
            "change_reason": None, "superseded": False, "commit_status": "pending",
            # no "attempt_id" key at all -- predates this field.
        }
    )

    result = persist_story_version(db, "PER-001", "The real first version.", None)

    assert result["version_created"] is True
    assert result["version"] == 1
    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1
    assert committed[0]["summary"] == "The real first version."
    assert PersonStoryVersionRepository(db).pending_for_person("PER-001") == []


def test_legacy_pending_row_without_attempt_id_is_left_alone_while_still_fresh(db):
    """The staleness grace period applies even to a legacy (no attempt_id)
    row -- a very recently created one must not be deleted just because it
    lacks the field, since it could still belong to an active writer."""
    _make_person(db, profile_summary="Legacy text.")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Legacy text.",
            "source_email_ids": [], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),  # fresh
            "change_reason": None, "superseded": False, "commit_status": "pending",
        }
    )

    with pytest.raises(DuplicateKeyError):
        persist_story_version(db, "PER-001", "A different attempt's text.", None)

    pending = PersonStoryVersionRepository(db).pending_for_person("PER-001")
    assert len(pending) == 1  # left untouched, not deleted just for lacking attempt_id


def test_promoted_pending_row_preserves_its_original_evidence_fields_exactly(db):
    """Preservation requirement: promotion must never recompute or alter
    source_email_ids/source_snapshot_ids -- only commit_status changes."""
    _make_person(db, profile_summary="Will differ.", profile_story_attempt_id="ATT-001")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Interrupted summary.",
            "source_email_ids": ["EML-OLD-1", "EML-OLD-2"], "source_snapshot_ids": ["PCS-OLD-1"],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
            "attempt_id": "ATT-001",
        }
    )

    persist_story_version(db, "PER-001", "A completely different call's own text.", ["EML-NEW"])

    promoted = next(
        v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001") if v["version"] == 1
    )
    assert promoted["summary"] == "Interrupted summary."
    assert promoted["source_email_ids"] == ["EML-OLD-1", "EML-OLD-2"]
    assert promoted["source_snapshot_ids"] == ["PCS-OLD-1"]


def test_reconciliation_promotion_is_idempotent_across_repeated_calls(db):
    _make_person(db, profile_summary="Will match.", profile_story_attempt_id="ATT-001")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Will match.",
            "source_email_ids": ["EML-001"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
            "attempt_id": "ATT-001",
        }
    )

    first = persist_story_version(db, "PER-001", "Will match.", ["EML-001"])
    second = persist_story_version(db, "PER-001", "Will match.", ["EML-001"])

    assert first["version_created"] is False  # promoted row already satisfies this call -- not meaningful
    assert first["version"] == 1
    assert second["version_created"] is False
    assert second["version"] == 1
    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1  # no duplicate, no re-promotion artifact


def test_concurrent_writers_well_behaved_completion_leaves_no_pending_debris(db, monkeypatch):
    """Combines Finding #4 and Finding #5: a real competing writer completes
    its own full pending -> committed cycle (carrying its own attempt_id)
    while our call is retrying. Our own reconciliation pass and retry must
    not be confused by it -- no pending rows survive, and both committed
    versions carry their own distinct, correct attempt_id."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    _add_snapshot(db, "PER-001", "EML-002")

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    version_repo = PersonStoryVersionRepository(db)
    calls: list[int] = []

    def competing_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            real_insert_pending(
                version_repo,
                {
                    "id": "PSV-COMPETITOR-1", "person_id": "PER-001", "version": 1,
                    "summary": "Competing writer's version.", "source_email_ids": ["EML-001"],
                    "source_snapshot_ids": ["PCS-EML-001"], "attempt_id": "ATT-COMPETITOR",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "change_reason": "First version.", "superseded": False, "commit_status": "pending",
                },
            )
            version_repo.set_commit_status("PER-001", 1, "committed")
            raise DuplicateKeyError("E11000 simulated real race on version 1")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", competing_insert_pending)

    result = persist_story_version(db, "PER-001", "My own summary.", ["EML-002"])

    assert result["version_created"] is True
    assert result["version"] == 2
    assert PersonStoryVersionRepository(db).pending_for_person("PER-001") == []
    committed = {v["version"]: v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001")}
    assert committed[1]["summary"] == "Competing writer's version."
    assert committed[1]["attempt_id"] == "ATT-COMPETITOR"
    assert committed[2]["summary"] == "My own summary."
    assert committed[2]["attempt_id"] == result["attempt_id"]


def test_person_story_version_raises_for_unknown_person(db):
    with pytest.raises(ValueError):
        persist_story_version(db, "PER-999", "Summary.", None)


# --- 12: history retrieval only returns committed versions -------------------------------------


def test_get_person_story_history_excludes_pending_versions(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "Committed summary.", ["EML-001"])
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-999", "person_id": "PER-001", "version": 2, "summary": "Never finished.",
            "source_email_ids": [], "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": None, "superseded": False, "commit_status": "pending",
        }
    )

    history = tools.get_person_story_history(db, "PER-001")

    assert len(history) == 1
    assert history[0]["summary"] == "Committed summary."


def test_get_person_story_history_empty_for_person_with_no_history(db):
    _make_person(db)

    assert tools.get_person_story_history(db, "PER-001") == []


def test_get_person_story_history_raises_for_unknown_person(db):
    with pytest.raises(ValueError):
        tools.get_person_story_history(db, "PER-999")


# --- 13: persist_person_profile integration / Milestone 1 compatibility ------------------------


def test_persist_person_profile_creates_a_version_and_reports_it(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Khyati's first real profile.", source_email_ids=["EML-001"]
    )

    assert result["story_version_created"] is True
    assert result["story_version"] == 1
    assert result["profile_summary"] == "Khyati's first real profile."
    # Audit finding #2 fix: a genuine successful generation DOES earn "generated".
    assert result["profile_status"] == "generated"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Khyati's first real profile."
    assert stored["profile_status"] == "generated"


def test_persist_person_profile_without_new_evidence_retains_version(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])

    result = tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])

    assert result["story_version_created"] is False
    assert result["story_version"] == 1


def test_persist_person_profile_with_no_profile_summary_at_all_creates_no_version(db):
    """A person with neither an existing profile nor a new one supplied --
    nothing to version, must not fabricate one."""
    _make_person(db, profile_summary=None)

    result = tools.persist_person_profile(db, "PER-001", role="Updated role only.")

    assert result["story_version_created"] is False
    assert result["story_version"] is None
    # Audit finding #2 regression: a role-only update on a person with no
    # existing profile must NOT be falsely marked "generated" -- no profile
    # text was ever produced or persisted by this call.
    assert result["profile_status"] != "generated"
    assert result["profile_summary"] is None
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_status"] != "generated"
    assert stored["role"] == "Updated role only."


# --- Audit findings #1 and #2 regressions (fix verification) ---------------------------------


def test_finding_1_regression_insufficient_evidence_never_overwrites_existing_profile_summary(db):
    """THE critical regression: a person already has a committed version and
    a real profile_summary. A later call submits DIFFERENT text but only
    evidence already covered by that version -- persist_story_version
    correctly judges this not meaningful, and profile_summary must stay
    exactly as it was. Before the fix, this call silently overwrote
    profile_summary with the new (unversioned) text."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(
        db, "PER-001", profile_summary="Original, committed narrative.", source_email_ids=["EML-001"]
    )

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="A completely different, unjustified narrative.",
        source_email_ids=["EML-001"],  # same evidence, nothing new
    )

    assert result["story_version_created"] is False
    assert result["profile_summary"] == "Original, committed narrative."
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Original, committed narrative."
    # Committed history must also be completely unaffected.
    history = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(history) == 1
    assert history[0]["version"] == 1
    assert history[0]["summary"] == "Original, committed narrative."


def test_finding_1_regression_identical_summary_resubmission_is_a_true_no_op(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="Stable narrative.", source_email_ids=["EML-001"])

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Stable narrative.", source_email_ids=["EML-001"]
    )

    assert result["story_version_created"] is False
    assert result["profile_summary"] == "Stable narrative."
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Stable narrative."
    assert len(PersonStoryVersionRepository(db).all_committed_for_person("PER-001")) == 1


def test_finding_1_regression_meaningful_update_still_updates_summary(db):
    """The fix must not become overly conservative -- a genuinely meaningful
    update (new evidence AND new text) must still update profile_summary."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="v2, with real new evidence.", source_email_ids=["EML-001", "EML-002"]
    )

    assert result["story_version_created"] is True
    assert result["profile_summary"] == "v2, with real new evidence."
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v2, with real new evidence."


# --- Evidence-metadata consistency regressions (profile_source_email_ids) --------------------


def test_rejected_version_preserves_both_existing_summary_and_existing_evidence(db):
    """Finding (final-verification pass): profile_source_email_ids must be
    gated on story_version_created exactly like profile_summary already is.
    A different summary submitted with evidence that introduces no
    genuinely new email (here: a narrower subset of what's already
    recorded) is rejected by persist_story_version's own meaningful-change
    rule -- both profile_summary AND profile_source_email_ids must then stay
    at their existing stored values, never drift to the just-submitted
    (narrower/different) evidence list."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    _add_snapshot(db, "PER-001", "EML-002")
    tools.persist_person_profile(
        db, "PER-001", profile_summary="Original.", source_email_ids=["EML-001", "EML-002"]
    )

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="A completely different, unjustified narrative.",
        source_email_ids=["EML-001"],  # a different (narrower) list, but no NEW email -> not meaningful
    )

    assert result["story_version_created"] is False
    assert result["profile_summary"] == "Original."
    assert result["profile_source_email_ids"] == ["EML-001", "EML-002"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Original."
    assert stored["profile_source_email_ids"] == ["EML-001", "EML-002"]


def test_meaningful_update_updates_evidence_together_with_summary(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="v2, with real new evidence.", source_email_ids=["EML-001", "EML-002"]
    )

    assert result["story_version_created"] is True
    assert result["profile_summary"] == "v2, with real new evidence."
    assert result["profile_source_email_ids"] == ["EML-001", "EML-002"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == ["EML-001", "EML-002"]


def test_identical_summary_resubmission_does_not_introduce_evidence_drift(db):
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="Stable narrative.", source_email_ids=["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    # Identical text resubmitted, but with a DIFFERENT (newer) evidence list --
    # must not be allowed to silently advance evidence for unchanged text.
    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Stable narrative.", source_email_ids=["EML-001", "EML-002"]
    )

    assert result["story_version_created"] is False
    assert result["profile_source_email_ids"] == ["EML-001"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == ["EML-001"]


def test_role_only_update_preserves_existing_summary_and_evidence(db):
    """A person who already has a real profile + versioned evidence; a later
    call that only changes `role` (no new profile_summary, no new
    source_email_ids) must leave both profile_summary and
    profile_source_email_ids completely untouched."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="Established narrative.", source_email_ids=["EML-001"])

    result = tools.persist_person_profile(db, "PER-001", role="VP of Partnerships")

    assert result["story_version_created"] is False
    assert result["profile_summary"] == "Established narrative."
    assert result["profile_source_email_ids"] == ["EML-001"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["role"] == "VP of Partnerships"
    assert stored["profile_summary"] == "Established narrative."
    assert stored["profile_source_email_ids"] == ["EML-001"]


def test_persist_person_profile_reflects_a_successful_duplicate_key_retry(db, monkeypatch):
    """Finding #4 fix, integration-level: persist_person_profile's own
    profile_summary/profile_source_email_ids must reflect whatever version
    the re-evaluated retry actually produced (version 2, with THIS call's
    own text/evidence), not the stale version 1 next_version it originally
    attempted."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    _add_snapshot(db, "PER-001", "EML-002")

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    version_repo = PersonStoryVersionRepository(db)
    calls: list[int] = []

    def competing_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            real_insert_pending(
                version_repo,
                {
                    "id": "PSV-COMPETITOR-1", "person_id": "PER-001", "version": 1,
                    "summary": "Competing writer's version.", "source_email_ids": ["EML-001"],
                    "source_snapshot_ids": ["PCS-EML-001"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "change_reason": "First version.", "superseded": False, "commit_status": "pending",
                },
            )
            version_repo.set_commit_status("PER-001", 1, "committed")
            raise DuplicateKeyError("E11000 simulated real race on version 1")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", competing_insert_pending)

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="My own summary.", source_email_ids=["EML-002"]
    )

    assert result["story_version_created"] is True
    assert result["story_version"] == 2
    assert result["profile_summary"] == "My own summary."
    assert result["profile_source_email_ids"] == ["EML-002"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "My own summary."
    assert stored["profile_source_email_ids"] == ["EML-002"]


def test_persist_person_profile_preserves_existing_fields_when_retry_becomes_a_no_op(db, monkeypatch):
    """Finding #4 fix, integration-level: when the retry re-evaluates and
    finds the update no longer meaningful (a competing writer already
    committed the same text+evidence), this call created no version of its
    own -- its profile_summary/profile_source_email_ids must stay exactly as
    they were before this call, never advance merely because the submitted
    text happened to match what someone else already committed."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    tools.persist_person_profile(db, "PER-001", profile_summary="Original.", source_email_ids=["EML-001"])

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    version_repo = PersonStoryVersionRepository(db)
    calls: list[int] = []

    def competing_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            real_insert_pending(
                version_repo,
                {
                    "id": "PSV-COMPETITOR-2", "person_id": "PER-001", "version": 2,
                    "summary": "Converged v2.", "source_email_ids": ["EML-001", "EML-002"],
                    "source_snapshot_ids": ["PCS-EML-001", "PCS-EML-002"],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "change_reason": "Converged.", "superseded": False, "commit_status": "pending",
                },
            )
            version_repo.set_commit_status("PER-001", 2, "committed")
            version_repo.mark_superseded("PER-001", 1)
            raise DuplicateKeyError("E11000 simulated real race on version 2")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", competing_insert_pending)

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Converged v2.", source_email_ids=["EML-001", "EML-002"]
    )

    assert result["story_version_created"] is False
    assert result["profile_summary"] == "Original."
    assert result["profile_source_email_ids"] == ["EML-001"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Original."
    assert stored["profile_source_email_ids"] == ["EML-001"]


def test_persist_person_profile_gates_profile_story_attempt_id_like_profile_summary(db):
    """Finding #5 fix, integration-level: profile_story_attempt_id must
    advance only when a version was actually created, and stay put
    otherwise -- exactly the same gating profile_summary and
    profile_source_email_ids already use."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])

    assert result["story_version_created"] is True
    first_attempt_id = result["profile_story_attempt_id"]
    assert first_attempt_id is not None
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_story_attempt_id"] == first_attempt_id

    # A later rejected (non-meaningful) update must not advance it.
    result2 = tools.persist_person_profile(
        db, "PER-001", profile_summary="A different, unjustified narrative.", source_email_ids=["EML-001"]
    )
    assert result2["story_version_created"] is False
    stored2 = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored2["profile_story_attempt_id"] == first_attempt_id  # unchanged


def test_legacy_profile_first_versioned_update_still_sets_evidence(db):
    """A pre-Milestone-2 person with profile_summary already set directly
    (no story history at all) going through persist_person_profile for the
    first time establishes version 1 unconditionally (meaningful=True, no
    prior version to compare against) -- evidence must be set together with
    it exactly like any other first version, unaffected by this fix."""
    _make_person(db, profile_summary="Legacy pre-Milestone-2 text.")
    _add_snapshot(db, "PER-001", "EML-001")

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Legacy pre-Milestone-2 text.", source_email_ids=["EML-001"]
    )

    assert result["story_version_created"] is True
    assert result["profile_source_email_ids"] == ["EML-001"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == ["EML-001"]


def test_existing_persist_person_profile_milestone_1_behavior_unaffected(db):
    """Milestone 1's own exact assertions, re-run here to confirm Milestone 2
    didn't change profile_status/profile_source_email_ids behavior."""
    _make_person(db)

    result = tools.persist_person_profile(
        db, "PER-001", profile_summary="Text.", source_email_ids=["EML-001", "EML-002"],
    )

    assert result["profile_status"] == "generated"
    assert result["profile_source_email_ids"] == ["EML-001", "EML-002"]


# --- 14: raw-dump isolation unaffected -----------------------------------------------------


def test_ingest_raw_email_only_still_never_touches_person_story_versions(db):
    from app.email.models import parse_email

    email = parse_email(
        {
            "message_id": "gmail-msg-001", "from": {"name": "X", "email": "x@example.com"},
            "to": [{"name": "Y", "email": "y@example.com"}], "subject": "Hi", "body": "Hi.",
            "timestamp": "2026-10-07T09:00:00Z",
        }
    )

    tools.ingest_raw_email_only(db, email)

    assert db.person_story_versions.count_documents({}) == 0
    assert db.people.count_documents({}) == 0


# --- Finding #7: verified repository mutations -----------------------------------------------


def test_priority_a_unverified_commit_status_raises_rather_than_false_success(db, monkeypatch):
    """A 0-match (or otherwise no-op) set_commit_status for the version THIS
    call just inserted must never be reported as a successful version
    creation -- it's load-bearing, so this raises."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    monkeypatch.setattr(
        PersonStoryVersionRepository, "set_commit_status",
        lambda self, person_id, version, commit_status: None,
    )

    with pytest.raises(RuntimeError):
        persist_story_version(db, "PER-001", "v1.", ["EML-001"])

    # Never falsely promoted to committed -- the row stays exactly pending.
    pending = PersonStoryVersionRepository(db).pending_for_person("PER-001")
    assert len(pending) == 1
    assert pending[0]["version"] == 1
    assert PersonStoryVersionRepository(db).all_committed_for_person("PER-001") == []


def test_priority_b_unverified_superseded_flag_logs_warning_but_still_succeeds(db, monkeypatch, caplog):
    """mark_superseded is cosmetic bookkeeping on a DIFFERENT (older) row
    than the one this call just committed -- a verification failure here
    must only log, never abort an otherwise-successful version commit."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")
    persist_story_version(db, "PER-001", "v1.", ["EML-001"])
    _add_snapshot(db, "PER-001", "EML-002")

    monkeypatch.setattr(
        PersonStoryVersionRepository, "mark_superseded", lambda self, person_id, version: None,
    )

    with caplog.at_level(logging.WARNING, logger="app.entities.person_story"):
        result = persist_story_version(db, "PER-001", "v2.", ["EML-001", "EML-002"])

    assert result["version_created"] is True
    assert result["version"] == 2
    committed = {v["version"]: v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001")}
    assert committed[2]["summary"] == "v2."  # the new version committed successfully
    assert committed[1]["superseded"] is False  # never flipped, due to the simulated no-op
    # The warning must carry enough context to identify the person and the
    # specific (previous) version that failed to verify -- not merely the
    # operation name -- so an operator reading logs can act on it directly.
    warning_messages = [record.message for record in caplog.records]
    assert any("mark_superseded" in message for message in warning_messages)
    assert any("person_id=PER-001" in message for message in warning_messages)
    assert any("version=1" in message for message in warning_messages)


def test_priority_c_unverified_historical_promotion_logs_warning_and_continues(db, monkeypatch, caplog):
    """A historical pending row is eligible for promotion (its attempt_id
    matches the Person's recorded pointer), but its commit-status write
    silently no-ops. This must not be treated as promoted, must log a
    warning, and -- critically -- must NOT abort this call's own,
    independent new-version evaluation."""
    _make_person(db, profile_summary="v2 text.", profile_story_attempt_id="ATT-001")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Interrupted summary.",
            "source_email_ids": ["EML-001"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
            "attempt_id": "ATT-001",
        }
    )
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-002", "person_id": "PER-001", "version": 2, "summary": "v2 text.",
            "source_email_ids": ["EML-002"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "Already committed separately.", "superseded": False, "commit_status": "committed",
        }
    )
    _add_snapshot(db, "PER-001", "EML-003")

    real_set_commit_status = PersonStoryVersionRepository.set_commit_status

    def selective_set_commit_status(self, person_id, version, commit_status):
        if version == 1:
            return None  # simulate the historical row's promotion silently failing
        return real_set_commit_status(self, person_id, version, commit_status)

    monkeypatch.setattr(PersonStoryVersionRepository, "set_commit_status", selective_set_commit_status)

    with caplog.at_level(logging.WARNING, logger="app.entities.person_story"):
        result = persist_story_version(db, "PER-001", "v3 text.", ["EML-002", "EML-003"])

    assert result["version_created"] is True
    assert result["version"] == 3  # this call's own independent evaluation succeeded normally
    pending = PersonStoryVersionRepository(db).pending_for_person("PER-001")
    assert len(pending) == 1
    assert pending[0]["version"] == 1  # never promoted -- simulated no-op left it pending
    # The warning must carry enough context to identify the person and the
    # specific historical version that failed to promote -- not merely the
    # reconciliation context -- so an operator reading logs can act on it.
    warning_messages = [record.message for record in caplog.records]
    assert any("reconciliation" in message for message in warning_messages)
    assert any("person_id=PER-001" in message for message in warning_messages)
    assert any("version=1" in message for message in warning_messages)


def test_priority_e_person_deleted_mid_call_raises_instead_of_resurrecting(db, monkeypatch):
    """If the Person document is deleted by some other process after
    persist_person_profile's initial read but before its final write, the
    final write must raise rather than silently recreating the person from
    the stale in-memory snapshot."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    real_persist_story_version = tools.persist_story_version

    def deleting_persist_story_version(db_, person_id, summary, source_email_ids):
        result = real_persist_story_version(db_, person_id, summary, source_email_ids)
        PersonRepository(db_)._collection.delete_one({"id": person_id})
        return result

    monkeypatch.setattr(tools, "persist_story_version", deleting_persist_story_version)

    with pytest.raises(RuntimeError):
        tools.persist_person_profile(db, "PER-001", profile_summary="v1.", source_email_ids=["EML-001"])

    assert PersonRepository(db).find_one({"id": "PER-001"}) is None  # not resurrected


def test_priority_e_normal_update_unaffected_by_update_existing_switch(db):
    """Sanity check: the ordinary (no concurrent deletion) path through
    update_existing must behave identically to the prior upsert_by_key-based
    write -- same fields, same values, nothing regressed by the switch."""
    _make_person(db)
    _add_snapshot(db, "PER-001", "EML-001")

    result = tools.persist_person_profile(
        db, "PER-001", role="VP", profile_summary="v1.", source_email_ids=["EML-001"]
    )

    assert result["role"] == "VP"
    assert result["profile_summary"] == "v1."
    assert result["profile_source_email_ids"] == ["EML-001"]
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["role"] == "VP"
    assert stored["profile_summary"] == "v1."
