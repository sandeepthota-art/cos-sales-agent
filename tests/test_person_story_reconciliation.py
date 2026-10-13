"""Tests for app.entities.person_story_reconciliation (Finding #3 recovery).

reconcile_person_story is conservative by design: it never creates a
person_story_versions document, never touches source_email_ids/
source_snapshot_ids, and only ever repairs profile_summary when BOTH
dry_run=False AND repair_profile_summary=True are explicitly passed --
ambiguous_mismatch is never auto-repaired under any flag combination.
"""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import PersonRepository, PersonStoryVersionRepository
from app.entities.person_story_reconciliation import reconcile_person_story
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


def _insert_committed_version(db, person_id, version, summary, superseded, source_email_ids=None):
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": f"PSV-{person_id}-{version}", "person_id": person_id, "version": version,
            "summary": summary, "source_email_ids": source_email_ids or [],
            "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": None, "superseded": superseded, "commit_status": "committed",
        }
    )


# --- 1: no history -----------------------------------------------------------------------------


def test_no_history_does_not_modify_profile_or_fabricate_a_version(db):
    _make_person(db, profile_summary="Legacy pre-Milestone-2 text.")

    result = reconcile_person_story(db, "PER-001")

    assert result["classification"] == "no_history"
    assert result["action"] == "none"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Legacy pre-Milestone-2 text."
    assert PersonStoryVersionRepository(db).all_committed_for_person("PER-001") == []


# --- 2: consistent -------------------------------------------------------------------------------


def test_profile_matching_latest_version_is_consistent(db):
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001")

    assert result["classification"] == "consistent"
    assert result["action"] == "none"
    assert result["matched_version"] == 1


# --- 3: probable_interrupted_write, dry-run leaves DB unchanged --------------------------------


def test_profile_matching_earlier_version_dry_run_proposes_repair_without_writing(db):
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=True)

    assert result["classification"] == "probable_interrupted_write"
    assert result["action"] == "would_update_profile_summary"
    assert result["matched_version"] == 1
    assert result["latest_version"] == 2
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v1 text."  # unchanged


def test_probable_interrupted_write_not_repaired_in_execute_without_opt_in(db):
    """--execute alone (no --repair-profile-summary) must NOT touch
    profile_summary -- manual-review-by-default even in execute mode."""
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=False)

    assert result["action"] == "would_update_profile_summary"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v1 text."


def test_probable_interrupted_write_repaired_only_with_explicit_opt_in(db):
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["action"] == "updated_profile_summary"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v2 text."


# --- 4: ambiguous_mismatch never auto-overwritten, including execute mode ----------------------


