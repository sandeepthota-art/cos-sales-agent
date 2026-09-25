# tests/test_entity_migration.py
"""All tests here use isolated mongomock fixtures only -- never production Atlas,
per this migration phase's explicit isolation rule."""
from datetime import datetime, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entity_migration import (
    _is_malformed_email,
    complete_run,
    get_run,
    start_run,
    stage_canonical_references_dry_run,
    stage_canonical_references_execute,
    stage_organizations_dry_run,
    stage_organizations_execute,
    verify_stage_canonical_references,
    verify_stage_organizations,
)
from app.entity_reconciliation import find_duplicate_candidates

_NOW = datetime(2026, 9, 13, 10, 30, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _person(**overrides):
    doc = {
        "id": "PER-001", "name": "Ashok Ganapam", "email": "ashok@databeat.io", "aliases": [],
        "org": "DataBeat", "org_id": None, "type": None, "goal_pillar": None, "role_in_pillar": None,
        "tier": None, "voice_register": None, "last_inbound": None, "last_outbound": None,
        "reports_to": None, "open_threads": ["thread_1"], "note_link": None,
        "review_flag": False, "source": "gmail",
    }
    doc.update(overrides)
    return doc


# --- 1-5: organizations stage ---------------------------------------------------------


def test_1_organization_created_by_domain(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    result = stage_organizations_execute(db)
    assert result["people_updated"] == 1
    orgs = OrganizationRepository(db).find_many({})
    assert len(orgs) == 1
    assert orgs[0]["domain"] == "databeat.io"
    person = PersonRepository(db).find_one({"id": "PER-001"})
    assert person["org_id"] == orgs[0]["id"]


def test_2_organization_reused_by_domain(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-002"}, _person(id="PER-002", email="someone@databeat.io", org_id=None)
    )
    stage_organizations_execute(db)
    assert OrganizationRepository(db).find_many({}).__len__() == 1
    p1 = PersonRepository(db).find_one({"id": "PER-001"})
    p2 = PersonRepository(db).find_one({"id": "PER-002"})
    assert p1["org_id"] == p2["org_id"]


def test_3_no_organization_for_no_email_person(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-003"}, _person(id="PER-003", email=None, org_id=None)
    )
    result = stage_organizations_execute(db)
    assert result["people_updated"] == 0
    assert result["people_skipped"] == 1
    assert OrganizationRepository(db).find_many({}).__len__() == 0
    assert PersonRepository(db).find_one({"id": "PER-003"}).get("org_id") is None


def test_4_person_org_id_assignment_preserves_free_text_org(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(org="Databeat.io"))
    stage_organizations_execute(db)
    person = PersonRepository(db).find_one({"id": "PER-001"})
    assert person["org_id"] is not None
    assert person["org"] == "Databeat.io"  # untouched, exactly as stored before


def test_5_idempotent_organization_migration(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    first = stage_organizations_execute(db)
    person_after_first = PersonRepository(db).find_one({"id": "PER-001"})
    orgs_after_first = OrganizationRepository(db).find_many({})

    second = stage_organizations_execute(db)
    person_after_second = PersonRepository(db).find_one({"id": "PER-001"})
    orgs_after_second = OrganizationRepository(db).find_many({})

    assert first["people_updated"] == 1
    assert second["people_updated"] == 0  # already linked -- skipped, not re-updated
    assert person_after_first == person_after_second
    assert orgs_after_first == orgs_after_second


# --- 6-13: canonical-references stage --------------------------------------------------


def _seed_thread_with_person(db, thread_id="thread_1", person_id="PER-001"):
    PersonRepository(db).upsert_by_key(
        {"id": person_id}, _person(id=person_id, open_threads=[thread_id], org_id="ORG-001")
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": thread_id}, {"thread_id": thread_id, "normalized_subject": "x", "message_ids": ["m1"]}
    )


def test_6_canonical_thread_references(db):
    _seed_thread_with_person(db)
    stage_canonical_references_execute(db)
    thread = ThreadRepository(db).find_one({"thread_id": "thread_1"})
    assert thread["person_ids"] == ["PER-001"]
    assert thread["org_ids"] == ["ORG-001"]


def test_7_canonical_commitment_references(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    result = stage_canonical_references_execute(db)
    assert result["updated_counts"]["commitments"] == 1
    commitment = CommitmentRepository(db).find_one({"id": "COM-001"})
    assert commitment["person_id"] == "PER-001"
    assert commitment["org_id"] == "ORG-001"
    assert commitment["owed_to"] == "Ashok Ganapam"  # legacy field untouched


def test_8_canonical_meeting_references(db):
    _seed_thread_with_person(db)
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"},
        {
            "id": "MTG-001", "date": _NOW, "attendees": ["Ashok Ganapam"], "person_ids": [],
            "org_id": None, "actionable": True, "thread_id": "thread_1",
        },
    )
    stage_canonical_references_execute(db)
    meeting = MeetingRepository(db).find_one({"id": "MTG-001"})
    assert meeting["person_ids"] == ["PER-001"]
    assert meeting["org_id"] == "ORG-001"
    assert meeting["attendees"] == ["Ashok Ganapam"]


def test_9_follow_up_inherits_from_commitment(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-001"}, {"id": "FU-001", "commitment_id": "COM-001", "thread_id": "thread_1", "person_id": None, "org_id": None}
    )
    stage_canonical_references_execute(db)
    follow_up = FollowUpRepository(db).find_one({"id": "FU-001"})
    commitment = CommitmentRepository(db).find_one({"id": "COM-001"})
    assert follow_up["person_id"] == commitment["person_id"] == "PER-001"
    assert follow_up["org_id"] == commitment["org_id"] == "ORG-001"


def test_10_project_references(db):
    _seed_thread_with_person(db)
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"},
        {
            "message_id": "m1", "thread_id": "thread_1", "subject": "x", "timestamp": _NOW,
            "entities_referenced": {"projects": ["PRJ-001"]},
        },
    )
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-001"},
        {
            "id": "PRJ-001", "project": "Rollout", "entity": "DataBeat", "goal_pillar": "Sales",
            "collaborators": [], "person_ids": [], "org_id": None, "source": "gmail",
        },
    )
    stage_canonical_references_execute(db)
    project = ProjectRepository(db).find_one({"id": "PRJ-001"})
    assert "PER-001" in project["person_ids"]
    assert project["org_id"] == "ORG-001"
    assert project["collaborators"] == []


def test_11_knowledge_references(db):
    _seed_thread_with_person(db)
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k1"},
        {
            "knowledge_id": "k1", "thread_id": "thread_1", "subject_key": "ashok_ganapam",
            "predicate": "prefers", "fact_key": "mornings", "current_value": "mornings",
            "history": [], "source_emails": ["m1"], "basis": "stated",
            "first_seen_at": _NOW, "last_confirmed_at": _NOW, "confidence": 0.8, "status": "active",
        },
    )
    stage_canonical_references_execute(db)
    item = KnowledgeRepository(db).find_one({"knowledge_id": "k1"})
    assert item["person_id"] == "PER-001"


