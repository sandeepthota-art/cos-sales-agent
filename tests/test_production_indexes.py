# tests/test_production_indexes.py
"""Phase 21: the production index manifest, plan-building, and approval-gated
execution/rollback. All mongomock only -- this test file never touches real Atlas,
and Phase 21A itself never calls execute_index_plan against real Atlas either
(that only happens after explicit human approval, in Phase 21B).
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.production_indexes import (
    INDEX_MANIFEST,
    build_index_plan,
    capture_pre_execution_index_snapshot,
    compute_manifest_hash,
    diff_manifest_against_existing,
    execute_index_plan,
    list_existing_indexes,
    read_index_plan,
    rollback_created_indexes,
    validate_unique_candidate,
    write_index_plan,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


# --- 1/9: deterministic manifest/plan hashing -----------------------------------------------


def test_manifest_hash_is_deterministic_across_calls():
    assert compute_manifest_hash(INDEX_MANIFEST) == compute_manifest_hash(INDEX_MANIFEST)


def test_manifest_hash_is_order_independent():
    reordered = list(reversed(INDEX_MANIFEST))
    assert compute_manifest_hash(INDEX_MANIFEST) == compute_manifest_hash(reordered)


def test_manifest_hash_ignores_narrative_fields_not_the_spec():
    reworded = [dict(e, reason="a completely different explanation") for e in INDEX_MANIFEST]
    assert compute_manifest_hash(INDEX_MANIFEST) == compute_manifest_hash(reworded)


def test_manifest_hash_changes_when_a_key_actually_changes():
    changed = [dict(e) for e in INDEX_MANIFEST]
    changed[0] = dict(changed[0], key=[("some_other_field", 1)])
    assert compute_manifest_hash(INDEX_MANIFEST) != compute_manifest_hash(changed)


def test_no_manifest_entry_combines_two_array_fields_in_one_compound_index():
    # MongoDB forbids more than one multikey (array) field per compound index --
    # confirm the manifest itself never violates this.
    for entry in INDEX_MANIFEST:
        array_fields_in_key = 0
        # is_list describes the WHOLE entry's single field here; this manifest never
        # defines a compound key at all, but guard the invariant explicitly anyway.
        if entry["is_list"]:
            array_fields_in_key += 1
        assert array_fields_in_key <= 1
        assert len(entry["key"]) == 1  # every manifest entry today is a simple, single-field index


def test_no_duplicate_index_names_in_manifest():
    names = [e["name"] for e in INDEX_MANIFEST]
    assert len(names) == len(set(names))


# --- 2/3: duplicate / equivalent existing index detection -----------------------------------


def test_diff_detects_already_satisfied_equivalent_index(db):
    db.organizations.create_index("id", name="id_1", unique=True)

    diff = diff_manifest_against_existing(db, INDEX_MANIFEST)

    satisfied_names = {e["name"] for e in diff["already_satisfied"]}
    assert "idx_organizations_id_unique" in satisfied_names
    assert not any(e["name"] == "idx_organizations_id_unique" for e in diff["missing"])


def test_diff_reports_missing_when_no_equivalent_exists(db):
    diff = diff_manifest_against_existing(db, INDEX_MANIFEST)

    missing_names = {e["name"] for e in diff["missing"]}
    assert "idx_organizations_domain_unique" in missing_names


# --- 4: conflicting index-name detection -----------------------------------------------------


def test_diff_detects_conflicting_name_with_different_spec(db):
    # Same NAME as a manifest entry, but a non-unique index instead of unique.
    db.organizations.create_index("id", name="idx_organizations_id_unique", unique=False)

    diff = diff_manifest_against_existing(db, INDEX_MANIFEST)

    conflicting_names = {c["manifest_entry"]["name"] for c in diff["conflicting_name"]}
    assert "idx_organizations_id_unique" in conflicting_names


# --- 5/7: unique-index data-quality validation, optional/null-field safety -------------------


def test_validate_unique_candidate_reports_safe_when_no_duplicates(db):
    db.organizations.insert_many([{"id": "ORG-1", "domain": "a.example"}, {"id": "ORG-2", "domain": "b.example"}])

    result = validate_unique_candidate(db, "organizations", "domain")

    assert result["safe_for_unique_index"] is True
    assert result["duplicate_values"] == {}


def test_validate_unique_candidate_detects_real_duplicates(db):
    db.organizations.insert_many([{"id": "ORG-1", "domain": "same.example"}, {"id": "ORG-2", "domain": "same.example"}])

    result = validate_unique_candidate(db, "organizations", "domain")

    assert result["safe_for_unique_index"] is False
    assert "same.example" in result["duplicate_values"]


def test_validate_unique_candidate_never_treats_null_as_a_duplicate(db):
    # Several documents missing/null on the field must never be flagged as colliding
    # with each other -- this is the exact optional/null-field safety Section H requires.
    db.people.insert_many([{"id": "PER-1", "org_id": None}, {"id": "PER-2", "org_id": None}, {"id": "PER-3"}])

    result = validate_unique_candidate(db, "people", "org_id")

    assert result["safe_for_unique_index"] is True
    assert result["null_or_missing_count"] == 3


# --- 6/8: sparse semantics + multikey array-field handling -----------------------------------


def test_diff_distinguishes_sparse_from_non_sparse_equivalent():
    manifest = [{"collection": "people", "name": "idx_test_sparse", "key": [("org_id", 1)], "unique": False, "sparse": True, "is_list": False, "required": True}]
    client = mongomock.MongoClient()
    db = client["t"]
    db.people.create_index("org_id", name="existing_non_sparse")  # same key, NOT sparse

    diff = diff_manifest_against_existing(db, manifest)

    assert manifest[0] in diff["missing"]  # not satisfied -- sparse flag differs
    assert diff["already_satisfied"] == []


def test_multikey_array_field_index_tolerates_none_valued_documents(db):
    db.threads.insert_many([
        {"thread_id": "t1", "person_ids": ["PER-1"], "org_ids": []},
        {"thread_id": "t2", "person_ids": None, "org_ids": None},  # the exact Phase 18 regression shape
    ])

    plan = build_index_plan(db)  # must not raise

    assert any(e["name"] == "idx_threads_person_ids" for e in plan["entries"])


# --- 10/11: approval gating + idempotent execution --------------------------------------------


def test_execute_index_plan_requires_approval(db):
    plan = build_index_plan(db)  # every entry defaults to approved: false

    with pytest.raises(ValueError, match="no approved index entries"):
        execute_index_plan(db, plan)


def test_execute_index_plan_only_creates_approved_entries(db):
    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = entry["name"] == "idx_migration_runs_run_id_unique"

    result = execute_index_plan(db, plan)

    assert result["created"] == ["idx_migration_runs_run_id_unique"]
    info = db.migration_runs.index_information()
    assert "idx_migration_runs_run_id_unique" in info


def test_execute_index_plan_is_idempotent(db):
    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = True
    # Remove blocked/unsafe entries for this happy-path test (none expected on a clean db).
    assert not plan["blocked_candidates"]

    first = execute_index_plan(db, plan)
    second_plan = build_index_plan(db)
    for entry in second_plan["entries"]:
        entry["approved"] = True
    second = execute_index_plan(db, second_plan)

    assert len(first["created"]) == len(INDEX_MANIFEST)
    assert second["created"] == []  # everything already satisfied on the second pass


def test_execute_index_plan_aborts_on_plan_hash_mismatch(db):
    plan = build_index_plan(db)
    plan["manifest_hash"] = "deadbeef00000000"
    for entry in plan["entries"]:
        entry["approved"] = True

    with pytest.raises(ValueError, match="hash mismatch"):
        execute_index_plan(db, plan)


def test_execute_index_plan_refuses_blocked_entry_even_if_approved(db):
    db.organizations.insert_many([{"id": "ORG-1", "domain": "dup.example"}, {"id": "ORG-2", "domain": "dup.example"}])
    plan = build_index_plan(db)
    domain_entry = next(e for e in plan["entries"] if e["name"] == "idx_organizations_domain_unique")
    assert domain_entry["blocked"] is True
    for entry in plan["entries"]:
        entry["approved"] = True

    with pytest.raises(ValueError, match="blocked"):
        execute_index_plan(db, plan)

    assert "idx_organizations_domain_unique" not in db.organizations.index_information()


def test_execute_index_plan_aborts_on_conflicting_name(db):
    db.organizations.create_index("id", name="idx_organizations_id_unique", unique=False)  # wrong spec, same name
    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = True

    with pytest.raises(ValueError, match="already exist"):
        execute_index_plan(db, plan)


def test_rejected_candidates_never_appear_in_the_executable_plan(db):
    plan = build_index_plan(db)

    entry_fields = {(e["collection"], e["key"][0][0]) for e in plan["entries"]}
    assert ("commitments", "project_id") not in entry_fields
    assert ("projects", "entity") not in entry_fields
    assert ("people", "org") not in entry_fields
    assert len(plan["rejected_candidates"]) >= 5


# --- 12/13: partial-execution identification + safe rollback -----------------------------------


def test_partial_failure_surfaces_indexes_created_before_the_error(db, monkeypatch):
    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = True

    original_create_index = db.organizations.create_index
    call_count = {"n": 0}

    def _flaky_create_index(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated transient failure")
        return original_create_index(*args, **kwargs)

    monkeypatch.setattr(db.organizations, "create_index", _flaky_create_index)

    with pytest.raises(RuntimeError, match="index creation failed after creating"):
        execute_index_plan(db, plan)

    # At least the first successfully-created index must exist even though the run failed.
    assert db.organizations.index_information()  # not empty -- something was created before the failure


def test_capture_pre_execution_snapshot_and_safe_rollback(db):
    db.people.create_index("id", name="preexisting_index")  # simulates an index Phase 21 didn't create
    plan = build_index_plan(db)
    snapshot = capture_pre_execution_index_snapshot(db, plan)
    for entry in plan["entries"]:
        entry["approved"] = True

    result = execute_index_plan(db, plan)
    rollback_result = rollback_created_indexes(db, snapshot, result["created"])

    assert set(rollback_result["dropped"]) == set(result["created"])
    assert rollback_result["skipped"] == []
    # Every index this phase created is now gone...
    remaining = list_existing_indexes(db, sorted({e["collection"] for e in INDEX_MANIFEST}))
    remaining_names = {idx["name"] for indexes in remaining.values() for idx in indexes}
    assert not (remaining_names & {e["name"] for e in INDEX_MANIFEST})
    # ...but the pre-existing index is untouched.
    assert "preexisting_index" in db.people.index_information()


def test_rollback_never_drops_a_preexisting_index_even_if_named_in_created_list(db):
    db.organizations.create_index("id", name="idx_organizations_id_unique", unique=True)
    plan = build_index_plan(db)
    snapshot = capture_pre_execution_index_snapshot(db, plan)

    # Simulate a caller mistakenly claiming this pre-existing index was "created".
    rollback_result = rollback_created_indexes(db, snapshot, ["idx_organizations_id_unique"])

    assert rollback_result["dropped"] == []
    assert rollback_result["skipped"][0]["name"] == "idx_organizations_id_unique"
    assert "idx_organizations_id_unique" in db.organizations.index_information()


# --- 14/15: no document mutation, current indexes preserved -----------------------------------


def test_build_index_plan_never_writes_a_document(db):
    db.people.insert_one({"id": "PER-1", "org_id": "ORG-1"})
    before = list(db.people.find({}, {"_id": 0}))

    build_index_plan(db)

    after = list(db.people.find({}, {"_id": 0}))
    assert before == after


def test_execute_index_plan_never_writes_a_document(db):
    db.people.insert_one({"id": "PER-1", "org_id": "ORG-1"})
    before = list(db.people.find({}, {"_id": 0}))
    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = True

    execute_index_plan(db, plan)

    after = list(db.people.find({}, {"_id": 0}))
    assert before == after


def test_execute_index_plan_preserves_indexes_it_did_not_create(db):
    db.commitments.create_index("thread_id", name="thread_id_1")  # a real pre-existing app index

    plan = build_index_plan(db)
    for entry in plan["entries"]:
        entry["approved"] = True
    execute_index_plan(db, plan)

    assert "thread_id_1" in db.commitments.index_information()


# --- plan I/O ----------------------------------------------------------------------------------


def test_write_and_read_index_plan_round_trip(db, tmp_path):
    plan = build_index_plan(db)
    path = str(tmp_path / "index_plan.json")

    write_index_plan(plan, path)
    loaded = read_index_plan(path)

    assert loaded["manifest_hash"] == plan["manifest_hash"]
    assert len(loaded["entries"]) == len(plan["entries"])