def test_ambiguous_mismatch_never_overwritten_even_in_execute_mode(db):
    _make_person(db, profile_summary="Some unrelated text matching no version at all.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["classification"] == "ambiguous_mismatch"
    assert result["action"] == "manual_review_required"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "Some unrelated text matching no version at all."


# --- 5: person not found --------------------------------------------------------------------------


def test_person_not_found_returns_clear_result_without_creating_a_person(db):
    result = reconcile_person_story(db, "PER-999")

    assert result["classification"] == "not_found"
    assert result["action"] == "none"
    assert PersonRepository(db).find_one({"id": "PER-999"}) is None


# --- 6: superseded-flag drift ----------------------------------------------------------------------


def test_superseded_drift_dry_run_does_not_write(db):
    _make_person(db, profile_summary="v2 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)  # drift: should be True
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=True)

    assert result["superseded_drift_versions"] == [1]
    assert result["superseded_repaired_versions"] == []
    v1 = next(v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001") if v["version"] == 1)
    assert v1["superseded"] is False


def test_superseded_drift_execute_repairs_only_verified_older_rows(db):
    _make_person(db, profile_summary="v2 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001", dry_run=False)

    assert result["superseded_repaired_versions"] == [1]
    versions = {v["version"]: v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001")}
    assert versions[1]["superseded"] is True


# --- 7: pending versions untouched by superseded reconciliation --------------------------------


def test_pending_versions_are_not_modified_by_superseded_reconciliation(db):
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-pending-1", "person_id": "PER-001", "version": 2, "summary": "Never finished.",
            "source_email_ids": [], "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": None, "superseded": False, "commit_status": "pending",
        }
    )

    reconcile_person_story(db, "PER-001", dry_run=False)

    pending = PersonStoryVersionRepository(db).pending_for_person("PER-001")
    assert len(pending) == 1
    assert pending[0]["version"] == 2
    assert pending[0]["commit_status"] == "pending"  # untouched


# --- 8: latest committed version's own flag is never touched ------------------------------------


def test_latest_committed_version_is_never_marked_superseded(db):
    _make_person(db, profile_summary="v2 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    reconcile_person_story(db, "PER-001", dry_run=False)

    versions = {v["version"]: v for v in PersonStoryVersionRepository(db).all_committed_for_person("PER-001")}
    assert versions[2]["superseded"] is False


# --- 9: idempotency ---------------------------------------------------------------------------------


def test_repeat_execution_is_idempotent(db):
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    first = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)
    second = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert first["action"] == "updated_profile_summary"
    assert second["classification"] == "consistent"
    assert second["action"] == "none"


def test_repeat_superseded_repair_is_idempotent(db):
    _make_person(db, profile_summary="v2 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    reconcile_person_story(db, "PER-001", dry_run=False)
    second = reconcile_person_story(db, "PER-001", dry_run=False)

    assert second["superseded_drift_versions"] == []
    assert second["superseded_repaired_versions"] == []


# --- 10: repository errors surfaced, not reported as success -----------------------------------


def test_profile_summary_repair_failure_is_raised_not_reported_as_success(db, monkeypatch):
    """Simulates a write that silently fails to verify (e.g. the person
    vanished between write and verify) -- must raise, never return
    action='updated_profile_summary' for a repair that didn't actually land."""
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    real_find_one = PersonRepository.find_one
    call_count = {"n": 0}

    def flaky_find_one(self, query):
        call_count["n"] += 1
        # First call: the initial read inside reconcile_person_story. Second
        # call: the post-write verification -- force it to look unrepaired.
        if call_count["n"] == 2:
            return {"id": "PER-001", "profile_summary": "v1 text."}  # verification should fail
        return real_find_one(self, query)

    monkeypatch.setattr(PersonRepository, "find_one", flaky_find_one)

    with pytest.raises(RuntimeError):
        reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)


def test_superseded_repair_failure_is_raised_not_reported_as_success(db, monkeypatch):
    _make_person(db, profile_summary="v2 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    # mark_superseded becomes a no-op -- simulates a write that silently
    # matched zero documents.
    monkeypatch.setattr(PersonStoryVersionRepository, "mark_superseded", lambda self, person_id, version: None)

    with pytest.raises(RuntimeError):
        reconcile_person_story(db, "PER-001", dry_run=False)


# --- 11: dry-run leaves the database unchanged for every classification ------------------------


@pytest.mark.parametrize(
    "setup_name",
    ["no_history", "consistent", "probable_interrupted_write", "ambiguous_mismatch", "superseded_drift"],
)
def test_dry_run_leaves_database_unchanged_for_every_classification(db, setup_name):
    if setup_name == "no_history":
        _make_person(db, profile_summary="Legacy text.")
    elif setup_name == "consistent":
        _make_person(db, profile_summary="v1 text.")
        _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    elif setup_name == "probable_interrupted_write":
        _make_person(db, profile_summary="v1 text.")
        _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
        _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)
    elif setup_name == "ambiguous_mismatch":
        _make_person(db, profile_summary="Unrelated text.")
        _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
    elif setup_name == "superseded_drift":
        _make_person(db, profile_summary="v2 text.")
        _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=False)
        _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    before_person = PersonRepository(db).find_one({"id": "PER-001"})
    before_versions = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")

    reconcile_person_story(db, "PER-001", dry_run=True, repair_profile_summary=True)

    after_person = PersonRepository(db).find_one({"id": "PER-001"})
    after_versions = PersonStoryVersionRepository(db).all_committed_for_person("PER-001")
    assert before_person == after_person
    assert before_versions == after_versions


# --- Diagnostic output never includes raw summary text as a standalone field ---------------------


def test_detail_field_never_echoes_raw_summary_text_as_a_bare_value(db):
    """The `detail` string may NAME versions, but routine diagnostic output
    must not surface the full stored narrative text as its own field."""
    _make_person(db, profile_summary="v1 text.")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True)
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False)

    result = reconcile_person_story(db, "PER-001")

    assert "summary" not in result
    assert "profile_summary" not in result


# --- Isolation: this tool never touches raw_emails_dump or creates a version --------------------


def test_reconciliation_never_creates_a_story_version(db):
    _make_person(db, profile_summary="Legacy text, no history.")

    reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert PersonStoryVersionRepository(db).all_committed_for_person("PER-001") == []


# --- Evidence-metadata restoration (profile_source_email_ids) ---------------------------------


def test_dry_run_proposes_evidence_repair_without_writing(db):
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=["EML-001", "EML-002"])

    result = reconcile_person_story(db, "PER-001", dry_run=True, repair_profile_summary=True)

    assert result["classification"] == "probable_interrupted_write"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v1 text."
    assert stored["profile_source_email_ids"] == ["EML-001"]  # unchanged


def test_authorized_repair_restores_both_summary_and_evidence_from_the_selected_version(db):
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=["EML-001", "EML-002"])

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["action"] == "updated_profile_summary"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v2 text."
    assert stored["profile_source_email_ids"] == ["EML-001", "EML-002"]  # restored from the TARGET version, not inferred


