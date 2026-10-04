"""The single, centralized Organization lifecycle contract -- mirrors
app.entities.lifecycle's Person contract exactly. Nothing else in the codebase
should inline an `org.get("status") == "merged"` check.

Contract (see app.entities.models.Organization.status/merged_into):
  - status missing entirely (every historical document predating this field) -> ACTIVE
  - status == "active"                                                        -> ACTIVE
  - status == "merged"                                                        -> MERGED, with
    merged_into pointing at the canonical replacement

This module never writes to MongoDB and never repairs a broken merged_into chain --
it only ever reports (as an explicit exception) that one is broken.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import OrganizationRepository

ACTIVE = "active"
MERGED = "merged"

_MAX_CHAIN_DEPTH = 10


class OrganizationCanonicalResolutionError(Exception):
    """A merged_into chain could not be safely followed to an active
    Organization -- missing target, self-reference, a cycle, or a chain deeper
    than _MAX_CHAIN_DEPTH. Callers must handle this explicitly; nothing in
    this module ever repairs the chain or invents a replacement Organization.
    """


def is_organization_merged(org: dict[str, Any]) -> bool:
    return org.get("status") == MERGED


def is_organization_active(org: dict[str, Any]) -> bool:
    return not is_organization_merged(org)


def resolve_canonical_organization_id(db: Database, org_id: str) -> str:
    """Returns the id of the ACTIVE Organization that `org_id` should be
    treated as today: itself, if already active; otherwise the result of
    following `merged_into` repeatedly until an active Organization is
    reached. Raises OrganizationCanonicalResolutionError (never returns a
    guess) on a missing target, self-reference, cycle, or a chain exceeding
    _MAX_CHAIN_DEPTH.
    """
    repo = OrganizationRepository(db)
    seen: set[str] = set()
    current_id = org_id

    for _ in range(_MAX_CHAIN_DEPTH):
        if current_id in seen:
            raise OrganizationCanonicalResolutionError(
                f"circular merged_into chain detected starting at {org_id!r} (revisited {current_id!r})"
            )
        seen.add(current_id)

        org = repo.find_one({"id": current_id})
        if org is None:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} does not exist (broken merged_into chain starting at {org_id!r})"
            )
        if is_organization_active(org):
            return current_id

        target = org.get("merged_into")
        if not target:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} is status='merged' but has no merged_into target"
            )
        if target == current_id:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} has merged_into pointing at itself"
            )
        current_id = target

    raise OrganizationCanonicalResolutionError(
        f"merged_into chain from {org_id!r} exceeded {_MAX_CHAIN_DEPTH} hops without reaching an active "
        "organization -- possible undetected cycle or pathological chain"
    )