def test_12_reply_draft_references(db):
    _seed_thread_with_person(db)
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"},
        {
            "message_id": "m1", "thread_id": "thread_1", "subject": "x", "timestamp": _NOW,
            "from": {"name": "Ashok Ganapam", "email": "ashok@databeat.io"},
            "entities_referenced": {"people": ["PER-001"]},
        },
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "m1"},
        {
            "reply_id": "reply_m1", "thread_id": "thread_1", "source_email_id": "m1",
            "status": "awaiting_approval", "draft": {"subject": "Re: x", "body": "..."},
            "person_id": None, "org_id": None, "created_by": "sales_agent", "created_at": _NOW,
        },
    )
    stage_canonical_references_execute(db)
    draft = ReplyDraftRepository(db).find_one({"source_email_id": "m1"})
    assert draft["person_id"] == "PER-001"
    assert draft["org_id"] == "ORG-001"


def test_12b_reply_draft_references_redirect_a_merged_sender_to_canonical(db):
    # Phase 20.1: this stage is idempotent and rerunnable (e.g. via --resume, or
    # reprocessing a backlog) -- a rerun after a future consolidation must never
    # backfill person_id with a Person that's since been merged.
    _seed_thread_with_person(db)  # PER-001, active, email ashok@databeat.io
    PersonRepository(db).upsert_by_key(
        {"id": "PER-002"},
        _person(id="PER-002", email="ashok.old@databeat.io", status="merged", merged_into="PER-001"),
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "m1"},
        {
            "message_id": "m1", "thread_id": "thread_1", "subject": "x", "timestamp": _NOW,
            "from": {"name": "Ashok Ganapam", "email": "ashok.old@databeat.io"},
            "entities_referenced": {"people": ["PER-001"]},
        },
    )
    ReplyDraftRepository(db).upsert_by_key(
        {"source_email_id": "m1"},
        {
            "reply_id": "reply_m1", "thread_id": "thread_1", "source_email_id": "m1",
            "status": "awaiting_approval", "draft": {"subject": "Re: x", "body": "..."},
            "person_id": None, "org_id": None, "created_by": "sales_agent", "created_at": _NOW,
        },
    )

    stage_canonical_references_execute(db)

    draft = ReplyDraftRepository(db).find_one({"source_email_id": "m1"})
    assert draft["person_id"] == "PER-001"
    assert draft["person_id"] != "PER-002"


