# tests/test_query_entity_resolution.py
"""Phase 22A: read-only Person/Organization/Project resolution for the query layer.
Every test uses synthetic mongomock fixtures -- no hardcoded production ids."""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import OrganizationRepository, PersonRepository, ProjectRepository
from app.query.entity_resolution import (
    resolve_organization_reference,
    resolve_person_reference,
    resolve_project_reference,
)
from app.query.schemas import ResolutionStatus


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


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


# --- Person: exact email ----------------------------------------------------------------


def test_resolve_person_by_exact_email(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="active@example.com"))

    ref = resolve_person_reference(db, email="Active@Example.com")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "PER-1"


def test_resolve_person_unknown_email_is_not_found(db):
    ref = resolve_person_reference(db, email="nobody@example.com")
    assert ref.resolution_status == ResolutionStatus.NOT_FOUND
    assert ref.resolved_id is None


# --- Person: merged/lifecycle --------------------------------------------------------------


def test_resolve_person_merged_by_email_redirects_to_canonical(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", email="canonical@example.com"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-1"}, _person(id="PER-1", email="merged@example.com", status="merged", merged_into="PER-2")
    )

    ref = resolve_person_reference(db, email="merged@example.com")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "PER-2"
    assert ref.resolved_id != "PER-1"  # never returns a merged Person as canonical


def test_resolve_person_multi_hop_chain_resolves_to_final_active(db):
    PersonRepository(db).upsert_by_key({"id": "PER-C"}, _person(id="PER-C", email="c@example.com"))
    PersonRepository(db).upsert_by_key({"id": "PER-B"}, _person(id="PER-B", email="b@example.com", status="merged", merged_into="PER-C"))
    PersonRepository(db).upsert_by_key({"id": "PER-A"}, _person(id="PER-A", email="a@example.com", status="merged", merged_into="PER-B"))

    ref = resolve_person_reference(db, email="a@example.com")

    assert ref.resolved_id == "PER-C"


def test_resolve_person_broken_merge_target_surfaces_lifecycle_error(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", email="broken@example.com", status="merged", merged_into=None))

    ref = resolve_person_reference(db, email="broken@example.com")

    assert ref.resolution_status == ResolutionStatus.LIFECYCLE_ERROR
    assert ref.resolved_id is None
    assert "no merged_into target" in ref.lifecycle_error


def test_resolve_person_circular_chain_surfaces_lifecycle_error(db):
    PersonRepository(db).upsert_by_key({"id": "PER-A"}, _person(id="PER-A", email="a@example.com", status="merged", merged_into="PER-B"))
    PersonRepository(db).upsert_by_key({"id": "PER-B"}, _person(id="PER-B", email="b@example.com", status="merged", merged_into="PER-A"))

    ref = resolve_person_reference(db, email="a@example.com")

    assert ref.resolution_status == ResolutionStatus.LIFECYCLE_ERROR
    assert "circular" in ref.lifecycle_error


# --- Person: name-only, ambiguity ------------------------------------------------------------


def test_resolve_person_unique_name_match_resolves(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Distinct Name", email="distinct@example.com"))

    ref = resolve_person_reference(db, raw_text="Distinct Name")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "PER-1"


def test_resolve_person_ambiguous_name_returns_candidates_never_guesses(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", name="Jason Greene", email="jg1@example.com"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Jason Smith", email="jg2@example.com"))

    ref = resolve_person_reference(db, raw_text="Jason")

    assert ref.resolution_status == ResolutionStatus.AMBIGUOUS
    assert ref.resolved_id is None
    assert {c.id for c in ref.candidates} == {"PER-1", "PER-2"}


def test_resolve_person_name_search_never_returns_a_merged_person(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", name="Ash Allen", email="ash.allen@example.com"))
    PersonRepository(db).upsert_by_key(
        {"id": "PER-1"}, _person(id="PER-1", name="Ash Allen", email="ash.old@example.com", status="merged", merged_into="PER-2")
    )

    ref = resolve_person_reference(db, raw_text="Ash Allen")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "PER-2"  # the merged fragment never appears as a candidate at all


def test_resolve_person_unknown_name_is_not_found(db):
    ref = resolve_person_reference(db, raw_text="Nobody At All")
    assert ref.resolution_status == ResolutionStatus.NOT_FOUND


def test_resolve_person_never_creates_a_person(db):
    before = PersonRepository(db).find_many({}).__len__()
    resolve_person_reference(db, raw_text="Totally Unknown Person")
    resolve_person_reference(db, email="unknown@example.com")
    after = PersonRepository(db).find_many({}).__len__()
    assert before == after == 0


# --- Organization ------------------------------------------------------------------------


def _org(**overrides):
    doc = {"id": "ORG-1", "name": "Acme", "domain": "acme.example", "aliases": [], "source": "gmail"}
    doc.update(overrides)
    return doc


def test_resolve_organization_by_domain(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, _org())

    ref = resolve_organization_reference(db, domain="ACME.example")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "ORG-1"


def test_resolve_organization_unknown_domain_not_found(db):
    ref = resolve_organization_reference(db, domain="nowhere.example")
    assert ref.resolution_status == ResolutionStatus.NOT_FOUND


def test_resolve_organization_by_name(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, _org(name="DataBeat", domain="databeat.io"))

    ref = resolve_organization_reference(db, raw_text="DataBeat")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "ORG-1"


def test_resolve_organization_ambiguous_name_returns_candidates(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, _org(id="ORG-1", name="Acme Corp", domain="acme1.example"))
    OrganizationRepository(db).upsert_by_key({"id": "ORG-2"}, _org(id="ORG-2", name="Acme Inc", domain="acme2.example"))

    ref = resolve_organization_reference(db, raw_text="Acme")

    assert ref.resolution_status == ResolutionStatus.AMBIGUOUS
    assert len(ref.candidates) == 2


def test_resolve_organization_never_creates_an_organization(db):
    before = OrganizationRepository(db).find_many({}).__len__()
    resolve_organization_reference(db, domain="unknown.example")
    resolve_organization_reference(db, raw_text="Unknown Co")
    after = OrganizationRepository(db).find_many({}).__len__()
    assert before == after == 0


# --- Project ------------------------------------------------------------------------------


def test_resolve_project_by_name(db):
    ProjectRepository(db).upsert_by_key({"id": "PRJ-1"}, {"id": "PRJ-1", "project": "Enterprise Rollout", "entity": None, "goal_pillar": "Sales", "person_ids": [], "org_id": None})

    ref = resolve_project_reference(db, raw_text="Enterprise Rollout")

    assert ref.resolution_status == ResolutionStatus.RESOLVED
    assert ref.resolved_id == "PRJ-1"


def test_resolve_project_never_creates_a_project(db):
    before = ProjectRepository(db).find_many({}).__len__()
    resolve_project_reference(db, raw_text="Some New Project")
    after = ProjectRepository(db).find_many({}).__len__()
    assert before == after == 0
