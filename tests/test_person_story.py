"""Tests for Milestone 2: app.entities.person_story (versioned person-story
history) and its integration into app.mcp.tools.persist_person_profile.

No transactions are used anywhere (mongomock raises NotImplementedError for
sessions -- confirmed directly, see app/entities/person_story.py's module
docstring). Consistency is instead tested via: pending/committed state,
write ordering, and the race-retry path.
"""
from datetime import datetime, timezone

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

    assert result == {"version_created": True, "version": 1, "change_reason": "First version (1 source email(s))."}


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


def test_race_retries_past_a_duplicate_key_error(db, monkeypatch):
    """A genuine concurrent race (two callers both compute next_version=1
    before either has inserted) can't be reproduced deterministically in a
    single-threaded test without mocking -- so this directly forces exactly
    one DuplicateKeyError on the first insert attempt (simulating "the other
    writer won version 1 a moment ago") and verifies the retry loop recovers
    by trying version 2 next, rather than raising or silently losing the
    write."""
    _make_person(db)

    real_insert_pending = PersonStoryVersionRepository.insert_pending
    calls: list[int] = []

    def flaky_insert_pending(self, document):
        calls.append(document["version"])
        if len(calls) == 1:
            raise DuplicateKeyError("E11000 simulated race")
        return real_insert_pending(self, document)

    monkeypatch.setattr(PersonStoryVersionRepository, "insert_pending", flaky_insert_pending)

    result = persist_story_version(db, "PER-001", "My own summary.", None)

    assert result["version_created"] is True
    assert result["version"] == 2
    assert calls == [1, 2]


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


def test_stale_pending_row_matching_current_profile_is_finished_committing(db):
    """Simulates a crash between the commit-flip and marking the previous
    version superseded/updating profile_summary having already happened --
    i.e. profile_summary already reflects the pending row's text. The next
    call must self-heal: commit it, not duplicate it."""
    _make_person(db, profile_summary="Interrupted summary.")
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Interrupted summary.",
            "source_email_ids": ["EML-001"], "source_snapshot_ids": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": "First version.", "superseded": False, "commit_status": "pending",
        }
    )

    persist_story_version(db, "PER-001", "Interrupted summary.", ["EML-001"])

    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1
    assert committed[0]["version"] == 1
    assert PersonStoryVersionRepository(db).pending_for_person("PER-001") == []


def test_stale_pending_row_not_matching_current_profile_is_discarded(db):
    """A pending row that never got far enough (profile_summary was never
    updated to match it) is stale debris from an abandoned attempt -- must
    be discarded, never promoted to committed, and must never block the
    next real version number."""
    _make_person(db, profile_summary=None)
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-001", "person_id": "PER-001", "version": 1, "summary": "Abandoned attempt.",
            "source_email_ids": [], "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": None, "superseded": False, "commit_status": "pending",
        }
    )

    result = persist_story_version(db, "PER-001", "The real first version.", None)

    assert result["version_created"] is True
    assert result["version"] == 1
    committed = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert len(committed) == 1
    assert committed[0]["summary"] == "The real first version."


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
