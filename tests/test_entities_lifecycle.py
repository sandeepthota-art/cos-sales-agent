# tests/test_entities_lifecycle.py
"""Phase 19.1: the centralized Person lifecycle contract, tested directly and in
isolation from identity resolution/reconciliation/consolidation (which each have
their own tests proving they actually USE this module). All mongomock only.
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import PersonRepository
from app.entities.lifecycle import (
    CanonicalResolutionError,
    is_person_active,
    is_person_merged,
    resolve_canonical_person_id,
)


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
    }
    doc.update(overrides)
    return doc


# --- Contract: missing / active / merged ----------------------------------------------


def test_missing_status_means_active():
    person = _person()
    person.pop("status", None)  # historical document predating the field entirely
    assert is_person_active(person) is True
    assert is_person_merged(person) is False


def test_status_active_means_active():
    person = _person(status="active")
    assert is_person_active(person) is True
    assert is_person_merged(person) is False


def test_status_merged_means_retired():
    person = _person(status="merged", merged_into="PER-2")
    assert is_person_active(person) is False
    assert is_person_merged(person) is True


# --- resolve_canonical_person_id ---------------------------------------------------------


def test_resolve_canonical_person_id_active_person_returns_itself(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1"))
    assert resolve_canonical_person_id(db, "PER-1") == "PER-1"


def test_resolve_canonical_person_id_missing_status_treated_as_active(db):
    person = _person(id="PER-1")
    person.pop("status", None)
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, person)
    assert resolve_canonical_person_id(db, "PER-1") == "PER-1"


def test_resolve_canonical_person_id_single_hop(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    assert resolve_canonical_person_id(db, "PER-1") == "PER-2"


def test_resolve_canonical_person_id_multi_hop(db):
    PersonRepository(db).upsert_by_key({"id": "PER-3"}, _person(id="PER-3"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", status="merged", merged_into="PER-3"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    assert resolve_canonical_person_id(db, "PER-1") == "PER-3"


def test_resolve_canonical_person_id_missing_person_raises(db):
    with pytest.raises(CanonicalResolutionError, match="does not exist"):
        resolve_canonical_person_id(db, "PER-999")


def test_resolve_canonical_person_id_missing_merged_into_target_raises(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into=None))
    with pytest.raises(CanonicalResolutionError, match="no merged_into target"):
        resolve_canonical_person_id(db, "PER-1")


def test_resolve_canonical_person_id_missing_downstream_target_raises(db):
    # PER-1 -> PER-2, but PER-2 was never actually created (data corruption).
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    with pytest.raises(CanonicalResolutionError, match="does not exist"):
        resolve_canonical_person_id(db, "PER-1")


def test_resolve_canonical_person_id_self_reference_raises(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-1"))
    with pytest.raises(CanonicalResolutionError, match="pointing at itself"):
        resolve_canonical_person_id(db, "PER-1")


def test_resolve_canonical_person_id_circular_chain_raises(db):
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2", status="merged", merged_into="PER-1"))
    with pytest.raises(CanonicalResolutionError, match="circular"):
        resolve_canonical_person_id(db, "PER-1")


def test_resolve_canonical_person_id_never_writes_to_atlas(db):
    PersonRepository(db).upsert_by_key({"id": "PER-2"}, _person(id="PER-2"))
    PersonRepository(db).upsert_by_key({"id": "PER-1"}, _person(id="PER-1", status="merged", merged_into="PER-2"))
    before = list(db.people.find({}, {"_id": 0}))

    resolve_canonical_person_id(db, "PER-1")

    after = list(db.people.find({}, {"_id": 0}))
    assert before == after