def test_ambiguous_mismatch_never_touches_evidence_even_in_execute_mode(db):
    _make_person(db, profile_summary="Unrelated text matching no version.", profile_source_email_ids=["EML-999"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=["EML-001", "EML-002"])

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["classification"] == "ambiguous_mismatch"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == ["EML-999"]  # untouched, never fabricated


def test_missing_evidence_on_target_version_is_handled_conservatively(db):
    """A version row that predates the source_email_ids field entirely (no
    key at all, not merely an empty list) must never cause the repair to
    fabricate an evidence list -- profile_summary still repairs normally,
    profile_source_email_ids is left exactly as it was."""
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-ORIGINAL"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    # Version 2 (the repair target) has NO source_email_ids key at all --
    # inserted directly, bypassing the test helper's `or []` default.
    PersonStoryVersionRepository(db).insert_pending(
        {
            "id": "PSV-PER-001-2", "person_id": "PER-001", "version": 2, "summary": "v2 text.",
            "source_snapshot_ids": [], "created_at": datetime.now(timezone.utc).isoformat(),
            "change_reason": None, "superseded": False, "commit_status": "committed",
        }
    )

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["action"] == "updated_profile_summary"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_summary"] == "v2 text."
    assert stored["profile_source_email_ids"] == ["EML-ORIGINAL"]  # left untouched, never fabricated


def test_empty_evidence_on_target_version_is_restored_as_empty_not_skipped(db):
    """An empty list IS the target version's real, stored evidence (as
    opposed to the key being entirely absent) -- it must be restored
    faithfully, not treated as 'missing' and left alone."""
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=[])

    result = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert result["action"] == "updated_profile_summary"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == []


def test_repeated_evidence_repair_is_idempotent(db):
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=["EML-001", "EML-002"])

    first = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)
    second = reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    assert first["action"] == "updated_profile_summary"
    assert second["classification"] == "consistent"
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["profile_source_email_ids"] == ["EML-001", "EML-002"]


def test_repair_does_not_clobber_unrelated_person_fields_changed_concurrently(db, monkeypatch):
    """The repair write must not resend a stale snapshot of the whole person
    document -- a field changed by someone else between reconcile's initial
    read and its repair write must survive."""
    _make_person(db, profile_summary="v1 text.", profile_source_email_ids=["EML-001"], role="Original role")
    _insert_committed_version(db, "PER-001", 1, "v1 text.", superseded=True, source_email_ids=["EML-001"])
    _insert_committed_version(db, "PER-001", 2, "v2 text.", superseded=False, source_email_ids=["EML-001", "EML-002"])

    real_find_one = PersonRepository.find_one
    calls = {"n": 0}

    def find_one_with_concurrent_edit(self, query):
        calls["n"] += 1
        result = real_find_one(self, query)
        if calls["n"] == 1 and result is not None:
            # Simulate a concurrent write landing right after reconcile's own
            # initial read of the person document.
            PersonRepository(db).upsert_by_key({"id": "PER-001"}, {"role": "Updated concurrently"})
        return result

    monkeypatch.setattr(PersonRepository, "find_one", find_one_with_concurrent_edit)

    reconcile_person_story(db, "PER-001", dry_run=False, repair_profile_summary=True)

    stored = real_find_one(PersonRepository(db), {"id": "PER-001"})
    assert stored["role"] == "Updated concurrently"
    assert stored["profile_summary"] == "v2 text."
    assert stored["profile_source_email_ids"] == ["EML-001", "EML-002"]


# --- CLI argument parsing -------------------------------------------------------------------------


def test_cli_requires_exactly_one_of_person_id_or_all():
    from scripts.reconcile_person_stories import build_arg_parser

    parser = build_arg_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--uri", "mongodb://x", "--db", "test"])  # neither given
    with pytest.raises(SystemExit):
        parser.parse_args(["--uri", "mongodb://x", "--db", "test", "--person-id", "PER-001", "--all"])  # both given


def test_cli_accepts_person_id_dry_run_by_default():
    from scripts.reconcile_person_stories import build_arg_parser

    args = build_arg_parser().parse_args(["--uri", "mongodb://x", "--db", "test", "--person-id", "PER-001"])

    assert args.person_id == "PER-001"
    assert args.execute is False
    assert args.repair_profile_summary is False


def test_cli_accepts_all_with_execute_and_repair_flags():
    from scripts.reconcile_person_stories import build_arg_parser

    args = build_arg_parser().parse_args(
        ["--uri", "mongodb://x", "--db", "test", "--all", "--execute", "--repair-profile-summary"]
    )

    assert args.all is True
    assert args.execute is True
    assert args.repair_profile_summary is True
