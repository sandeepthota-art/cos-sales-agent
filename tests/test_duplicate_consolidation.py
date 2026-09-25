# tests/test_duplicate_consolidation.py
"""Phase 18: controlled, approval-gated Person consolidation. All tests use isolated
mongomock fixtures only -- this phase never executes anything against production
Atlas; the only Atlas interaction anywhere in Phase 18 is a read-only dry-run plan
generation, run separately and reported outside this test suite.
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    KnowledgeRepository,
    MeetingRepository,
    MigrationRunRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ThreadRepository,
)
from app.duplicate_consolidation import (
    compute_unique_plan_impact,
    execute_merge_plan,
    generate_merge_plan,
    read_merge_plan,
    write_merge_plan,
)
from app.entity_migration import get_run, start_run


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
    PersonRepository(db).upsert_by_key(
        {"id": canonical_id}, _person(id=canonical_id, open_threads=[shared_thread])
    )
    PersonRepository(db).upsert_by_key(
        {"id": duplicate_id},
        _person(id=duplicate_id, name="Ashok Ganapam", email=None, org_id=None, open_threads=[shared_thread]),
    )


def _plan_for(db, duplicate_id="PER-390", canonical_id="PER-391"):
    plan = generate_merge_plan(db)
    return next(m for m in plan if m["duplicate_person_id"] == duplicate_id and m["canonical_person_id"] == canonical_id)


def _approved_plan(db, duplicate_id="PER-390", canonical_id="PER-391"):
    mapping = dict(_plan_for(db, duplicate_id, canonical_id))
    mapping["approved"] = True
    return [mapping]


# --- Part A: plan generation ------------------------------------------------------------


def test_generate_merge_plan_includes_only_safe_to_review(db):
    _seed_ashok_duplicate(db)
    plan = generate_merge_plan(db)
    assert len(plan) == 1
    assert plan[0]["classification"] == "SAFE_TO_REVIEW"


def test_generate_merge_plan_excludes_blocked_data_conflict(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-441"}, _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", org_id="ORG-002")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-443"}, _person(id="PER-443", name="Lowell Simpson", email="simpsonlowell@gmail.com", org="Outfront Media", org_id="ORG-002")
    )
    plan = generate_merge_plan(db)
    assert plan == []


def test_generate_merge_plan_excludes_blocked_ambiguous(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    for pid, email in [("PER-001", "lowell1@outfront.com"), ("PER-002", "lowell2@outfront.com"), ("PER-003", None)]:
        PersonRepository(db).upsert_by_key(
            {"id": pid}, _person(id=pid, name="Lowell Simpson", email=email, org="Outfront Media", org_id="ORG-002" if email else None)
        )
    plan = generate_merge_plan(db)
    assert all(m["duplicate_person_id"] != "PER-003" for m in plan)


def test_generate_merge_plan_excludes_needs_manual_review(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key(
        {"id": "PER-513"}, _person(id="PER-513", name="Ash Allen", email="ash.allen@databeat.io", open_threads=["thread_shared"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-608"}, _person(id="PER-608", name="Ash", email=None, org_id=None, open_threads=["thread_shared"])
    )
    plan = generate_merge_plan(db)
    assert plan == []


def test_generate_merge_plan_defaults_approved_false(db):
    _seed_ashok_duplicate(db)
    plan = generate_merge_plan(db)
    assert all(m["approved"] is False for m in plan)


def test_generate_merge_plan_never_sets_approved_true(db):
    # Part M: a plan generator that ever defaults to True would silently turn
    # SAFE_TO_REVIEW into approval -- this must never happen, checked across many
    # candidates at once, not just the single-pair happy path above.
    _seed_org(db)
    for i in range(5):
        canonical_id, duplicate_id = f"PER-{100 + i}", f"PER-{200 + i}"
        PersonRepository(db).upsert_by_key({"id": canonical_id}, _person(id=canonical_id, name=f"Person {i}", email=f"p{i}@databeat.io", open_threads=[f"t{i}"]))
        PersonRepository(db).upsert_by_key({"id": duplicate_id}, _person(id=duplicate_id, name=f"Person {i}", email=None, org_id=None, open_threads=[f"t{i}"]))
    plan = generate_merge_plan(db)
    assert len(plan) == 5
    assert not any(m["approved"] for m in plan)


def test_generate_merge_plan_includes_downstream_impact(db):
    _seed_ashok_duplicate(db)
    plan = generate_merge_plan(db)
    assert "downstream_impact" in plan[0]
    assert "threads" in plan[0]["downstream_impact"]


# --- Part B: unique impact across the plan ------------------------------------------------


def test_compute_unique_plan_impact_counts_shared_document_once(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-400"}, _person(id="PER-400", email=None, org_id=None, open_threads=["t1"]))
    # A single thread references BOTH duplicates -- consolidating either mapping
    # touches this same document; the unique count must not double it.
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "person_ids": ["PER-390", "PER-400"], "org_ids": ["ORG-001"]})

    plan = generate_merge_plan(db)
    assert {m["duplicate_person_id"] for m in plan} == {"PER-390", "PER-400"}
    impact = compute_unique_plan_impact(db, plan)
    assert impact["per_collection"]["threads"]["unique_documents_affected"] == 1


def test_compute_unique_plan_impact_detects_conflicting_mappings(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-500"}, _person(id="PER-500", name="Someone Else", email="someone@databeat.io", open_threads=["t2"]))
    plan = [
        {"duplicate_person_id": "PER-390", "canonical_person_id": "PER-391", "classification": "SAFE_TO_REVIEW", "approved": False},
        {"duplicate_person_id": "PER-390", "canonical_person_id": "PER-500", "classification": "SAFE_TO_REVIEW", "approved": False},
    ]
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "person_ids": ["PER-390"], "org_ids": []})

    impact = compute_unique_plan_impact(db, plan)
    assert any(c["collection"] == "threads" for c in impact["conflicting_mappings"])


def test_compute_unique_plan_impact_tolerates_explicit_none_list_field(db):
    # Real Atlas data can store a list-valued field (e.g. Meeting.person_ids) as an
    # explicit None rather than an empty list or a missing key -- found live via the
    # Phase 18 real-Atlas dry-run, which crashed with TypeError before this fix.
    _seed_ashok_duplicate(db)
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-1"}, {"id": "MTG-1", "attendees": [], "person_ids": None, "org_id": None}
    )
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-1"}, {"id": "PRJ-1", "project": "renewal", "person_ids": None, "org_id": None}
    )

    plan = generate_merge_plan(db)
    impact = compute_unique_plan_impact(db, plan)  # must not raise

    assert impact["per_collection"]["meetings"]["unique_documents_affected"] == 0
    assert impact["per_collection"]["projects"]["unique_documents_affected"] == 0


def test_compute_unique_plan_impact_already_canonical_not_double_counted(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1", "t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    ThreadRepository(db).upsert_by_key({"thread_id": "t1"}, {"thread_id": "t1", "person_ids": ["PER-390"], "org_ids": []})
    ThreadRepository(db).upsert_by_key({"thread_id": "t2"}, {"thread_id": "t2", "person_ids": ["PER-391"], "org_ids": []})

    plan = generate_merge_plan(db)
    impact = compute_unique_plan_impact(db, plan)
    counts = impact["per_collection"]["threads"]
    assert counts["requires_repoint"] == 1
    assert counts["already_canonical"] == 1
    assert counts["unique_documents_affected"] == 2


# --- Part G: approval-artifact I/O ---------------------------------------------------------


def test_write_and_read_merge_plan_round_trip(db, tmp_path):
    _seed_ashok_duplicate(db)
    plan = generate_merge_plan(db)
    path = str(tmp_path / "plan.json")

    write_merge_plan(plan, path)
    loaded = read_merge_plan(path)

    assert loaded == plan


# --- Parts H/I: approval-gated execution ----------------------------------------------------


def test_execute_merge_plan_raises_if_no_approved_mappings(db):
    _seed_ashok_duplicate(db)
    plan = generate_merge_plan(db)  # all approved: False

    with pytest.raises(ValueError, match="no approved mappings"):
        execute_merge_plan(db, plan)


def test_execute_merge_plan_ignores_unapproved_mappings_in_same_plan(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))

    plan = generate_merge_plan(db)
    for m in plan:
        m["approved"] = m["duplicate_person_id"] == "PER-390"  # only approve one of the two

    result = execute_merge_plan(db, plan)

    assert result["completed_mappings"] == ["PER-390->PER-391"]
    assert PersonRepository(db).find_one({"id": "PER-389"})["status"] == "active"


# --- Parts D/E: downstream repointing --------------------------------------------------------


def test_execute_merge_plan_repoints_scalar_person_id_field(db):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "follow up", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "email:1", "made_on": "2026-01-01", "status": "open"},
    )

    result = execute_merge_plan(db, _approved_plan(db))

    assert result["updated_counts"]["commitments"] == 1
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-391"


def test_execute_merge_plan_repoints_list_field_with_dedup(db):
    _seed_ashok_duplicate(db)
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "t1"}, {"thread_id": "t1", "person_ids": ["PER-390", "PER-391"], "org_ids": ["ORG-001"]}
    )
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-1"}, {"id": "MTG-1", "attendees": ["Ashok"], "person_ids": ["PER-390"], "org_id": None}
    )

    execute_merge_plan(db, _approved_plan(db))

    assert ThreadRepository(db).find_one({"thread_id": "t1"})["person_ids"] == ["PER-391"]
    assert MeetingRepository(db).find_one({"id": "MTG-1"})["person_ids"] == ["PER-391"]


def test_execute_merge_plan_never_overwrites_canonical_person_fields(db):
    _seed_ashok_duplicate(db)
    before = PersonRepository(db).find_one({"id": "PER-391"})

    execute_merge_plan(db, _approved_plan(db))

    after = PersonRepository(db).find_one({"id": "PER-391"})
    assert after["email"] == before["email"]
    assert after["org_id"] == before["org_id"]
    assert after["name"] == before["name"]
    assert after["status"] == "active"
    assert after["merged_into"] is None


def test_execute_merge_plan_marks_duplicate_status_merged_and_merged_into(db):
    _seed_ashok_duplicate(db)

    execute_merge_plan(db, _approved_plan(db))

    duplicate = PersonRepository(db).find_one({"id": "PER-390"})
    assert duplicate["status"] == "merged"
    assert duplicate["merged_into"] == "PER-391"
    assert duplicate["id"] == "PER-390"  # never physically deleted


def test_execute_merge_plan_only_repoints_explicit_knowledge_person_id(db):
    _seed_ashok_duplicate(db)
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "K-1"}, {"knowledge_id": "K-1", "thread_id": "thread_anchor", "person_id": "PER-390", "org_id": None, "subject_key": "renewal"}
    )

    execute_merge_plan(db, _approved_plan(db))

    assert KnowledgeRepository(db).find_one({"knowledge_id": "K-1"})["person_id"] == "PER-391"


def test_execute_merge_plan_leaves_thread_level_knowledge_untouched(db):
    _seed_ashok_duplicate(db)
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "K-2"},
        {"knowledge_id": "K-2", "thread_id": "thread_anchor", "person_id": None, "org_id": None, "subject_key": "thread_anchor"},
    )

    execute_merge_plan(db, _approved_plan(db))

    item = KnowledgeRepository(db).find_one({"knowledge_id": "K-2"})
    assert item["person_id"] is None
    assert item["subject_key"] == "thread_anchor"


# --- Part F: calendar safety --------------------------------------------------------------


def test_execute_merge_plan_blocks_mapping_with_external_calendar_attendees(db):
    _seed_ashok_duplicate(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "person_id": "PER-390", "org_id": None,
            "meeting_id": None, "event": {"attendees": ["external@customer.com"]},
        },
    )

    result = execute_merge_plan(db, _approved_plan(db))

    assert result["blocked_mappings"]
    assert result["updated_counts"].get("calendar_actions", 0) == 0
    # The blocked mapping's other downstream collections are untouched too -- the
    # whole mapping is blocked, never partially applied.
    assert PersonRepository(db).find_one({"id": "PER-390"})["status"] == "active"


def test_execute_merge_plan_repoints_calendar_action_without_external_attendees(db):
    _seed_ashok_duplicate(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_anchor", "meeting_fingerprint": "fp1", "person_id": "PER-390", "org_id": None,
            "meeting_id": None, "event": {"attendees": []},
        },
    )

    result = execute_merge_plan(db, _approved_plan(db))

    assert result["updated_counts"]["calendar_actions"] == 1
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_anchor", "meeting_fingerprint": "fp1"})
    assert action["person_id"] == "PER-391"
    assert action["event"]["attendees"] == []


# --- Parts J/K: checkpointing, resume, idempotency -----------------------------------------


def test_execute_merge_plan_is_idempotent_on_rerun(db):
    _seed_ashok_duplicate(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-1"},
        {"id": "COM-1", "what": "follow up", "class": "mine", "owed_by": "Ashok", "owed_to": None,
         "person_id": "PER-390", "org_id": None, "source_record": "email:1", "made_on": "2026-01-01", "status": "open"},
    )
    plan = _approved_plan(db)

    first = execute_merge_plan(db, plan)
    second = execute_merge_plan(db, plan)  # fresh run, no resume -- everything already repointed

    assert first["updated_counts"]["commitments"] == 1
    assert second["updated_counts"].get("commitments", 0) == 0
    assert CommitmentRepository(db).find_one({"id": "COM-1"})["person_id"] == "PER-391"


def test_execute_merge_plan_resumes_without_reapplying_completed_mappings(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, _person(id="PER-390", email=None, org_id=None, open_threads=["t1"]))
    PersonRepository(db).upsert_by_key({"id": "PER-395"}, _person(id="PER-395", name="Jason Greene", email="jason@databeat.io", open_threads=["t2"]))
    PersonRepository(db).upsert_by_key({"id": "PER-389"}, _person(id="PER-389", name="Jason", email=None, org_id=None, open_threads=["t2"]))
    plan = generate_merge_plan(db)
    for m in plan:
        m["approved"] = True

    run_id = start_run(db, "duplicate-consolidation")
    run = get_run(db, run_id)
    MigrationRunRepository(db).upsert_by_key({"run_id": run_id}, {**run, "completed_mappings": ["PER-390->PER-391"]})

    result = execute_merge_plan(db, plan, resume_run_id=run_id)

    assert "PER-390->PER-391" in result["completed_mappings"]
    assert "PER-389->PER-395" in result["completed_mappings"]
    # PER-390 was pre-marked complete before this call -- it must not have been
    # touched again by this resumed run (its person status change already existed
    # from whatever earlier run completed it; here nothing marks it, so it stays
    # untouched at "active" from _seed_ashok_duplicate-style setup).
    assert PersonRepository(db).find_one({"id": "PER-389"})["status"] == "merged"


def test_execute_merge_plan_refuses_resume_with_different_plan_hash(db):
    _seed_ashok_duplicate(db)
    plan = _approved_plan(db)
    run_id = execute_merge_plan(db, plan)["run_id"]

    other_plan = [{**plan[0], "duplicate_person_id": "PER-999", "canonical_person_id": "PER-391"}]

    with pytest.raises(ValueError, match="different plan"):
        execute_merge_plan(db, other_plan, resume_run_id=run_id)


# --- Part L: special-case validation ---------------------------------------------------------


def test_ashok_cluster_all_safe_to_review_and_in_plan(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t0"]))
    duplicate_ids = ["PER-386", "PER-390", "PER-400", "PER-405"]
    for i, pid in enumerate(duplicate_ids):
        PersonRepository(db).upsert_by_key(
            {"id": pid}, _person(id=pid, name="Ashok Ganapam" if i % 2 else "Ashok", email=None, org_id=None, open_threads=[f"t{i}"])
        )
        ThreadRepository(db).upsert_by_key({"thread_id": f"t{i}"}, {"thread_id": f"t{i}", "person_ids": [], "org_ids": []})
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t0", "t1", "t2", "t3"]))

    plan = generate_merge_plan(db)
    plan_pairs = {(m["duplicate_person_id"], m["canonical_person_id"]) for m in plan}

    for pid in duplicate_ids:
        assert (pid, "PER-391") in plan_pairs


def test_lowell_and_short_name_pairs_never_appear_approved_in_plan(db):
    _seed_org(db, org_id="ORG-002", name="Outfront Media", domain="outfront.com")
    PersonRepository(db).upsert_by_key(
        {"id": "PER-441"}, _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", org_id="ORG-002", open_threads=["t1"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-443"}, _person(id="PER-443", name="Lowell Simpson", email="simpsonlowell@gmail.com", org="Outfront Media", org_id="ORG-002", open_threads=["t1"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-513"}, _person(id="PER-513", name="Ash Allen", email="ash.allen@databeat.io", open_threads=["t2"])
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-608"}, _person(id="PER-608", name="Ash", email=None, org_id=None, open_threads=["t2"])
    )

    plan = generate_merge_plan(db)
    plan_duplicate_ids = {m["duplicate_person_id"] for m in plan}

    assert "PER-441" not in plan_duplicate_ids
    assert "PER-443" not in plan_duplicate_ids
    assert "PER-608" not in plan_duplicate_ids
    assert all(not m["approved"] for m in plan)


# --- Phase 19.1 Part 13: consolidation compatibility with a lifecycle-aware plan generator --


def test_generate_merge_plan_never_proposes_an_already_merged_duplicate(db):
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"},
        _person(id="PER-390", email=None, org_id=None, open_threads=["t1"], status="merged", merged_into="PER-391"),
    )

    plan = generate_merge_plan(db)

    assert plan == []


def test_generate_merge_plan_never_proposes_a_merged_person_as_canonical_target(db):
    _seed_org(db)
    # PER-386 is itself already merged into PER-391 -- a brand-new orphan matching
    # PER-386's old name/org must never be proposed AGAINST PER-386.
    PersonRepository(db).upsert_by_key(
        {"id": "PER-386"},
        _person(id="PER-386", email="ashok.old@databeat.io", status="merged", merged_into="PER-391", open_threads=["t_old"]),
    )
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-900"}, _person(id="PER-900", name="Ashok Ganapam", email=None, org_id=None, open_threads=["t1"])
    )

    plan = generate_merge_plan(db)

    canonical_targets = {m["canonical_person_id"] for m in plan}
    assert "PER-386" not in canonical_targets
    if plan:
        assert canonical_targets == {"PER-391"}


def test_generate_merge_plan_does_not_resurface_a_completed_phase19_style_mapping(db):
    # Mirrors exactly what Phase 19 execution leaves behind for one mapping.
    _seed_org(db)
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person(id="PER-391", open_threads=["t1"]))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"},
        _person(id="PER-390", email=None, org_id=None, open_threads=["t1"], status="merged", merged_into="PER-391"),
    )

    plan = generate_merge_plan(db)

    assert all(m["duplicate_person_id"] != "PER-390" for m in plan)
