# tests/test_duplicate_consolidation_execution.py
"""Phase 19: approved-plan execution, plan-integrity checks, rollback snapshot, and
post-mapping/final verification. All tests use isolated mongomock fixtures only --
this phase's real-Atlas execution requires an explicit human-approved plan file that
does not exist yet, so nothing here ever touches production Atlas.
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ThreadRepository,
)
from app.duplicate_consolidation import (
    _plan_hash,
    capture_rollback_snapshot,
    execute_approved_plan,
    generate_merge_plan,
    read_rollback_snapshot,
    rollback_mapping_from_snapshot,
    run_final_verification,
    validate_approved_plan,
    validate_source_plan_hash,
    verify_mapping_postconditions,
    verify_preexecution_state,
    write_rollback_snapshot,
)
from app.entity_migration import get_run


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _person(**overrides):
    doc = {
        "id": "PER-391", "name": "Ashok Ganapam", "email": "ashok@databeat.io", "aliases": [],
        "org": "DataBeat", "org_id": "ORG-001", "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": ["thread_anchor"], "note_link": None,
        "review_flag": False, "source": "gmail", "status": "active", "merged_into": None,
    }
    doc.update(overrides)
    return doc


def _seed_org(db, org_id="ORG-001", name="DataBeat", domain="databeat.io"):
    OrganizationRepository(db).upsert_by_key(
        {"id": org_id}, {"id": org_id, "name": name, "domain": domain, "aliases": [], "source": "gmail"}
    )


def _seed_ashok_duplicate(db, duplicate_id="PER-390", canonical_id="PER-391", shared_thread="thread_anchor"):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": canonical_id}, _person(id=canonical_id, open_threads=[shared_thread]))
    PersonRepository(db).upsert_by_key(
        {"id": duplicate_id},
        _person(id=duplicate_id, name="Ashok Ganapam", email=None, org_id=None, open_threads=[shared_thread]),
    )


def _source_plan(db):
    return generate_merge_plan(db)


def _approved(source_plan, *pairs):
    """Builds an approval artifact: every mapping from source_plan matching one of
    the given (duplicate, canonical) pairs gets approved=True; nothing else does."""
    wanted = set(pairs)
    out = []
    for m in source_plan:
        m = dict(m)
        m["approved"] = (m["duplicate_person_id"], m["canonical_person_id"]) in wanted
        out.append(m)
    return out


# --- 1/2: approval gating -----------------------------------------------------------------


def test_approved_plan_required(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    unapproved = _approved(source, ("NOTHING", "APPROVED"))  # matches nothing -- all stay False

    with pytest.raises(ValueError, match="no approved mappings"):
        execute_approved_plan(db, unapproved, source)


def test_unapproved_mapping_rejected_stays_untouched(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    result = execute_approved_plan(db, approved, source)

    assert result["completed_mappings"] == ["PER-390->PER-391"]
    assert PersonRepository(db).find_one({"id": "PER-389"})["status"] == "active"


# --- 3: plan hash validation ---------------------------------------------------------------


def test_source_plan_hash_mismatch_blocks_execution(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    with pytest.raises(ValueError, match="hash mismatch"):
        execute_approved_plan(db, approved, source, expected_source_plan_hash="deadbeef00000000")


def test_source_plan_hash_match_allows_execution(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    result = execute_approved_plan(db, approved, source, expected_source_plan_hash=_plan_hash(source))

    assert result["completed_mappings"] == ["PER-390->PER-391"]


# --- 21/22: blocked/manual candidates can never execute, even if approved=true is forced ---


def test_lowell_rejection_even_if_forcibly_approved(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-441"}, _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", org_id="ORG-002")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-443"}, _person(id="PER-443", name="Lowell Simpson", email="simpsonlowell@gmail.com", org="Outfront Media", org_id="ORG-002")
    )
    source = _source_plan(db)  # Lowell pair is BLOCKED_DATA_CONFLICT -- never in source plan at all
    # An attacker/mistaken approval artifact tries to smuggle it in anyway:
    forced = source + [{
        "duplicate_person_id": "PER-443", "canonical_person_id": "PER-441", "duplicate_name": "Lowell Simpson",
        "duplicate_email": "simpsonlowell@gmail.com", "duplicate_org_id": "ORG-002", "canonical_name": "Lowell Simpson",
        "canonical_email": "lowell@outfront.com", "canonical_org_id": "ORG-002", "confidence": "HIGH", "evidence": [],
        "classification": "SAFE_TO_REVIEW", "downstream_impact": {}, "approved": True,
    }]

    with pytest.raises(ValueError, match="does not exist in the Phase 18 source plan"):
        execute_approved_plan(db, forced, source)


def test_per608_rejection_needs_manual_review_never_executable(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key(
        {"id": "PER-513"}, _person(id="PER-513", name="Ash Allen", email="ash.allen@databeat.io", open_threads=["t1"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-608"}, _person(id="PER-608", name="Ash", email=None, org_id=None, open_threads=["t1"])
    )
    source = _source_plan(db)
    assert source == []  # NEEDS_MANUAL_REVIEW never enters the source plan

    forced = [{
        "duplicate_person_id": "PER-608", "canonical_person_id": "PER-513", "duplicate_name": "Ash",
        "duplicate_email": None, "duplicate_org_id": None, "canonical_name": "Ash Allen",
        "canonical_email": "ash.allen@databeat.io", "canonical_org_id": None, "confidence": "HIGH", "evidence": [],
        "classification": "SAFE_TO_REVIEW", "downstream_impact": {}, "approved": True,
    }]

    with pytest.raises(ValueError, match="does not exist in the Phase 18 source plan"):
        execute_approved_plan(db, forced, source)


# --- Part 3 structural validations ----------------------------------------------------------


def test_validate_approved_plan_rejects_blocked_source_classification(db):
    source = [{
        "duplicate_person_id": "PER-443", "canonical_person_id": "PER-441", "classification": "BLOCKED_DATA_CONFLICT",
    }]
    approved = [{**source[0], "approved": True}]

    errors = validate_approved_plan(approved, source)

    assert any("blocked/manual candidates" in e for e in errors)


def test_validate_approved_plan_rejects_self_mapping():
    source = [{"duplicate_person_id": "PER-1", "canonical_person_id": "PER-1", "classification": "SAFE_TO_REVIEW"}]
    approved = [{**source[0], "approved": True}]

    errors = validate_approved_plan(approved, source)

    assert any("self-mapping" in e for e in errors)


def test_validate_approved_plan_rejects_duplicate_with_multiple_targets():
    source = [
        {"duplicate_person_id": "PER-1", "canonical_person_id": "PER-2", "classification": "SAFE_TO_REVIEW"},
        {"duplicate_person_id": "PER-1", "canonical_person_id": "PER-3", "classification": "SAFE_TO_REVIEW"},
    ]
    approved = [{**m, "approved": True} for m in source]

    errors = validate_approved_plan(approved, source)

    assert any("multiple canonical targets" in e for e in errors)


def test_validate_approved_plan_rejects_circular_mapping():
    source = [
        {"duplicate_person_id": "PER-1", "canonical_person_id": "PER-2", "classification": "SAFE_TO_REVIEW"},
        {"duplicate_person_id": "PER-2", "canonical_person_id": "PER-1", "classification": "SAFE_TO_REVIEW"},
    ]
    approved = [{**m, "approved": True} for m in source]

    errors = validate_approved_plan(approved, source)

    assert any("circular/chained" in e for e in errors)


def test_validate_approved_plan_rejects_new_unexpected_mapping():
    source = [{"duplicate_person_id": "PER-1", "canonical_person_id": "PER-2", "classification": "SAFE_TO_REVIEW"}]
    approved = [{"duplicate_person_id": "PER-9", "canonical_person_id": "PER-2", "classification": "SAFE_TO_REVIEW", "approved": True}]

    errors = validate_approved_plan(approved, source)

    assert any("does not exist in the Phase 18 source plan" in e for e in errors)


def test_validate_approved_plan_accepts_clean_plan(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    assert validate_approved_plan(approved, source) == []


# --- Part 5 pre-execution state checks -------------------------------------------------------


def test_preexecution_state_blocks_when_duplicate_deleted(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    db.people.delete_one({"id": "PER-390"})

    errors = verify_preexecution_state(db, approved)

    assert any("no longer exists" in e for e in errors)


def test_preexecution_state_blocks_when_canonical_already_merged(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    canonical = PersonRepository(db).find_one({"id": "PER-391"})
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, {**canonical, "status": "merged", "merged_into": "PER-999"})

    errors = verify_preexecution_state(db, approved)

    assert any("cannot be a merge target" in e for e in errors)


def test_preexecution_state_allows_idempotent_rerun_already_merged_into_same_canonical(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, {**duplicate, "status": "merged", "merged_into": "PER-391"})

    errors = verify_preexecution_state(db, approved)

    assert errors == []


def test_preexecution_state_blocks_conflicting_prior_merge(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, {**duplicate, "status": "merged", "merged_into": "PER-999"})

    errors = verify_preexecution_state(db, approved)

    assert any("state conflict" in e for e in errors)
    with pytest.raises(ValueError, match="pre-execution state validation failed"):
        execute_approved_plan(db, approved, source)


# --- 4/23/24: canonical immutability, Ashok/John Toth execution ------------------------------


def test_execute_approved_plan_never_overwrites_canonical_identity(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    before = PersonRepository(db).find_one({"id": "PER-391"})

    execute_approved_plan(db, approved, source)

    after = PersonRepository(db).find_one({"id": "PER-391"})
    assert after["name"] == before["name"]
    assert after["email"] == before["email"]
    assert after["org_id"] == before["org_id"]
    assert after["status"] == "active"


def test_ashok_cluster_mapping_executes_and_converges_on_canonical(db):
    _seed_org(db)
    duplicate_ids = ["PER-386", "PER-390", "PER-400", "PER-405"]
    for i, pid in enumerate(duplicate_ids):
        PersonRepository(db).upsert_by_key(
            {"id": pid}, _person(id=pid, name="Ashok Ganapam", email=None, org_id=None, open_threads=[f"t{i+1}"])
        )
    # PER-391 must share a thread with each duplicate for name+thread-overlap
    # evidence to fire -- mirrors the real Atlas data shape.
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1", "t2", "t3", "t4"]))
    source = _source_plan(db)
    approved = _approved(source, *[(pid, "PER-391") for pid in ["PER-386", "PER-390", "PER-400", "PER-405"]])

    result = execute_approved_plan(db, approved, source)

    assert set(result["completed_mappings"]) == {f"{pid}->PER-391" for pid in ["PER-386", "PER-390", "PER-400", "PER-405"]}
    for pid in ["PER-386", "PER-390", "PER-400", "PER-405"]:
        assert PersonRepository(db).find_one({"id": pid})["merged_into"] == "PER-391"


def test_john_toth_mapping_executes(db):
    _seed_org(db, org_id="ORG-001", name="The 614 Group", domain="614group.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-384"}, _person(id="PER-384", name="John Toth", email="john.t@614group.com", org="The 614 Group", open_threads=["t1"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-382"}, _person(id="PER-382", name="John Toth", email=None, org="The 614 Group", org_id=None, open_threads=["t1"])
    )
    source = _source_plan(db)
    approved = _approved(source, ("PER-382", "PER-384"))

    result = execute_approved_plan(db, approved, source)

    assert result["completed_mappings"] == ["PER-382->PER-384"]
    assert PersonRepository(db).find_one({"id": "PER-382"})["status"] == "merged"
    assert PersonRepository(db).find_one({"id": "PER-384"})["email"] == "john.t@614group.com"


# --- 6/7/8: repointing correctness -----------------------------------------------------------


def test_scalar_and_list_repointing_with_dedup(db):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )
    ThreadRepository(db).upsert_by_key({"thread_id": "thread_anchor"}, {"thread_id": "thread_anchor", "person_ids": ["PER-390", "PER-391"], "org_ids": []})
    MeetingRepository(db).upsert_by_key({"id": "MTG-1"}, {"id": "MTG-1", "attendees": [], "person_ids": ["PER-390"], "org_id": None})
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    execute_approved_plan(db, approved, source)

    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-391"
    assert ThreadRepository(db).find_one({"thread_id": "thread_anchor"})["person_ids"] == ["PER-391"]
    assert MeetingRepository(db).find_one({"id": "MTG-1"})["person_ids"] == ["PER-391"]


# --- 9/10: knowledge safety --------------------------------------------------------------------


def test_knowledge_explicit_person_only_repointed(db):
    _seed_ashok_duplicate(db)
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "K-1"}, {"knowledge_id": "K-1", "thread_id": "thread_anchor", "person_id": "PER-390", "org_id": None, "subject_key": "renewal"}
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "K-2"}, {"knowledge_id": "K-2", "thread_id": "thread_anchor", "person_id": None, "org_id": None, "subject_key": "thread_anchor"}
    )
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    execute_approved_plan(db, approved, source)

    assert KnowledgeRepository(db).find_one({"knowledge_id": "K-1"})["person_id"] == "PER-391"
    thread_level = KnowledgeRepository(db).find_one({"knowledge_id": "K-2"})
    assert thread_level["person_id"] is None
    assert thread_level["subject_key"] == "thread_anchor"


# --- 11: calendar safety ------------------------------------------------------------------------


def test_calendar_attendee_conflict_blocks_mapping(db):
    _seed_ashok_duplicate(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "person_id": "PER-390", "org_id": None,
         "meeting_id": None, "event": {"attendees": ["external@customer.com"]}},
    )
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    result = execute_approved_plan(db, approved, source)

    assert result["blocked_mappings"]
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"})
    assert action["event"]["attendees"] == ["external@customer.com"]  # untouched


# --- 12: transaction behavior (documented determination, exercised on mongomock) ---------------


def test_execution_completes_without_requiring_mongo_transactions(db):
    # mongomock has no reliable multi-document transaction support; this test proves
    # execute_approved_plan works correctly without one, consistent with the
    # documented Part 9 determination (idempotent sequential writes, not transactions).
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    result = execute_approved_plan(db, approved, source)

    assert result["completed_mappings"] == ["PER-390->PER-391"]


# --- 13/14/15: checkpoint, resume, idempotency ---------------------------------------------------


def test_checkpoint_recorded_after_each_mapping(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"), ("PER-389", "PER-395"))

    result = execute_approved_plan(db, approved, source)

    run = get_run(db, result["run_id"])
    assert set(run["completed_mappings"]) == {"PER-390->PER-391", "PER-389->PER-395"}
    assert len(run["execution_log"]) == 2


def test_resume_does_not_reapply_completed_mappings(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"), ("PER-389", "PER-395"))

    first = execute_approved_plan(db, approved, source)
    # Simulate a second invocation resuming the same run -- nothing left to do.
    second = execute_approved_plan(db, approved, source, resume_run_id=first["run_id"])

    assert second["completed_mappings"] == first["completed_mappings"]


def test_idempotent_rerun_produces_zero_additional_changes(db):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))

    execute_approved_plan(db, approved, source)
    # Fresh run (no resume) against the same already-completed state.
    second = execute_approved_plan(db, approved, source)

    assert second["updated_counts"].get("commitments", 0) == 0
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-391"


# --- 16: abort on conflicting state -----------------------------------------------------------


def test_abort_on_conflicting_state_writes_nothing(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, {**duplicate, "status": "merged", "merged_into": "PER-999"})

    with pytest.raises(ValueError, match="pre-execution state validation failed"):
        execute_approved_plan(db, approved, source)

    # No migration_runs record should exist -- validation failed before start_run.
    assert db.migration_runs.count_documents({}) == 0


# --- 17/18: rollback ----------------------------------------------------------------------------


def test_rollback_restores_incomplete_mapping(db, tmp_path):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )
    approved = _approved(_source_plan(db), ("PER-390", "PER-391"))
    snapshot = capture_rollback_snapshot(db, approved, "migrun_test")
    path = str(tmp_path / "snapshot.json")
    write_rollback_snapshot(snapshot, path)

    execute_approved_plan(db, approved, _source_plan(db))
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-391"

    loaded = read_rollback_snapshot(path)
    result = rollback_mapping_from_snapshot(db, loaded, "PER-390", "PER-391")

    assert not result["needs_manual_recovery"]
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-390"
    assert PersonRepository(db).find_one({"id": "PER-390"})["status"] == "active"


def test_rollback_refuses_to_overwrite_independently_modified_document(db, tmp_path):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )
    approved = _approved(_source_plan(db), ("PER-390", "PER-391"))
    snapshot = capture_rollback_snapshot(db, approved, "migrun_test")

    execute_approved_plan(db, approved, _source_plan(db))
    # Something else independently changes the document after migration completed.
    changed = CommitmentRepository(db).find_one({"id": "COM-1"})
    CommitmentRepository(db).upsert_by_key({"id": "COM-1"}, {**changed, "status": "closed"})

    result = rollback_mapping_from_snapshot(db, snapshot, "PER-390", "PER-391")

    assert result["needs_manual_recovery"]
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["status"] == "closed"  # untouched


# --- 19/20: post-mapping and final verification -------------------------------------------------


def test_verify_mapping_postconditions_passes_after_clean_execution(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"))
    mapping = next(m for m in approved if m["approved"])

    execute_approved_plan(db, approved, source)

    assert verify_mapping_postconditions(db, mapping) == []


def test_verify_mapping_postconditions_flags_lingering_reference(db):
    _seed_ashok_duplicate(db)
    source = _source_plan(db)
    mapping = next(m for m in source if m["duplicate_person_id"] == "PER-390")
    # Simulate a botched execution: person retired but a reference never repointed.
    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, {**duplicate, "status": "merged", "merged_into": "PER-391"})
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "e:1", "made_on": "2026-01-01", "status": "open"},
    )

    errors = verify_mapping_postconditions(db, mapping)

    assert any("still references duplicate" in e for e in errors)


def test_run_final_verification_aggregates_across_all_approved_mappings(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))
    source = _source_plan(db)
    approved = _approved(source, ("PER-390", "PER-391"), ("PER-389", "PER-395"))

    execute_approved_plan(db, approved, source)
    report = run_final_verification(db, approved)

    assert report["passed"] is True
    assert report["total_errors"] == 0