def test_13_calendar_action_metadata(db):
    _seed_thread_with_person(db)
    MeetingRepository(db).upsert_by_key(
        {"id": "MTG-001"},
        {"id": "MTG-001", "date": _NOW, "attendees": [], "person_ids": [], "org_id": None, "actionable": True, "thread_id": "thread_1"},
    )
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_1", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_1", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": []},
            "actor_type": "authenticated_user", "reason": None, "person_id": None, "org_id": None, "meeting_id": None,
        },
    )
    stage_canonical_references_execute(db)
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_1", "meeting_fingerprint": "fp1"})
    assert action["person_id"] == "PER-001"
    assert action["org_id"] == "ORG-001"
    assert action["meeting_id"] == "MTG-001"


# --- 14: calendar attendees safety -------------------------------------------------------


def test_14_calendar_attendees_remain_empty_after_migration(db):
    _seed_thread_with_person(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_1", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_1", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": []},
            "actor_type": "authenticated_user", "reason": None, "person_id": None, "org_id": None, "meeting_id": None,
        },
    )
    stage_canonical_references_execute(db)
    action = CalendarActionRepository(db).find_one({"thread_id": "thread_1", "meeting_fingerprint": "fp1"})
    assert action["event"]["attendees"] == []


def test_14b_calendar_action_with_pre_existing_attendees_raises_instead_of_silently_writing(db):
    # A real safety anomaly (should never exist given the app's own validator, but this
    # migration must never silently proceed past it either).
    _seed_thread_with_person(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_1", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_1", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": ["intruder@example.com"]},
            "actor_type": "authenticated_user", "reason": None, "person_id": None, "org_id": None, "meeting_id": None,
        },
    )
    with pytest.raises(RuntimeError, match="safety anomaly"):
        stage_canonical_references_execute(db)


# --- 15: ambiguous matches are skipped ---------------------------------------------------


def test_15_ambiguous_person_matches_are_skipped(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"}, _person(id="PER-001", name="Ashok Ganapam", email="ashok@databeat.io", open_threads=["thread_1"], org_id="ORG-001")
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-002"}, _person(id="PER-002", name="Ashok Kumar", email="ashok.k@databeat.io", open_threads=["thread_1"], org_id="ORG-001")
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "thread_1"}, {"thread_id": "thread_1", "normalized_subject": "x", "message_ids": ["m1"]}
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    dry = stage_canonical_references_dry_run(db)
    commitment_proposal = next(p for p in dry["proposals"] if p["collection"] == "commitments")
    assert commitment_proposal["status"] == "SKIPPED_AMBIGUOUS"

    stage_canonical_references_execute(db)
    assert CommitmentRepository(db).find_one({"id": "COM-001"}).get("person_id") is None


# --- 16: duplicate candidates never merged ------------------------------------------------


def test_16_duplicate_candidates_are_reported_only_never_merged(db):
    anchor = _person(id="PER-391", open_threads=["thread_anchor"])
    orphan = _person(id="PER-390", email=None, org_id=None, open_threads=["thread_anchor"])
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, anchor)
    PersonRepository(db).upsert_by_key({"id": "PER-390"}, orphan)

    candidates = find_duplicate_candidates(PersonRepository(db).find_many({}))
    assert any(c["source_person_id"] == "PER-390" and c["candidate_canonical_person_id"] == "PER-391" for c in candidates)

    # Nothing about calling find_duplicate_candidates changes the underlying records.
    assert PersonRepository(db).find_many({}).__len__() == 2
    assert PersonRepository(db).find_one({"id": "PER-390"}) is not None
    assert PersonRepository(db).find_one({"id": "PER-391"}) is not None


