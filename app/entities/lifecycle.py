"""Phase 19.1: the single, centralized Person lifecycle contract.

Every other module that needs to know whether a Person is "real" (participates in
identity resolution, duplicate detection, and consolidation) or "retired" (kept for
audit/history only) calls the helpers here -- nothing else in the codebase should
inline a `person.get("status") == "merged"` check.

Contract (see app.entities.models.Person.status/merged_into):
  - status missing entirely (every historical document predating this field) -> ACTIVE
  - status == "active"                                                        -> ACTIVE
  - status == "merged"                                                        -> MERGED, with
    merged_into pointing at the canonical replacement

This module never writes to MongoDB and never repairs a broken merged_into chain --
it only ever reports (as an explicit exception) that one is broken, so callers can
surface the integrity problem rather than silently inventing a new Person or looping
forever on a cycle.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import PersonRepository

ACTIVE = "active"
MERGED = "merged"

# A real merged_into chain should never need more than one hop (A merged into B,
# B active) -- this ceiling exists purely to turn an otherwise-infinite loop on an
# undetected cycle into a bounded, explicit failure. The `seen`-set check in
# resolve_canonical_person_id already catches any cycle long before this would ever
# be reached; this is a second, independent guard against extreme pathological data.
_MAX_CHAIN_DEPTH = 10


class CanonicalResolutionError(Exception):
    """A merged_into chain could not be safely followed to an active Person --
    missing target, self-reference, a cycle, or a chain deeper than
    _MAX_CHAIN_DEPTH. Callers must handle this explicitly; nothing in this module
    ever repairs the chain or invents a replacement Person on its own.
    """


def is_person_merged(person: dict[str, Any]) -> bool:
    return person.get("status") == MERGED


def is_person_active(person: dict[str, Any]) -> bool:
    return not is_person_merged(person)


def resolve_canonical_person_id(db: Database, person_id: str) -> str:
    """Returns the id of the ACTIVE Person that `person_id` should be treated as
    today: itself, if already active; otherwise the result of following
    `merged_into` repeatedly until an active Person is reached.

    Raises CanonicalResolutionError (never returns a guess, never creates or
    modifies anything) when:
      - `person_id` (or a Person later in the chain) does not exist
      - a merged Person has no merged_into target
      - a merged_into value points back at the same Person (self-reference)
      - the chain revisits a Person already seen (a cycle)
      - the chain exceeds _MAX_CHAIN_DEPTH hops without reaching an active Person
    """
    repo = PersonRepository(db)
    seen: set[str] = set()
    current_id = person_id

    for _ in range(_MAX_CHAIN_DEPTH):
        if current_id in seen:
            raise CanonicalResolutionError(
                f"circular merged_into chain detected starting at {person_id!r} (revisited {current_id!r})"
            )
        seen.add(current_id)

        person = repo.find_one({"id": current_id})
        if person is None:
            raise CanonicalResolutionError(
                f"person {current_id!r} does not exist (broken merged_into chain starting at {person_id!r})"
            )
        if is_person_active(person):
            return current_id

        target = person.get("merged_into")
        if not target:
            raise CanonicalResolutionError(f"person {current_id!r} is status='merged' but has no merged_into target")
        if target == current_id:
            raise CanonicalResolutionError(f"person {current_id!r} has merged_into pointing at itself")
        current_id = target

    raise CanonicalResolutionError(
        f"merged_into chain from {person_id!r} exceeded {_MAX_CHAIN_DEPTH} hops without reaching an active person "
        "-- possible undetected cycle or pathological chain"
    )
