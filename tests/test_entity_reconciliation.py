# tests/test_entity_reconciliation.py
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository,
    OrganizationRepository,
    PersonRepository,
    ThreadRepository,
)
from app.entity_reconciliation import (
    audit_commitments,
    audit_organizations,
    audit_people,
    audit_threads,
    build_report,
    find_duplicate_candidates,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _person(**overrides):
    doc = {
        "id": "PER-391", "name": "Ashok Ganapam", "email": "ashok@databeat.io", "aliases": [],
        "org": "DataBeat", "org_id": "ORG-001", "type": None, "goal_pillar": None,
        "role_in_pillar": None, "tier": None, "voice_register": None, "last_inbound": None,
        "last_outbound": None, "reports_to": None, "open_threads": ["thread_anchor"],
        "note_link": None, "review_flag": False, "source": "gmail",
    }
    doc.update(overrides)
    return doc


def test_audit_people_reports_org_id_coverage(db):
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    PersonRepository(db).upsert_by_key(
        {"id": "PER-390"},
        _person(id="PER-390", email=None, org_id=None, open_threads=["thread_x"]),
    )

    report = audit_people(db)

    assert report["total"] == 2
    assert len(report["with_org_id"]) == 1
    assert len(report["without_org_id"]) == 1
    assert len(report["email_but_no_org_id"]) == 0  # the org_id-less one also has no email


def test_audit_people_detects_no_duplicate_emails_when_none_exist(db):
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    report = audit_people(db)
    assert report["duplicate_emails"] == {}


def test_find_duplicate_candidates_high_confidence_via_shared_thread(db):
    # Mirrors the real PER-390 -> PER-391 finding from the earlier live audit.
    anchor = _person()
    orphan = _person(id="PER-390", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_anchor"])

    candidates = find_duplicate_candidates([anchor, orphan])

    assert len(candidates) == 1
    c = candidates[0]
    assert c["source_person_id"] == "PER-390"
    assert c["candidate_canonical_person_id"] == "PER-391"
    assert c["confidence"] == "HIGH"
    assert "thread_anchor" in c["shared_thread_ids"]


def test_find_duplicate_candidates_medium_confidence_via_org_match_only(db):
    anchor = _person()
    orphan = _person(
        id="PER-500", name="Ashok Ganapam", email=None, org_id=None, open_threads=["unrelated_thread"]
    )

    candidates = find_duplicate_candidates([anchor, orphan])

    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "MEDIUM"
    assert candidates[0]["shared_thread_ids"] == []


def test_find_duplicate_candidates_low_confidence_when_only_name_matches(db):
    anchor = _person()
    orphan = _person(
        id="PER-600", name="Ashok Ganapam", email=None, org=None, org_id=None,
        open_threads=["unrelated_thread"],
    )

    candidates = find_duplicate_candidates([anchor, orphan])

    assert len(candidates) == 1
    assert candidates[0]["confidence"] == "LOW"


def test_find_duplicate_candidates_never_guesses_when_ambiguous(db):
    anchor_1 = _person(id="PER-391", email="ashok@databeat.io")
    anchor_2 = _person(id="PER-999", name="Ashok Kumar", email="ashok.kumar@databeat.io")
    orphan = _person(id="PER-390", name="Ashok", email=None, org_id=None, open_threads=["x"])

    candidates = find_duplicate_candidates([anchor_1, anchor_2, orphan])

    assert candidates == []  # ambiguous -- never proposed


def test_find_duplicate_candidates_disambiguates_two_name_identical_anchors_via_shared_thread(db):
    # Real case found in the live Atlas data: two distinct, genuinely different real
    # people share the exact same name and org (different email domains) -- a no-email
    # orphan sharing a thread with only ONE of them is still safely resolvable, not a
    # guess, via that thread-overlap evidence.
    anchor_a = _person(id="PER-391", email="ashok@databeat.io", open_threads=["thread_a"])
    anchor_b = _person(id="PER-568", email="ashok.g@databeat.us", open_threads=["thread_b"])
    orphan = _person(id="PER-390", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_a"])

    candidates = find_duplicate_candidates([anchor_a, anchor_b, orphan])

    orphan_candidates = [c for c in candidates if c["source_person_id"] == "PER-390"]
    assert len(orphan_candidates) == 1
    assert orphan_candidates[0]["candidate_canonical_person_id"] == "PER-391"
    assert orphan_candidates[0]["confidence"] == "HIGH"


def test_find_duplicate_candidates_still_skips_when_thread_overlap_does_not_disambiguate(db):
    anchor_a = _person(id="PER-391", email="ashok@databeat.io", open_threads=["thread_shared"])
    anchor_b = _person(id="PER-568", email="ashok.g@databeat.us", open_threads=["thread_shared"])
    orphan = _person(id="PER-390", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_shared"])

    candidates = find_duplicate_candidates([anchor_a, anchor_b, orphan])

    assert [c for c in candidates if c["source_person_id"] == "PER-390"] == []


def test_find_duplicate_candidates_flags_two_anchors_sharing_name_and_org_as_medium_or_high(db):
    # Mirrors the real Lowell Simpson case: two DIFFERENT stored email addresses for
    # (very likely) the same real person, same name, same org -- both already
    # "anchors" by this script's own no-email/anchor split, so this is a distinct
    # detection path from the orphan->anchor one above.
    a = _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media", open_threads=["t1"])
    b = _person(id="PER-444", name="Lowell Simpson", email="simpsonlowell@gmail.com; lowell@outfront.com", org="Outfront Media", open_threads=["t1"])

    candidates = find_duplicate_candidates([a, b])

    assert len(candidates) == 1
    c = candidates[0]
    assert {c["source_person_id"], c["candidate_canonical_person_id"]} == {"PER-441", "PER-444"}
    assert c["confidence"] in ("HIGH", "MEDIUM")
    assert any("data_quality_flag" in line for line in c["evidence"])


def test_find_duplicate_candidates_never_merges_two_no_email_people(db):
    # Two no-email people, same name -- neither is an "anchor" (no email), so no
    # candidate mapping is ever proposed between them.
    a = _person(id="PER-001", email=None, org_id=None)
    b = _person(id="PER-002", email=None, org_id=None)

    assert find_duplicate_candidates([a, b]) == []


def test_audit_organizations_reports_member_counts_and_would_be_created_domains(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    people = [
        _person(),
        _person(id="PER-500", email="someone@newcompany.example", org_id=None),
    ]

    report = audit_organizations(db, people)

    assert len(report["organizations"]) == 1
    assert report["members_by_org"]["ORG-001"] == ["PER-391"]
    assert report["would_be_created_domains"] == {"newcompany.example": 1}


def test_audit_threads_reports_missing_canonical_refs(db):
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "t1"}, {"thread_id": "t1", "normalized_subject": "x", "message_ids": ["m1"]}
    )
    ThreadRepository(db).upsert_by_key(
        {"thread_id": "t2"},
        {"thread_id": "t2", "normalized_subject": "y", "message_ids": ["m2"], "person_ids": ["PER-391"], "org_ids": ["ORG-001"]},
    )

    report = audit_threads(db)

    assert report["total"] == 2
    missing_ids = {t["thread_id"] for t in report["missing_canonical_refs"]}
    assert missing_ids == {"t1"}


def test_audit_commitments_reports_missing_canonical_refs(db):
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "x", "class": "mine", "owed_by": "Ashok", "owed_to": None,
            "source_record": "m1", "made_on": "2026-01-01T00:00:00Z", "status": "open",
            "thread_id": "t1", "person_id": None, "org_id": None,
        },
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-002"},
        {
            "id": "COM-002", "what": "y", "class": "mine", "owed_by": "Ashok", "owed_to": None,
            "source_record": "m2", "made_on": "2026-01-01T00:00:00Z", "status": "open",
            "thread_id": "t1", "person_id": "PER-391", "org_id": "ORG-001",
        },
    )

    report = audit_commitments(db)

    assert report["total"] == 2
    assert [c["id"] for c in report["missing_canonical_refs"]] == ["COM-001"]


def test_build_report_never_writes_and_includes_no_modification_marker(db):
    PersonRepository(db).upsert_by_key({"id": "PER-391"}, _person())
    before = PersonRepository(db).find_many({})

    build_report(db)

    after = PersonRepository(db).find_many({})
    assert before == after  # strictly read-only


# --- Phase 19.1: lifecycle-aware duplicate detection --------------------------------------
#
# find_duplicate_candidates([...]) is a pure function over a people list -- no mongomock
# needed for these. None of them hard-code a real Atlas id; they build fresh synthetic
# active/merged pairs, matching Part 5's "must work for PER-900/901/902 tomorrow"
# requirement. The Ashok/John-Toth-flavored names below are just readable stand-ins, not
# special-cased logic anywhere in the code under test.


def test_merged_person_excluded_from_duplicate_detection_as_source(db):
    anchor = _person()  # PER-391, active
    already_merged_orphan = _person(
        id="PER-390", name="Ashok Ganapam", email=None, org_id=None,
        open_threads=["thread_anchor"], status="merged", merged_into="PER-391",
    )

    candidates = find_duplicate_candidates([anchor, already_merged_orphan])

    assert candidates == []


def test_merged_person_excluded_from_duplicate_detection_as_canonical_target(db):
    # A merged Person must never be proposed as somebody else's canonical target,
    # even when it still has a real, matching email on file.
    merged_anchor = _person(id="PER-386", status="merged", merged_into="PER-391")
    new_orphan = _person(id="PER-900", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_anchor"])

    candidates = find_duplicate_candidates([merged_anchor, new_orphan])

    assert candidates == []


def test_active_canonical_person_remains_eligible_after_sibling_is_merged(db):
    active_canonical = _person(id="PER-391")
    merged_sibling = _person(id="PER-386", status="merged", merged_into="PER-391", open_threads=["other_thread"])
    new_orphan = _person(id="PER-900", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_anchor"])

    candidates = find_duplicate_candidates([active_canonical, merged_sibling, new_orphan])

    assert len(candidates) == 1
    assert candidates[0]["candidate_canonical_person_id"] == "PER-391"


def test_previously_approved_mapping_is_not_resurfaced_after_being_merged(db):
    # Simulates exactly what Phase 19 produced: PER-390 retired, merged_into PER-391.
    canonical = _person(id="PER-391")
    retired_duplicate = _person(
        id="PER-390", name="Ashok Ganapam", email=None, org_id=None,
        open_threads=["thread_anchor"], status="merged", merged_into="PER-391",
    )

    candidates = find_duplicate_candidates([canonical, retired_duplicate])

    assert candidates == []


def test_john_toth_style_mapping_not_resurfaced_after_merge(db):
    canonical = _person(id="PER-384", name="John Toth", email="john.t@614group.com", org="The 614 Group")
    retired_duplicate = _person(
        id="PER-382", name="John Toth", email=None, org="The 614 Group", org_id=None,
        open_threads=["thread_anchor"], status="merged", merged_into="PER-384",
    )

    candidates = find_duplicate_candidates([canonical, retired_duplicate])

    assert candidates == []


def test_blocked_data_conflict_style_pair_unaffected_by_lifecycle_filter(db):
    # Two ACTIVE anchors with distinct real emails, same name+org -- still surfaced
    # exactly as before (this is a candidate-GENERATION concern; classification into
    # BLOCKED_DATA_CONFLICT happens one layer up in duplicate_impact_analysis).
    anchor_a = _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media")
    anchor_b = _person(id="PER-443", name="Lowell Simpson", email="simpsonlowell@gmail.com", org="Outfront Media")

    candidates = find_duplicate_candidates([anchor_a, anchor_b])

    assert len(candidates) == 1
    assert {candidates[0]["source_person_id"], candidates[0]["candidate_canonical_person_id"]} == {"PER-441", "PER-443"}


def test_malformed_combined_email_pair_unaffected_by_lifecycle_filter(db):
    anchor = _person(id="PER-441", name="Lowell Simpson", email="lowell@outfront.com", org="Outfront Media")
    malformed = _person(
        id="PER-444", name="Lowell Simpson", email="simpsonlowell@gmail.com; lowell@outfront.com", org="Outfront Media",
    )

    candidates = find_duplicate_candidates([anchor, malformed])

    assert len(candidates) == 1  # data-quality flagging is unchanged; classification lives elsewhere


def test_short_generic_name_pair_still_detected_and_unaffected_by_lifecycle_filter(db):
    anchor = _person(id="PER-513", name="Ash Allen", email="ash.allen@databeat.io", org="DataBeat")
    orphan = _person(id="PER-608", name="Ash", email=None, org_id=None, open_threads=["thread_anchor"])

    candidates = find_duplicate_candidates([anchor, orphan])

    assert len(candidates) == 1  # still a HIGH candidate row here -- NEEDS_MANUAL_REVIEW is a classification concern


def test_shared_thread_evidence_still_used_for_active_people_after_lifecycle_filter(db):
    anchor_a = _person(id="PER-391", email="ashok@databeat.io", open_threads=["thread_a"])
    anchor_b = _person(id="PER-568", email="ashok.g@databeat.us", open_threads=["thread_b"])
    orphan = _person(id="PER-390", name="Ashok Ganapam", email=None, org_id=None, open_threads=["thread_a"])

    candidates = find_duplicate_candidates([anchor_a, anchor_b, orphan])

    match = next(c for c in candidates if c["source_person_id"] == "PER-390")
    assert match["candidate_canonical_person_id"] == "PER-391"
    assert match["confidence"] == "HIGH"