# --- 17: dry-run performs zero writes -----------------------------------------------------


def test_17_organizations_dry_run_performs_zero_writes(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    before = PersonRepository(db).find_many({})

    stage_organizations_dry_run(db)

    after = PersonRepository(db).find_many({})
    assert before == after
    assert OrganizationRepository(db).find_many({}) == []


def test_17b_canonical_references_dry_run_performs_zero_writes(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    before_thread = ThreadRepository(db).find_one({"thread_id": "thread_1"})
    before_commitment = CommitmentRepository(db).find_one({"id": "COM-001"})

    stage_canonical_references_dry_run(db)

    assert ThreadRepository(db).find_one({"thread_id": "thread_1"}) == before_thread
    assert CommitmentRepository(db).find_one({"id": "COM-001"}) == before_commitment


# --- 18: re-running migration creates zero duplicate changes -----------------------------


def test_18_rerunning_canonical_references_migration_is_idempotent(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    first = stage_canonical_references_execute(db)
    state_after_first = CommitmentRepository(db).find_one({"id": "COM-001"})

    second = stage_canonical_references_execute(db)
    state_after_second = CommitmentRepository(db).find_one({"id": "COM-001"})

    assert first["updated_counts"]["commitments"] == 1
    assert second["updated_counts"].get("commitments", 0) == 0
    assert state_after_first == state_after_second


# --- 19: verification detects invalid references ------------------------------------------


def test_19_verification_detects_invalid_org_id(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"}, _person(org_id="ORG-DOES-NOT-EXIST")
    )
    result = verify_stage_organizations(db)
    assert result["passed"] is False
    assert any("does not reference an existing Organization" in f for f in result["failures"])


def test_19b_verification_detects_missing_org_id(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(org_id=None))
    result = verify_stage_organizations(db)
    assert result["passed"] is False
    assert any("has a valid email" in f for f in result["failures"])


def test_19c_verification_passes_on_correctly_migrated_data(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(org_id=None))
    stage_organizations_execute(db)
    result = verify_stage_organizations(db)
    assert result["passed"] is True
    assert result["failures"] == []


# --- 20: migration can resume after interruption -------------------------------------------


def test_20_migration_resumes_after_interruption_without_reprocessing(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(id="PER-001"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-002"}, _person(id="PER-002", email="second@databeat.io", org_id=None)
    )

    first = stage_organizations_execute(db, limit=1)  # simulate an interrupted run
    run = get_run(db, first["run_id"])
    assert run["status"] == "completed"  # limit=1 completes cleanly after one record
    assert run["last_processed_id"] == "PER-001"
    assert PersonRepository(db).find_one({"id": "PER-002"}).get("org_id") is None

    # Resuming with the SAME run_id continues from PER-001, not from the beginning --
    # PER-001 is never touched again (would raise the immutable-field guard if it were).
    second = stage_organizations_execute(db, resume_run_id=first["run_id"])
    resumed_run = get_run(db, second["run_id"])
    assert resumed_run["run_id"] == first["run_id"]
    assert resumed_run["processed_count"] == 2
    assert PersonRepository(db).find_one({"id": "PER-002"}).get("org_id") is not None


def test_20b_checkpoint_record_tracks_progress_fields(db):
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())
    run_id = start_run(db, "organizations")
    stage_organizations_execute(db, resume_run_id=run_id)
    run = get_run(db, run_id)
    assert run["status"] == "completed"
    assert run["completed_at"] is not None
    assert run["processed_count"] == 1
    assert run["updated_count"] == 1
    assert run["error_count"] == 0


# --- Regression: malformed/combined email values must never produce a bogus org ---------
# Real case found in the Atlas organizations dry-run: PER-444's stored email is
# "simpsonlowell@gmail.com; lowell@outfront.com" -- naive domain extraction would derive
# the nonsense domain "gmail.com; lowell@outfront.com" and create a bogus Organization.


@pytest.mark.parametrize(
    "email",
    [
        "simpsonlowell@gmail.com; lowell@outfront.com",  # the real PER-444 case, semicolon
        "a@example.com, b@example.com",  # comma-separated
        "a@example.com@example.net",  # more than one '@'
    ],
)
def test_is_malformed_email_detects_combined_values(email):
    assert _is_malformed_email(email) is True


@pytest.mark.parametrize("email", ["ashok@databeat.io", "a.b+tag@sub.example.co.uk"])
def test_is_malformed_email_accepts_normal_single_addresses(email):
    assert _is_malformed_email(email) is False


def _lowell_444(**overrides):
    doc = _person(
        id="PER-444", name="Lowell Simpson",
        email="simpsonlowell@gmail.com; lowell@outfront.com",
        org="Outfront Media", org_id=None, open_threads=["thread_lowell"],
    )
    doc.update(overrides)
    return doc


def test_malformed_email_dry_run_reports_the_case_explicitly_and_proposes_no_organization(db):
    PersonRepository(db).upsert_by_key({"id": "PER-444"}, _lowell_444())

    result = stage_organizations_dry_run(db)

    assert result["people_with_malformed_email"] == 1
    assert result["malformed_email_cases"] == [
        {
            "person_id": "PER-444",
            "email": "simpsonlowell@gmail.com; lowell@outfront.com",
            "reason": "malformed/multiple email",
            "action": "organization skipped",
        }
    ]
    # No bogus domain/organization is ever proposed from the combined value.
    assert result["would_create_domains"] == []
    assert result["organizations_that_would_be_created"] == 0
    assert result["people_that_would_receive_org_id"] == 0
    # PER-444 must not be silently counted as "valid" or plain "invalid" either -- it
    # has its own distinct malformed-email bucket.
    assert result["people_with_valid_email"] == 0
    assert result["people_with_invalid_email"] == 0


def test_malformed_email_dry_run_performs_zero_writes(db):
    PersonRepository(db).upsert_by_key({"id": "PER-444"}, _lowell_444())
    before = PersonRepository(db).find_many({})

    stage_organizations_dry_run(db)

    after = PersonRepository(db).find_many({})
    assert before == after
    assert OrganizationRepository(db).find_many({}) == []


def test_malformed_email_execution_skips_without_creating_organization_or_org_id(db):
    PersonRepository(db).upsert_by_key({"id": "PER-444"}, _lowell_444())

    result = stage_organizations_execute(db)

    assert result["people_updated"] == 0
    assert result["people_skipped"] == 1
    assert OrganizationRepository(db).find_many({}) == []
    person = PersonRepository(db).find_one({"id": "PER-444"})
    assert person["org_id"] is None
    # Every other field, including the malformed email itself, is untouched.
    assert person["email"] == "simpsonlowell@gmail.com; lowell@outfront.com"
    assert person["org"] == "Outfront Media"
    assert person["name"] == "Lowell Simpson"


def test_malformed_email_person_never_merged_or_deleted(db):
    PersonRepository(db).upsert_by_key({"id": "PER-444"}, _lowell_444())
    stage_organizations_execute(db)
    assert PersonRepository(db).find_many({}).__len__() == 1
    assert PersonRepository(db).find_one({"id": "PER-444"}) is not None


def test_malformed_email_does_not_block_other_peoples_valid_organizations(db):
    PersonRepository(db).upsert_by_key({"id": "PER-444"}, _lowell_444())
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person())  # a normal, valid email

    result = stage_organizations_execute(db)

    assert result["people_updated"] == 1  # PER-001 only
    assert result["people_skipped"] == 1  # PER-444 only
    assert OrganizationRepository(db).find_many({}).__len__() == 1
    assert OrganizationRepository(db).find_many({})[0]["domain"] == "databeat.io"


# --- Stage 2 verification -----------------------------------------------------------------


def test_verify_canonical_references_passes_after_correct_execution(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    stage_canonical_references_execute(db)

    result = verify_stage_canonical_references(db)

    assert result["passed"] is True
    assert result["failures"] == []
    assert result["external_attendees_found"] == 0


def test_verify_canonical_references_detects_unknown_person_reference(db):
    _seed_thread_with_person(db)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-999"},
        {
            "id": "COM-999", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m1", "made_on": _NOW, "status": "open", "thread_id": "thread_1",
            "person_id": "PER-DOES-NOT-EXIST", "org_id": None,
        },
    )
    result = verify_stage_canonical_references(db)
    assert result["passed"] is False
    assert any("references unknown person" in f for f in result["failures"])


def test_verify_canonical_references_detects_follow_up_commitment_mismatch(db):
    _seed_thread_with_person(db)
    PersonRepository(db).upsert_by_key(
        {"id": "PER-002"}, _person(id="PER-002", email="other@databeat.io", org_id="ORG-001", open_threads=["thread_1"])
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "x", "class": "mine", "owed_by": None, "owed_to": None,
            "source_record": "m1", "made_on": _NOW, "status": "open", "thread_id": "thread_1",
            "person_id": "PER-001", "org_id": "ORG-001",
        },
    )
    FollowUpRepository(db).upsert_by_key(
        {"id": "FU-001"},
        {"id": "FU-001", "commitment_id": "COM-001", "thread_id": "thread_1", "person_id": "PER-002", "org_id": "ORG-001"},
    )
    result = verify_stage_canonical_references(db)
    assert result["passed"] is False
    assert any("does not match parent" in f for f in result["failures"])


def _seed_full_relationship_set(db, thread_id="thread_1", person_id="PER-001"):
    _seed_thread_with_person(db, thread_id=thread_id, person_id=person_id)
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m1", "made_on": _NOW,
            "status": "open", "thread_id": thread_id, "person_id": None, "org_id": None,
        },
    )
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "k1"},
        {
            "knowledge_id": "k1", "thread_id": thread_id, "subject_key": "ashok_ganapam",
            "predicate": "prefers", "fact_key": "mornings", "current_value": "mornings",
            "history": [], "source_emails": ["m1"], "basis": "stated",
            "first_seen_at": _NOW, "last_confirmed_at": _NOW, "confidence": 0.8, "status": "active",
        },
    )


