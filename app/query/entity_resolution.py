"""Phase 22A: read-only Person/Organization resolution for the query layer.

Reuses the existing identity primitives -- app.entities.resolution.
resolve_canonical_person_for_email and app.entities.lifecycle -- rather than
re-deriving the identity hierarchy. Nothing in this module ever calls
PersonRepository.upsert_by_key, OrganizationRepository.upsert_by_key, or
app.entities.resolution.resolve_person/resolve_organization (both of which CAN
create records) -- a query must never create a Person or Organization merely
because someone asked about one.

Name-only global search (no email) is a NEW matching context this module
introduces, deliberately absent from app.entities.resolution: during ingestion,
"name alone must never establish identity" because doing so would silently merge
two different real people going forward. Here, a read-only query is never written
back anywhere -- returning an AMBIGUOUS result with candidates when multiple active
people share matching name tokens is exactly the safe behavior Section R requires,
not a violation of the identity hierarchy.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import OrganizationRepository, PersonRepository, ProjectRepository
from app.entities.lifecycle import CanonicalResolutionError, is_person_active
from app.entities.resolution import resolve_canonical_person_for_email
from app.knowledge.normalize import normalize_text
from app.query.schemas import EntityCandidate, EntityReference, ResolutionStatus


def _name_tokens(name: str) -> set[str]:
    return set(normalize_text(name or "").split())


def _name_matches(query_tokens: set[str], candidate_name: str, alias_names: list[str] | None = None) -> bool:
    candidate_tokens = _name_tokens(candidate_name)
    if query_tokens <= candidate_tokens or candidate_tokens <= query_tokens:
        return True
    for alias in alias_names or []:
        if query_tokens <= _name_tokens(alias):
            return True
    return False


def resolve_person_reference(db: Database, raw_text: str | None = None, email: str | None = None) -> EntityReference:
    """Never creates a Person. An exact email always wins (tier 1, same as
    ingestion); a bare name searches only ACTIVE people and returns AMBIGUOUS
    (never guesses) when more than one matches.
    """
    if email:
        try:
            found = resolve_canonical_person_for_email(db, email)
        except CanonicalResolutionError as exc:
            return EntityReference(
                kind="person", raw_text=raw_text, email_hint=email,
                resolution_status=ResolutionStatus.LIFECYCLE_ERROR, lifecycle_error=str(exc),
            )
        if found is None:
            return EntityReference(kind="person", raw_text=raw_text, email_hint=email, resolution_status=ResolutionStatus.NOT_FOUND)
        return EntityReference(kind="person", raw_text=raw_text, email_hint=email, resolution_status=ResolutionStatus.RESOLVED, resolved_id=found["id"])

    if not raw_text:
        return EntityReference(kind="person", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    query_tokens = _name_tokens(raw_text)
    if not query_tokens:
        return EntityReference(kind="person", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    matches = [
        p for p in PersonRepository(db).find_many({})
        if is_person_active(p) and _name_matches(query_tokens, p["name"], p.get("aliases"))
    ]
    return _reference_from_matches("person", raw_text, matches)


def resolve_organization_reference(db: Database, raw_text: str | None = None, domain: str | None = None) -> EntityReference:
    """Never creates an Organization -- looks up by domain (exact) or by name/alias
    token match only. Uses a plain find_one/find_many, never
    app.entities.resolution.resolve_organization, which can create a record.
    """
    if domain:
        found = OrganizationRepository(db).find_one({"domain": domain.strip().lower()})
        if found is None:
            return EntityReference(kind="organization", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)
        return EntityReference(kind="organization", raw_text=raw_text, resolution_status=ResolutionStatus.RESOLVED, resolved_id=found["id"])

    if not raw_text:
        return EntityReference(kind="organization", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    query_tokens = _name_tokens(raw_text)
    if not query_tokens:
        return EntityReference(kind="organization", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    matches = [
        o for o in OrganizationRepository(db).find_many({})
        if _name_matches(query_tokens, o["name"], o.get("aliases"))
    ]
    return _reference_from_matches("organization", raw_text, matches)


def resolve_project_reference(db: Database, raw_text: str | None = None) -> EntityReference:
    """Never creates a Project. Matches on the stored `project` name field only."""
    if not raw_text:
        return EntityReference(kind="project", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    query_tokens = _name_tokens(raw_text)
    if not query_tokens:
        return EntityReference(kind="project", raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)

    matches = [p for p in ProjectRepository(db).find_many({}) if _name_matches(query_tokens, p["project"])]
    return _reference_from_matches("project", raw_text, matches)


def _reference_from_matches(kind: str, raw_text: str, matches: list[dict[str, Any]]) -> EntityReference:
    if not matches:
        return EntityReference(kind=kind, raw_text=raw_text, resolution_status=ResolutionStatus.NOT_FOUND)  # type: ignore[arg-type]
    if len(matches) == 1:
        return EntityReference(kind=kind, raw_text=raw_text, resolution_status=ResolutionStatus.RESOLVED, resolved_id=matches[0]["id"])  # type: ignore[arg-type]
    name_field = "project" if kind == "project" else "name"
    candidates = [
        EntityCandidate(id=m["id"], name=m[name_field], email=m.get("email"), org=m.get("org") or m.get("entity"))
        for m in matches
    ]
    return EntityReference(kind=kind, raw_text=raw_text, resolution_status=ResolutionStatus.AMBIGUOUS, candidates=candidates)  # type: ignore[arg-type]
