"""Part 3 of the global-identity design: assemble a canonical Person's connected
context (the "360-degree view" / "resolve_person_context" operation) purely by
following canonical ID references -- never by copying/duplicating the underlying
objects into the Person document itself (Part 5).

Priority order per category (this module's second iteration, once canonical
person_id/org_id fields exist on commitments/meetings/follow_ups/projects/
knowledge_items -- see app.pipeline._process_entities and the model changes in
app.entities.models/app.knowledge.models):

  1. Canonical person_id / person_ids match  -> basis: "canonical_person_id"
  2. Canonical org_id match (no person match) -> basis: "canonical_org_id"
  3. Legacy thread-scoped free-text name match -> basis: "legacy_thread_scoped_name_match"

The legacy fallback exists ONLY for objects created before canonical references
existed (or where the evidence to set one was never strong enough -- e.g. an
attendee name that matched no resolved Person this email). It is never presented as
equivalent to a canonical relationship, and a category that already has a canonical
match never also runs the legacy fallback for the same objects.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entities.lifecycle import CanonicalResolutionError, resolve_canonical_person_id
from app.knowledge.normalize import normalize_text


def _name_and_alias_tokens(person: dict[str, Any]) -> set[str]:
    tokens = set(normalize_text(person.get("name") or "").split())
    for alias in person.get("aliases", []):
        tokens |= set(normalize_text(alias).split())
    return tokens


def _tag(docs: list[dict[str, Any]], basis: str) -> list[dict[str, Any]]:
    return [{**doc, "basis": basis} for doc in docs]


def _legacy_name_match(
    docs: list[dict[str, Any]], name_tokens: set[str], name_fields: list[str]
) -> list[dict[str, Any]]:
    matches = []
    for doc in docs:
        doc_tokens: set[str] = set()
        for field in name_fields:
            value = doc.get(field)
            if isinstance(value, str):
                doc_tokens |= set(normalize_text(value).split())
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        doc_tokens |= set(normalize_text(item).split())
        if name_tokens & doc_tokens:
            matches.append(doc)
    return matches


def get_person_context(db: Database, person_id: str) -> dict[str, Any] | None:
    """Returns None if person_id doesn't exist. Otherwise a dict with the canonical
    person/organization plus every category of related context this schema supports
    -- each item carries its own `basis` (see module docstring), so a caller never
    mistakes a legacy, approximate match for a guaranteed canonical one.
    """
    person_repo = PersonRepository(db)
    person = person_repo.find_one({"id": person_id})
    if person is None:
        return None

    # Phase 19.1: additive only -- every category below still keys off the literal
    # person_id passed in, exactly as before consolidation existed (a merged
    # Person's own historical associations are never deleted, and this never
    # silently redirects the rest of the 360 view to the canonical's data). This
    # just lets a caller discover, for a merged Person, which active Person is now
    # the "primary identity" (Part 10) without changing any other field's meaning.
    # A broken merged_into chain is surfaced here, not raised -- Person 360 is a
    # read/reporting path and must not fail entirely because of one broken chain.
    try:
        canonical_person_id: str | None = resolve_canonical_person_id(db, person_id)
        canonical_resolution_error: str | None = None
    except CanonicalResolutionError as exc:
        canonical_person_id = None
        canonical_resolution_error = str(exc)

    open_threads: list[str] = person.get("open_threads", [])
    name_tokens = _name_and_alias_tokens(person)

    # --- Organization (canonical, via org_id) ---
    organization = None
    other_people_at_org: list[dict[str, Any]] = []
    org_id = person.get("org_id")
    if org_id:
        organization = OrganizationRepository(db).find_one({"id": org_id})
        other_people_at_org = [
            p for p in person_repo.find_many({"org_id": org_id}) if p["id"] != person_id
        ]

    # --- Emails (canonical: entities_referenced.people) ---
    emails = list(
        db.emails.find(
            {"entities_referenced.people": person_id},
            {"_id": 0, "message_id": 1, "subject": 1, "timestamp": 1, "thread_id": 1},
        )
    )

    # --- Threads (canonical: person's own open_threads) ---
    threads = ThreadRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []

    # --- Related people (canonical: shared open_threads) ---
    related_people = [
        p
        for p in person_repo.find_many({"open_threads": {"$in": open_threads}})
        if p["id"] != person_id
    ] if open_threads else []

    def _canonical_then_legacy(
        canonical_query: dict[str, Any],
        legacy_pool: list[dict[str, Any]],
        legacy_name_fields: list[str],
    ) -> list[dict[str, Any]]:
        canonical = list(canonical_query["repo"].find_many(canonical_query["filter"]))
        if canonical:
            return _tag(canonical, "canonical_person_id")
        if org_id:
            org_canonical = [d for d in legacy_pool if d.get("org_id") == org_id]
            if org_canonical:
                return _tag(org_canonical, "canonical_org_id")
        legacy = _legacy_name_match(legacy_pool, name_tokens, legacy_name_fields)
        return _tag(legacy, "legacy_thread_scoped_name_match")

    # --- Commitments: canonical person_id first, then org_id, then legacy name match ---
    commitments_in_threads = (
        CommitmentRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
    )
    commitments = _canonical_then_legacy(
        {"repo": CommitmentRepository(db), "filter": {"person_id": person_id}},
        commitments_in_threads,
        ["owed_by", "owed_to"],
    )

    # --- Meetings: canonical person_ids first, then org_id, then legacy name match ---
    meetings_in_threads = MeetingRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
    meetings = _canonical_then_legacy(
        {"repo": MeetingRepository(db), "filter": {"person_ids": person_id}},
        meetings_in_threads,
        ["attendees"],
    )

    # --- Follow-ups: canonical person_id (inherited from Commitment) or thread-scoped ---
    follow_ups_canonical = FollowUpRepository(db).find_many({"person_id": person_id})
    if follow_ups_canonical:
        follow_ups = _tag(follow_ups_canonical, "canonical_person_id")
    else:
        follow_ups_in_threads = (
            FollowUpRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
        )
        if org_id:
            org_matches = [f for f in follow_ups_in_threads if f.get("org_id") == org_id]
            follow_ups = (
                _tag(org_matches, "canonical_org_id")
                if org_matches
                else _tag(follow_ups_in_threads, "legacy_thread_scoped_name_match")
            )
        else:
            follow_ups = _tag(follow_ups_in_threads, "legacy_thread_scoped_name_match")

    # --- Projects: canonical person_ids first, then org_id, then legacy collaborator match ---
    projects_canonical = ProjectRepository(db).find_many({"person_ids": person_id})
    if projects_canonical:
        projects = _tag(projects_canonical, "canonical_person_id")
    elif org_id and ProjectRepository(db).find_many({"org_id": org_id}):
        projects = _tag(ProjectRepository(db).find_many({"org_id": org_id}), "canonical_org_id")
    else:
        projects_in_org = ProjectRepository(db).find_many({"entity": organization["name"]}) if organization else []
        projects = _tag(_legacy_name_match(projects_in_org, name_tokens, ["collaborators"]), "legacy_thread_scoped_name_match")

    # --- Knowledge: canonical person_id first, then org_id, then thread-scoped only ---
    knowledge_canonical = KnowledgeRepository(db).find_many({"person_id": person_id})
    if knowledge_canonical:
        knowledge = _tag(knowledge_canonical, "canonical_person_id")
    elif org_id and KnowledgeRepository(db).find_many({"org_id": org_id}):
        knowledge = _tag(KnowledgeRepository(db).find_many({"org_id": org_id}), "canonical_org_id")
    else:
        knowledge_in_threads = KnowledgeRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
        knowledge = _tag(knowledge_in_threads, "thread_scoped_only")

    # --- Reply drafts: canonical person_id first, else thread-scoped only ---
    reply_drafts_canonical = ReplyDraftRepository(db).find_many({"person_id": person_id})
    if reply_drafts_canonical:
        reply_drafts = _tag(reply_drafts_canonical, "canonical_person_id")
    else:
        reply_drafts_in_threads = (
            ReplyDraftRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
        )
        reply_drafts = _tag(reply_drafts_in_threads, "thread_scoped_only")

    # --- Calendar actions: canonical person_id first, else thread-scoped only ---
    calendar_actions_canonical = CalendarActionRepository(db).find_many({"person_id": person_id})
    if calendar_actions_canonical:
        calendar_actions = _tag(calendar_actions_canonical, "canonical_person_id")
    else:
        calendar_actions_in_threads = (
            CalendarActionRepository(db).find_many({"thread_id": {"$in": open_threads}}) if open_threads else []
        )
        calendar_actions = _tag(calendar_actions_in_threads, "thread_scoped_only")

    return {
        "person": person,
        "canonical_person_id": canonical_person_id,
        "canonical_resolution_error": canonical_resolution_error,
        "organization": {"basis": "canonical_id", "data": organization},
        "emails": {"basis": "canonical_id", "data": emails},
        "threads": {"basis": "canonical_id", "data": threads},
        "related_people": {"basis": "canonical_id_shared_thread", "data": related_people},
        "other_people_at_org": {"basis": "canonical_id", "data": other_people_at_org},
        "commitments": commitments,
        "meetings": meetings,
        "follow_ups": follow_ups,
        "projects": projects,
        "knowledge": knowledge,
        "reply_drafts": reply_drafts,
        "calendar_actions": calendar_actions,
    }


def get_organization_context(db: Database, org_id: str) -> dict[str, Any] | None:
    """Company/organization 360-degree view -- the same canonical-id-first principle
    as get_person_context, applied via org_id instead of person_id. Every category
    here queries a real canonical `org_id` field written by Stage 2 of the entity
    migration (app.entity_migration) -- no new identity-resolution algorithm, no
    name-text matching of company names (organizations are never merged/matched by
    display name anywhere in this codebase -- see resolve_organization). Returns None
    if org_id doesn't exist.
    """
    organization = OrganizationRepository(db).find_one({"id": org_id})
    if organization is None:
        return None

    people = PersonRepository(db).find_many({"org_id": org_id})
    threads = ThreadRepository(db).find_many({"org_ids": org_id})
    commitments = CommitmentRepository(db).find_many({"org_id": org_id})
    meetings = MeetingRepository(db).find_many({"org_id": org_id})
    follow_ups = FollowUpRepository(db).find_many({"org_id": org_id})
    projects = ProjectRepository(db).find_many({"org_id": org_id})
    knowledge = KnowledgeRepository(db).find_many({"org_id": org_id})
    reply_drafts = ReplyDraftRepository(db).find_many({"org_id": org_id})
    calendar_actions = CalendarActionRepository(db).find_many({"org_id": org_id})

    return {
        "organization": organization,
        "people": {"basis": "canonical_org_id", "data": people},
        "threads": {"basis": "canonical_org_id", "data": threads},
        "commitments": {"basis": "canonical_org_id", "data": commitments},
        "meetings": {"basis": "canonical_org_id", "data": meetings},
        "follow_ups": {"basis": "canonical_org_id", "data": follow_ups},
        "projects": {"basis": "canonical_org_id", "data": projects},
        "knowledge": {"basis": "canonical_org_id", "data": knowledge},
        "reply_drafts": {"basis": "canonical_org_id", "data": reply_drafts},
        "calendar_actions": {"basis": "canonical_org_id", "data": calendar_actions},
    }