# --- Part A: Stage 2 checkpointing ---------------------------------------------------------


def test_stage2_checkpoint_is_created_with_expected_fields(db):
    _seed_full_relationship_set(db)
    result = stage_canonical_references_execute(db)
    run = get_run(db, result["run_id"])
    assert run["stage"] == "canonical-references"
    assert run["status"] == "completed"
    assert run["started_at"] is not None
    assert run["completed_at"] is not None
    assert set(run["completed_phases"]) == {"threads", "relationships", "knowledge_items"}


def test_stage2_checkpoint_records_progress_per_phase(db):
    _seed_full_relationship_set(db)
    run_id = start_run(db, "canonical-references")
    stage_canonical_references_execute(db, resume_run_id=run_id)
    run = get_run(db, run_id)
    assert run["updated_counts_by_collection"]["commitments"] == 1
    assert run["updated_counts_by_collection"]["threads"] == 1
    assert run["updated_counts_by_collection"]["knowledge_items"] == 1


def test_stage2_checkpoint_records_failure_state(db, monkeypatch):
    _seed_full_relationship_set(db)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated interruption")

    import app.entity_migration as migration_module

    monkeypatch.setattr(migration_module, "_execute_relationships_phase", _boom)

    with pytest.raises(RuntimeError, match="simulated interruption"):
        stage_canonical_references_execute(db)

    run = MigrationRunRepositoryOf(db)
    assert run["status"] == "failed"
    assert run["last_error"] == "simulated interruption"
    assert "threads" in run["completed_phases"]  # the phase before the failure did complete
    assert "relationships" not in run["completed_phases"]


