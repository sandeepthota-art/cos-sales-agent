"""Phase 20: a reusable, read-only production-invariant verification layer.

Every function here only ever calls find()/find_many()/find_one() (directly, or
through the existing repository classes) -- nothing here writes to MongoDB, creates
an index, retires a Person, or modifies application state in any way, exactly like
app.entity_reconciliation and app.duplicate_impact_analysis. Nothing here hardcodes
a current Atlas id, name, or count -- every check operates generically over
whatever people/organizations/records actually exist, so it holds for tomorrow's
data exactly as it does for today's.

This is a verification layer, not a migration tool: it never repairs anything it
finds wrong, and never decides that something IS wrong on its own authority beyond
reporting the fact -- consuming code (or a human) decides what, if anything, to do
about a finding.
"""

from collections import defaultdict
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
from app.entities.lifecycle import CanonicalResolutionError, is_person_active, is_person_merged, resolve_canonical_person_id

_VALID_STATUS_VALUES = {"active", "merged"}


# --- Section 2: Person lifecycle invariants ------------------------------------------------


def verify_person_lifecycle(db: Database) -> dict[str, Any]:
    """Generic ACTIVE/MERGED contract verification -- see app.entities.lifecycle for
    the contract itself. Every merged Person's chain is actually walked via
    resolve_canonical_person_id, so this reuses the one real implementation rather
    than re-deriving cycle/depth/self-reference detection here.
    """
    people = PersonRepository(db).find_many({})
    by_id = {p["id"]: p for p in people}
    active = [p for p in people if is_person_active(p)]
    merged = [p for p in people if is_person_merged(p)]

    invalid_status_values = [
        {"person_id": p["id"], "status": p.get("status")}
        for p in people
        if p.get("status") is not None and p.get("status") not in _VALID_STATUS_VALUES
    ]

    missing_merged_into: list[str] = []
    invalid_merged_into_target: list[str] = []
    self_references: list[str] = []
    circular_chains: list[str] = []
    depth_violations: list[str] = []
    other_resolution_errors: list[dict[str, str]] = []
    merged_resolving_to_active = 0

    for p in merged:
        target = p.get("merged_into")
        if not target:
            missing_merged_into.append(p["id"])
            continue
        if target == p["id"]:
            self_references.append(p["id"])
            continue
        if target not in by_id:
            invalid_merged_into_target.append(p["id"])
            continue
        try:
            resolve_canonical_person_id(db, p["id"])
            merged_resolving_to_active += 1
        except CanonicalResolutionError as exc:
            message = str(exc)
            if "circular" in message:
                circular_chains.append(p["id"])
            elif "exceeded" in message:
                depth_violations.append(p["id"])
            else:
                other_resolution_errors.append({"person_id": p["id"], "error": message})

    active_resolving_to_self = sum(1 for p in active if resolve_canonical_person_id(db, p["id"]) == p["id"])

    return {
        "total_persons": len(people),
        "active_persons": len(active),
        "merged_persons": len(merged),
        "invalid_status_values": invalid_status_values,
        "merged_with_missing_merged_into": missing_merged_into,
        "merged_with_invalid_merged_into_target": invalid_merged_into_target,
        "self_references": self_references,
        "circular_chains": circular_chains,
        "depth_violations": depth_violations,
        "other_resolution_errors": other_resolution_errors,
        "merged_resolving_to_active": merged_resolving_to_active,
        "active_resolving_to_self": active_resolving_to_self,
    }


# --- Section 5: Organization invariants -----------------------------------------------------


def verify_organization_invariants(db: Database) -> dict[str, Any]:
    people = PersonRepository(db).find_many({})
    orgs = OrganizationRepository(db).find_many({})
    org_by_id = {o["id"]: o for o in orgs}

    dangling_org_id = [p["id"] for p in people if p.get("org_id") and p["org_id"] not in org_by_id]

    domain_owners: dict[str, list[str]] = defaultdict(list)
    for o in orgs:
        if o.get("domain"):
            domain_owners[o["domain"]].append(o["id"])
    duplicate_domains = {domain: ids for domain, ids in domain_owners.items() if len(ids) > 1}

    return {
        "total_organizations": len(orgs),
        "people_with_org_id": sum(1 for p in people if p.get("org_id")),
        "dangling_org_id_person_ids": dangling_org_id,
        "duplicate_domains": duplicate_domains,
    }


# --- Section 6/7: cross-collection referential integrity + merged-reference audit ----------

