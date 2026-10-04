# tests/test_entities_organization_lifecycle.py
"""The centralized Organization lifecycle contract, mirroring
tests/test_entities_lifecycle.py's Person-lifecycle tests exactly. All
mongomock only.
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import OrganizationRepository
from app.entities.organization_lifecycle import (
    OrganizationCanonicalResolutionError,
    is_organization_active,
    is_organization_merged,
    resolve_canonical_organization_id,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _org(**overrides):
    org_id = overrides.get("id", "ORG-1")
    doc = {"id": org_id, "name": "Someone Co", "domain": f"{org_id.lower()}.example", "aliases": [], "source": "gmail"}
    doc.update(overrides)
    return doc


def test_missing_status_means_active():
    org = _org()
    org.pop("status", None)
    assert is_organization_active(org) is True
    assert is_organization_merged(org) is False


def test_status_merged_means_retired():
    org = _org(status="merged", merged_into="ORG-2")
    assert is_organization_active(org) is False
    assert is_organization_merged(org) is True


def test_resolve_canonical_organization_id_returns_self_when_active(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, _org(status="active"))
    assert resolve_canonical_organization_id(db, "ORG-1") == "ORG-1"


def test_resolve_canonical_organization_id_follows_merged_into(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-2"}, _org(id="ORG-2", status="active"))
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-1"}, _org(id="ORG-1", status="merged", merged_into="ORG-2")
    )
    assert resolve_canonical_organization_id(db, "ORG-1") == "ORG-2"


def test_resolve_canonical_organization_id_raises_on_broken_chain(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-1"}, _org(id="ORG-1", status="merged", merged_into="ORG-404-DOES-NOT-EXIST")
    )
    with pytest.raises(OrganizationCanonicalResolutionError):
        resolve_canonical_organization_id(db, "ORG-1")