def MigrationRunRepositoryOf(db):
    from app.database.repositories import MigrationRunRepository

    runs = MigrationRunRepository(db).find_many({})
    assert len(runs) == 1
    return runs[0]


def test_stage2_resume_continues_after_a_failed_phase_without_redoing_completed_phases(db, monkeypatch):
    _seed_full_relationship_set(db)
    import app.entity_migration as migration_module

    original_relationships_phase = migration_module._execute_relationships_phase
    call_count = {"n": 0}

    def _fail_once(db_arg, dry, updated_counts):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated interruption")
        return original_relationships_phase(db_arg, dry, updated_counts)

    monkeypatch.setattr(migration_module, "_execute_relationships_phase", _fail_once)

    with pytest.raises(RuntimeError):
        migration_module.stage_canonical_references_execute(db)
    failed_run = MigrationRunRepositoryOf(db)
    assert failed_run["status"] == "failed"
    run_id = failed_run["run_id"]

    # Resuming with the same run_id: the threads phase (already completed) must not
    # run again -- only relationships + knowledge_items proceed.
    result = migration_module.stage_canonical_references_execute(db, resume_run_id=run_id)
    assert result["run_id"] == run_id
    resumed_run = get_run(db, run_id)
    assert resumed_run["status"] == "completed"
    assert set(resumed_run["completed_phases"]) == {"threads", "relationships", "knowledge_items"}
    # Only one migration_runs document exists throughout -- resume reused the same
    # checkpoint record rather than creating a second one.
    assert MigrationRunRepositoryOf(db)["run_id"] == run_id