# (label, repository, id-extractor, [(field, is_list, referenced_kind), ...])
_REF_SPECS: list[tuple[str, type, Any, list[tuple[str, bool, str]]]] = [
    ("threads", ThreadRepository, lambda d: d["thread_id"], [("person_ids", True, "person"), ("org_ids", True, "org")]),
    ("commitments", CommitmentRepository, lambda d: d["id"], [("person_id", False, "person"), ("org_id", False, "org")]),
    ("meetings", MeetingRepository, lambda d: d["id"], [("person_ids", True, "person"), ("org_id", False, "org")]),
    ("follow_ups", FollowUpRepository, lambda d: d["id"], [("person_id", False, "person"), ("org_id", False, "org")]),
    ("projects", ProjectRepository, lambda d: d["id"], [("person_ids", True, "person"), ("org_id", False, "org")]),
    ("knowledge_items", KnowledgeRepository, lambda d: d["knowledge_id"], [("person_id", False, "person"), ("org_id", False, "org")]),
    ("reply_drafts", ReplyDraftRepository, lambda d: d["source_email_id"], [("person_id", False, "person"), ("org_id", False, "org")]),
    (
        "calendar_actions", CalendarActionRepository, lambda d: f"{d['thread_id']}:{d['meeting_fingerprint']}",
        [("person_id", False, "person"), ("org_id", False, "org"), ("meeting_id", False, "meeting")],
    ),
]


def verify_cross_collection_referential_integrity(db: Database) -> dict[str, Any]:
    """For every applicable person_id/person_ids/org_id/org_ids/meeting_id field
    across all 8 downstream collections: flags a dangling reference (points at an
    id that doesn't exist) and separately flags a reference that's structurally
    valid but points at a MERGED Person -- Type A / Type D from the Phase 20 merged-
    reference audit (Section 7); B/C (legacy thread-scoped or human-readable-only)
    are outside this function's scope by construction, since it only ever reads
    already-canonical id fields, never free text.

    List-valued fields are read defensively: a real document can store one as an
    explicit None (not merely absent or []) -- see the Phase 18 regression this
    guards against (app.duplicate_consolidation._downstream_ids's original bug).
    """
    person_ids = {p["id"] for p in PersonRepository(db).find_many({})}
    merged_person_ids = {p["id"] for p in PersonRepository(db).find_many({}) if is_person_merged(p)}
    org_ids = {o["id"] for o in OrganizationRepository(db).find_many({})}
    meeting_ids = {m["id"] for m in MeetingRepository(db).find_many({})}
    valid_ids_by_kind = {"person": person_ids, "org": org_ids, "meeting": meeting_ids}

    report: dict[str, Any] = {}
    for label, repo_cls, key_fn, fields in _REF_SPECS:
        docs = repo_cls(db).find_many({})
        dangling: list[dict[str, Any]] = []
        merged_refs: list[dict[str, Any]] = []
        for doc in docs:
            for field, is_list, kind in fields:
                value = doc.get(field)
                values = (value or []) if is_list else ([value] if value else [])
                valid_ids = valid_ids_by_kind[kind]
                for v in values:
                    if v not in valid_ids:
                        dangling.append({"document_id": key_fn(doc), "field": field, "value": v})
                    elif kind == "person" and v in merged_person_ids:
                        merged_refs.append({"document_id": key_fn(doc), "field": field, "value": v})
        report[label] = {
            "total_checked": len(docs),
            "dangling_references": dangling,
            "merged_person_references": merged_refs,
        }
    return report


# --- Section 9: calendar safety, verified generically over ALL calendar_actions -------------


def verify_calendar_safety(db: Database) -> dict[str, Any]:
    actions = CalendarActionRepository(db).find_many({})
    violations = [
        {"thread_id": a.get("thread_id"), "meeting_fingerprint": a.get("meeting_fingerprint")}
        for a in actions
        if (a.get("event") or {}).get("attendees")
    ]
    return {"total_calendar_actions": len(actions), "external_attendee_violations": violations, "safe": len(violations) == 0}


# --- Section 10: knowledge safety, verified generically over ALL knowledge_items ------------


def verify_knowledge_safety(db: Database) -> dict[str, Any]:
    items = KnowledgeRepository(db).find_many({})
    merged_person_ids = {p["id"] for p in PersonRepository(db).find_many({}) if is_person_merged(p)}

    explicit_person = [i for i in items if i.get("person_id")]
    explicit_person_merged = [i for i in explicit_person if i["person_id"] in merged_person_ids]
    org_only = [i for i in items if i.get("org_id") and not i.get("person_id")]
    thread_level_or_unresolved = [i for i in items if not i.get("person_id") and not i.get("org_id")]

    return {
        "total_knowledge_items": len(items),
        "explicit_person_links": len(explicit_person),
        "explicit_person_links_pointing_at_merged_person": [i["knowledge_id"] for i in explicit_person_merged],
        "org_only_links": len(org_only),
        "thread_level_or_unresolved": len(thread_level_or_unresolved),
    }