def test_stage2_resume_is_idempotent_and_produces_zero_additional_changes(db):
    _seed_full_relationship_set(db)
    run_id = start_run(db, "canonical-references")
    stage_canonical_references_execute(db, resume_run_id=run_id)

    state_after_first = {
        "commitment": CommitmentRepository(db).find_one({"id": "COM-001"}),
        "thread": ThreadRepository(db).find_one({"thread_id": "thread_1"}),
        "knowledge": KnowledgeRepository(db).find_one({"knowledge_id": "k1"}),
    }

    # Resuming an ALREADY-COMPLETED run must be a pure no-op: every phase is already
    # in completed_phases, so nothing is re-executed.
    stage_canonical_references_execute(db, resume_run_id=run_id)

    state_after_second = {
        "commitment": CommitmentRepository(db).find_one({"id": "COM-001"}),
        "thread": ThreadRepository(db).find_one({"thread_id": "thread_1"}),
        "knowledge": KnowledgeRepository(db).find_one({"knowledge_id": "k1"}),
    }
    assert state_after_first == state_after_second
    assert PersonRepository(db).find_many({}).__len__() == 1  # no duplicate Person either


def test_stage2_resume_never_duplicates_writes_across_separate_run_ids(db):
    # A completely fresh, unrelated run over the same already-migrated data must also
    # find nothing left to do (idempotency doesn't depend on reusing the same run_id).
    _seed_full_relationship_set(db)
    stage_canonical_references_execute(db)

    second_result = stage_canonical_references_execute(db)

    assert second_result["updated_counts"] == {}
    assert CommitmentRepository(db).find_one({"id": "COM-001"})["person_id"] == "PER-001"


def test_stage2_resume_preserves_existing_correct_canonical_references(db):
    _seed_full_relationship_set(db)
    stage_canonical_references_execute(db)
    commitment_before = CommitmentRepository(db).find_one({"id": "COM-001"})

    # A brand-new record appears (as if new data arrived between runs) -- resuming
    # must not touch the already-correct COM-001 while still processing the new one.
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-002"},
        {
            "id": "COM-002", "what": "schedule demo", "class": "mine", "owed_by": None,
            "owed_to": "Ashok Ganapam", "source_record": "m2", "made_on": _NOW,
            "status": "open", "thread_id": "thread_1", "person_id": None, "org_id": None,
        },
    )
    stage_canonical_references_execute(db)

    assert CommitmentRepository(db).find_one({"id": "COM-001"}) == commitment_before
    assert CommitmentRepository(db).find_one({"id": "COM-002"})["person_id"] == "PER-001"


def test_verify_canonical_references_flags_external_attendees_as_safety_anomaly(db):
    _seed_thread_with_person(db)
    CalendarActionRepository(db).upsert_by_key(
        {"thread_id": "thread_1", "meeting_fingerprint": "fp1"},
        {
            "thread_id": "thread_1", "meeting_fingerprint": "fp1", "status": "awaiting_approval",
            "event": {"title": "x", "start": _NOW, "end": _NOW, "timezone": "UTC", "description": "x", "attendees": ["intruder@example.com"]},
            "actor_type": "authenticated_user", "reason": None, "person_id": None, "org_id": None, "meeting_id": None,
        },
    )
    result = verify_stage_canonical_references(db)
    assert result["passed"] is False
    assert result["external_attendees_found"] == 1
    assert any("SAFETY ANOMALY" in f for f in result["failures"])
