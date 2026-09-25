"""Controlled, resumable migration of historical Atlas data into the canonical
Person/Organization architecture (app.entities.models.Organization, Person.org_id,
and the person_id/org_id/person_ids fields added to Commitment/Meeting/FollowUp/
Project/KnowledgeItem/ReplyDraft/CalendarAction).

SAFETY, BY CONSTRUCTION:
  - --dry-run NEVER calls upsert_by_key or any other write. Every dry-run function in
    this module only ever calls find()/find_many()/find_one().
  - This module reuses the application's OWN identity/matching primitives -- it does
    not reimplement them:
      * app.entities.resolution.resolve_organization (domain-only org identity)
      * app.entities.resolution.match_resolved_person_by_name (name+org matching,
        ambiguity-safe)
      * app.pipeline._thread_resolved_people (the authoritative "who's already
        resolved in this thread" lookup)
      * app.pipeline._link_thread_to_entities / _link_knowledge_to_entities (called
        UNCHANGED for the real write; this module never re-derives their logic for
        execution, only mirrors their read-only matching for dry-run preview)
  - Every write only ever fills a MISSING canonical field. An existing person_id/
    org_id/org_id is never overwritten, and no pre-existing human-readable field
    (owed_by, owed_to, attendees, collaborators, org, email, id, thread_id) is ever
    modified. Stage 1's execution self-verifies this per record (see
    _capture_immutable_fields/_verify_immutable_fields below) and treats any
    discrepancy as a hard error, not a warning.
  - Never merges or deletes a Person. Duplicate candidates are reported only (this
    module imports app.entity_reconciliation.find_duplicate_candidates verbatim --
    a second, competing duplicate-detection algorithm is not built here).
  - Never creates a MongoDB index except under the separate, explicitly-invoked
    "indexes" stage -- never as a side effect of any other stage.

CLI usage:
    python -m app.entity_migration --stage organizations --dry-run
    python -m app.entity_migration --stage organizations --limit 50
    python -m app.entity_migration --stage organizations --resume <run_id>
    python -m app.entity_migration --stage organizations --verify
    python -m app.entity_migration --stage canonical-references --dry-run
    python -m app.entity_migration --stage canonical-references
    python -m app.entity_migration --stage duplicate-review
    python -m app.entity_migration --stage indexes --dry-run
    python -m app.entity_migration --stage indexes
"""

import argparse
import sys
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.database.mongodb import get_client, initialize_database
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    MigrationRunRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entities.resolution import match_resolved_person_by_name, resolve_canonical_person_for_email, resolve_organization
from app.entity_reconciliation import find_duplicate_candidates
from app.knowledge.normalize import normalize_text
from app.pipeline import _link_knowledge_to_entities, _link_thread_to_entities, _thread_resolved_people


def _is_malformed_email(email: str) -> bool:
    """A value that cannot possibly be a single real email address -- semicolon- or
    comma-separated (multiple addresses combined into one field), or containing zero
    or more than one '@'. Real case found in the Atlas dry-run: PER-444's stored
    email is "simpsonlowell@gmail.com; lowell@outfront.com" -- naive domain
    extraction would derive the nonsense domain "gmail.com; lowell@outfront.com" and
    create a bogus Organization from it. Stage 1 must never derive a domain, create
    an Organization, or assign org_id from a value like this, and must never guess
    which of the combined addresses is authoritative.
    """
    return ";" in email or "," in email or email.count("@") != 1


def _domain_of(email: str | None) -> str | None:
    if not email or _is_malformed_email(email):
        return None
    return email.strip().lower().split("@", 1)[1] or None


# --- Checkpointing (Phase 12) ---------------------------------------------------------


def start_run(db: Database, stage: str) -> str:
    run_id = f"migrun_{uuid.uuid4().hex}"
    MigrationRunRepository(db).upsert_by_key(
        {"run_id": run_id},
        {
            "run_id": run_id, "stage": stage, "started_at": datetime.now(timezone.utc).isoformat(),
            "completed_at": None, "status": "running", "last_processed_id": None,
            "processed_count": 0, "updated_count": 0, "skipped_count": 0, "error_count": 0,
        },
    )
    return run_id


def get_run(db: Database, run_id: str) -> dict[str, Any] | None:
    return MigrationRunRepository(db).find_one({"run_id": run_id})


def _update_run(db: Database, run_id: str, **fields: Any) -> None:
    existing = get_run(db, run_id)
    if existing is None:
        raise ValueError(f"no migration run found for run_id={run_id!r}")
    MigrationRunRepository(db).upsert_by_key({"run_id": run_id}, {**existing, **fields})


def complete_run(db: Database, run_id: str, status: str = "completed") -> None:
    _update_run(db, run_id, status=status, completed_at=datetime.now(timezone.utc).isoformat())


# --- Stage "organizations": dry run (Phase 4) ------------------------------------------


def stage_organizations_dry_run(db: Database, limit: int | None = None) -> dict[str, Any]:
    people = PersonRepository(db).find_many({})
    if limit:
        people = sorted(people, key=lambda p: p["id"])[:limit]

    no_email = [p for p in people if not p.get("email")]
    with_email = [p for p in people if p.get("email")]
    malformed_email = [p for p in with_email if _is_malformed_email(p["email"])]
    invalid_email = [p for p in with_email if not _is_malformed_email(p["email"]) and not _domain_of(p["email"])]
    valid_email = [p for p in with_email if not _is_malformed_email(p["email"]) and _domain_of(p["email"])]

    malformed_cases = [
        {"person_id": p["id"], "email": p["email"], "reason": "malformed/multiple email", "action": "organization skipped"}
        for p in sorted(malformed_email, key=lambda p: p["id"])
    ]

    existing_orgs_by_domain = {o["domain"]: o for o in OrganizationRepository(db).find_many({}) if o.get("domain")}
    domain_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in valid_email:
        domain_groups[_domain_of(p["email"])].append(p)

    would_create_domains = sorted(d for d in domain_groups if d not in existing_orgs_by_domain)
    already_linked = [p for p in valid_email if p.get("org_id")]
    would_receive_org_id = [p for p in valid_email if not p.get("org_id")]

    conflicts = []
    for p in already_linked:
        domain = _domain_of(p["email"])
        expected = existing_orgs_by_domain.get(domain)
        if expected and expected["id"] != p["org_id"]:
            conflicts.append(
                {"person_id": p["id"], "domain": domain, "current_org_id": p["org_id"], "expected_org_id": expected["id"]}
            )

    sample = []
    for p in sorted(would_receive_org_id, key=lambda p: p["id"])[:10]:
        domain = _domain_of(p["email"])
        target = existing_orgs_by_domain.get(domain)
        sample.append(
            {
                "person_id": p["id"], "email": p["email"], "domain": domain,
                "would_map_to_org_id": target["id"] if target else "(new organization)",
            }
        )

    return {
        "people_examined": len(people),
        "people_with_valid_email": len(valid_email),
        "people_with_missing_email": len(no_email),
        "people_with_invalid_email": len(invalid_email),
        "people_with_malformed_email": len(malformed_email),
        "malformed_email_cases": malformed_cases,
        "unique_email_domains": len(domain_groups),
        "existing_organizations": len(existing_orgs_by_domain),
        "organizations_that_would_be_created": len(would_create_domains),
        "would_create_domains": would_create_domains,
        "people_that_would_receive_org_id": len(would_receive_org_id),
        "people_already_correctly_linked": len(already_linked),
        "conflicts": conflicts,
        "sample_mappings": sample,
    }


# --- Stage "organizations": execution (Phase 5) ----------------------------------------


_IMMUTABLE_PERSON_FIELDS = ("id", "email", "org")


def _capture_immutable(doc: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: doc.get(f) for f in fields}


def stage_organizations_execute(
    db: Database, limit: int | None = None, resume_run_id: str | None = None
) -> dict[str, Any]:
    run_id = resume_run_id or start_run(db, "organizations")
    run = get_run(db, run_id)
    if run is None:
        raise ValueError(f"no migration run found for run_id={run_id!r}")

    people = sorted(PersonRepository(db).find_many({}), key=lambda p: p["id"])
    if run.get("last_processed_id"):
        people = [p for p in people if p["id"] > run["last_processed_id"]]
    if limit:
        people = people[:limit]

    processed, updated, skipped, error_count = (
        run.get("processed_count", 0), run.get("updated_count", 0),
        run.get("skipped_count", 0), run.get("error_count", 0),
    )
    error_details: list[dict[str, Any]] = []
    person_repo = PersonRepository(db)

    for p in people:
        try:
            if p.get("org_id") or not p.get("email") or _is_malformed_email(p["email"]):
                # Malformed/combined email values (e.g. PER-444's real
                # "a@x; b@y" case) must never reach resolve_organization at all --
                # it does its own naive domain split and would produce a nonsense
                # organization from a value like this. Never guess which address is
                # authoritative; leave the person unresolved.
                skipped += 1
            else:
                before = _capture_immutable(p, _IMMUTABLE_PERSON_FIELDS)
                org_id = resolve_organization(db, p["email"], p.get("org"))
                if org_id is None:
                    skipped += 1
                else:
                    person_repo.upsert_by_key({"id": p["id"]}, {**p, "org_id": org_id})
                    after = _capture_immutable(person_repo.find_one({"id": p["id"]}), _IMMUTABLE_PERSON_FIELDS)
                    if before != after:
                        raise RuntimeError(
                            f"immutable field changed during org_id backfill: before={before} after={after}"
                        )
                    updated += 1
        except Exception as exc:  # noqa: BLE001 - one bad record must not abort the whole run
            error_count += 1
            error_details.append({"person_id": p["id"], "error": str(exc)})
        processed += 1
        _update_run(
            db, run_id, last_processed_id=p["id"], processed_count=processed,
            updated_count=updated, skipped_count=skipped, error_count=error_count,
        )

    complete_run(db, run_id, status="completed" if error_count == 0 else "completed_with_errors")
    return {
        "run_id": run_id, "organizations_created_or_reused": updated,
        "people_updated": updated, "people_skipped": skipped,
        "errors": error_count, "error_details": error_details,
    }


# --- Stage "organizations": verification (Phase 6) -------------------------------------


def verify_stage_organizations(db: Database) -> dict[str, Any]:
    people = PersonRepository(db).find_many({})
    orgs = OrganizationRepository(db).find_many({})
    org_by_id = {o["id"]: o for o in orgs}

    domain_owners: dict[str, list[str]] = defaultdict(list)
    for o in orgs:
        if o.get("domain"):
            domain_owners[o["domain"]].append(o["id"])
    duplicate_domains = {d: ids for d, ids in domain_owners.items() if len(ids) > 1}

    failures: list[str] = []
    for p in people:
        domain = _domain_of(p.get("email"))
        if domain:
            if not p.get("org_id"):
                failures.append(f"{p['id']}: has a valid email ({domain}) but no org_id")
            elif p["org_id"] not in org_by_id:
                failures.append(f"{p['id']}: org_id {p['org_id']!r} does not reference an existing Organization")
            elif org_by_id[p["org_id"]].get("domain") != domain:
                failures.append(
                    f"{p['id']}: org_id {p['org_id']!r} points to domain "
                    f"{org_by_id[p['org_id']].get('domain')!r}, expected {domain!r}"
                )
        elif p.get("org_id"):
            failures.append(f"{p['id']}: has no usable email but has org_id {p['org_id']!r} (should be unresolved)")
    if duplicate_domains:
        failures.append(f"duplicate Organization documents for the same domain: {duplicate_domains}")

    return {
        "passed": len(failures) == 0,
        "failures": failures,
        "people_checked": len(people),
        "organizations_checked": len(orgs),
    }


# --- Stage "canonical-references": shared helpers --------------------------------------


def _missing(doc: dict[str, Any], field: str) -> bool:
    value = doc.get(field)
    return not value  # covers missing key, None, and empty list identically


def _project_thread_ids(db: Database, project_id: str) -> set[str]:
    """Project has no thread_id field of its own -- the only canonical path from a
    Project back to a thread is via the already-canonical emails.entities_referenced.
    projects reference (an email that mentioned this project also has its own
    thread_id). This is reading an existing canonical reference, not a new one.
    """
    return {
        e["thread_id"]
        for e in db.emails.find({"entities_referenced.projects": project_id}, {"_id": 0, "thread_id": 1})
        if e.get("thread_id")
    }


# --- Stage "canonical-references": dry run (Phase 8) ------------------------------------


def _propose_commitment(doc: dict[str, Any]) -> dict[str, Any]:
    base = {"collection": "commitments", "record_id": doc["id"], "thread_id": doc.get("thread_id")}
    if _missing(doc, "person_id") is False:
        return {**base, "status": "SKIPPED_ALREADY_CANONICAL"}
    thread_id = doc.get("thread_id")
    if not thread_id:
        return {**base, "status": "SKIPPED_NO_THREAD"}
    people = _thread_resolved_people(_DB_FOR_HELPERS[0], thread_id)
    match = match_resolved_person_by_name(people, doc.get("owed_to")) or match_resolved_person_by_name(
        people, doc.get("owed_by")
    )
    if match is None:
        return {**base, "status": "SKIPPED_AMBIGUOUS", "current_owed_by": doc.get("owed_by"), "current_owed_to": doc.get("owed_to")}
    return {
        **base,
        "current_owed_by": doc.get("owed_by"), "current_owed_to": doc.get("owed_to"),
        "proposed_person_id": match["id"], "proposed_org_id": match.get("org_id"),
        "matched_person": match["name"], "reason": "thread canonical person match",
        "confidence": "HIGH", "status": "WOULD_UPDATE",
    }


# A tiny closure box so the small per-record propose_* helpers above/below don't all
# need `db` threaded through every call explicitly -- set once per dry-run/execute call.
_DB_FOR_HELPERS: list[Database] = [None]  # type: ignore[list-item]


def _propose_meeting(doc: dict[str, Any]) -> dict[str, Any]:
    base = {"collection": "meetings", "record_id": doc["id"], "thread_id": doc.get("thread_id")}
    if doc.get("person_ids"):
        return {**base, "status": "SKIPPED_ALREADY_CANONICAL"}
    thread_id = doc.get("thread_id")
    if not thread_id:
        return {**base, "status": "SKIPPED_NO_THREAD"}
    people = _thread_resolved_people(_DB_FOR_HELPERS[0], thread_id)
    matches = [match_resolved_person_by_name(people, a) for a in doc.get("attendees", [])]
    matches = list({m["id"]: m for m in matches if m is not None}.values())
    if not matches:
        return {**base, "status": "SKIPPED_NO_MATCH", "current_attendees": doc.get("attendees")}
    return {
        **base, "current_attendees": doc.get("attendees"),
        "proposed_person_ids": [m["id"] for m in matches],
        "proposed_org_id": next((m.get("org_id") for m in matches if m.get("org_id")), None),
        "reason": "attendee name matched against thread canonical people",
        "confidence": "HIGH", "status": "WOULD_UPDATE",
    }


def _propose_project(doc: dict[str, Any]) -> dict[str, Any]:
    base = {"collection": "projects", "record_id": doc["id"]}
    if doc.get("person_ids"):
        return {**base, "status": "SKIPPED_ALREADY_CANONICAL"}
    thread_ids = _project_thread_ids(_DB_FOR_HELPERS[0], doc["id"])
    if not thread_ids:
        return {**base, "status": "SKIPPED_NO_THREAD", "current_entity": doc.get("entity")}
    entity_normalized = normalize_text(doc.get("entity") or "")
    matches: dict[str, dict[str, Any]] = {}
    for thread_id in thread_ids:
        for person in _thread_resolved_people(_DB_FOR_HELPERS[0], thread_id):
            if entity_normalized and normalize_text(person.get("org") or "") == entity_normalized:
                matches[person["id"]] = person
    if not matches:
        return {**base, "status": "SKIPPED_NO_MATCH", "current_entity": doc.get("entity")}
    org_id = next((p.get("org_id") for p in matches.values() if p.get("org_id")), None)
    return {
        **base, "current_entity": doc.get("entity"), "source_thread_ids": sorted(thread_ids),
        "proposed_person_ids": sorted(matches), "proposed_org_id": org_id,
        "reason": "org-matched person(s) resolved in a thread that mentioned this project",
        "confidence": "MEDIUM", "status": "WOULD_UPDATE",
    }


def _propose_reply_draft(doc: dict[str, Any]) -> dict[str, Any]:
    # record_id is the natural lookup key (ReplyDraftRepository's own unique index is
    # on source_email_id, not reply_id) -- kept consistent with how the execute loop
    # below re-finds this exact document.
    base = {"collection": "reply_drafts", "record_id": doc["source_email_id"], "thread_id": doc.get("thread_id")}
    if doc.get("person_id"):
        return {**base, "status": "SKIPPED_ALREADY_CANONICAL"}
    email = db_helper_find_email(doc.get("source_email_id"))
    sender_email = ((email or {}).get("from") or {}).get("email")
    if not sender_email:
        return {**base, "status": "SKIPPED_NO_THREAD"}
    # Phase 20.1: lifecycle-aware -- this stage is idempotent and rerunnable, so a
    # future rerun (e.g. via --resume, or reprocessing a backlog) must never backfill
    # person_id with a Person that's since been merged. See
    # app.entities.resolution.resolve_canonical_person_for_email.
    person = resolve_canonical_person_for_email(_DB_FOR_HELPERS[0], sender_email)
    if person is None:
        return {**base, "status": "SKIPPED_NO_MATCH"}
    return {
        **base, "proposed_person_id": person["id"], "proposed_org_id": person.get("org_id"),
        "reason": "triggering email's sender", "confidence": "HIGH", "status": "WOULD_UPDATE",
    }


def db_helper_find_email(message_id: str | None) -> dict[str, Any] | None:
    if not message_id:
        return None
    return _DB_FOR_HELPERS[0].emails.find_one({"message_id": message_id}, {"_id": 0, "from": 1})


def _propose_calendar_action(doc: dict[str, Any], meetings_by_thread: dict[str, list[str]]) -> dict[str, Any]:
    base = {
        "collection": "calendar_actions",
        "record_id": f"{doc['thread_id']}:{doc['meeting_fingerprint']}",
        "thread_id": doc.get("thread_id"),
    }
    if doc.get("person_id"):
        return {**base, "status": "SKIPPED_ALREADY_CANONICAL"}
    thread_id = doc.get("thread_id")
    people = _thread_resolved_people(_DB_FOR_HELPERS[0], thread_id) if thread_id else []
    if len(people) != 1:
        return {**base, "status": "SKIPPED_AMBIGUOUS", "reason": f"{len(people)} distinct people resolved in this thread"}
    person = people[0]
    meeting_ids = meetings_by_thread.get(thread_id, [])
    return {
        **base,
        "proposed_person_id": person["id"], "proposed_org_id": person.get("org_id"),
        "proposed_meeting_id": meeting_ids[0] if len(meeting_ids) == 1 else None,
        "reason": "sole person resolved in this thread", "confidence": "MEDIUM", "status": "WOULD_UPDATE",
    }


def stage_canonical_references_dry_run(db: Database, limit: int | None = None) -> dict[str, Any]:
    _DB_FOR_HELPERS[0] = db
    proposals: list[dict[str, Any]] = []

    threads = ThreadRepository(db).find_many({})
    for t in (threads[:limit] if limit else threads):
        missing = not t.get("person_ids") or not t.get("org_ids")
        proposals.append(
            {
                "collection": "threads", "record_id": t["thread_id"],
                "status": "WOULD_UPDATE" if missing else "SKIPPED_ALREADY_CANONICAL",
                "reason": "recompute person_ids/org_ids from thread's resolved people",
            }
        )

    commitments = CommitmentRepository(db).find_many({})
    for c in (commitments[:limit] if limit else commitments):
        proposals.append(_propose_commitment(c))

    meetings = MeetingRepository(db).find_many({})
    for m in (meetings[:limit] if limit else meetings):
        proposals.append(_propose_meeting(m))

    commitment_by_id = {c["id"]: c for c in commitments}
    commitment_proposal_by_id = {
        p["record_id"]: p for p in proposals if p["collection"] == "commitments"
    }
    follow_ups = FollowUpRepository(db).find_many({})
    for f in (follow_ups[:limit] if limit else follow_ups):
        base = {"collection": "follow_ups", "record_id": f["id"], "thread_id": f.get("thread_id")}
        if f.get("person_id"):
            proposals.append({**base, "status": "SKIPPED_ALREADY_CANONICAL"})
            continue
        commitment_id = f.get("commitment_id")
        source = commitment_proposal_by_id.get(commitment_id) if commitment_id else None
        person_id = (source or {}).get("proposed_person_id") or (commitment_by_id.get(commitment_id) or {}).get("person_id")
        org_id = (source or {}).get("proposed_org_id") or (commitment_by_id.get(commitment_id) or {}).get("org_id")
        if not person_id:
            proposals.append({**base, "status": "SKIPPED_NO_MATCH", "reason": "parent commitment has no resolvable person"})
            continue
        proposals.append(
            {
                **base, "proposed_person_id": person_id, "proposed_org_id": org_id,
                "reason": "inherited from parent commitment", "confidence": "HIGH", "status": "WOULD_UPDATE",
            }
        )

    projects = ProjectRepository(db).find_many({})
    for p in (projects[:limit] if limit else projects):
        proposals.append(_propose_project(p))

    knowledge_items = KnowledgeRepository(db).find_many({})
    for k in (knowledge_items[:limit] if limit else knowledge_items):
        base = {"collection": "knowledge_items", "record_id": k["knowledge_id"], "thread_id": k.get("thread_id")}
        if k.get("person_id") or k.get("org_id"):
            proposals.append({**base, "status": "SKIPPED_ALREADY_CANONICAL"})
            continue
        proposals.append(
            {**base, "status": "WOULD_ATTEMPT", "reason": "delegated to app.pipeline._link_knowledge_to_entities unchanged"}
        )

    reply_drafts = ReplyDraftRepository(db).find_many({})
    for r in (reply_drafts[:limit] if limit else reply_drafts):
        proposals.append(_propose_reply_draft(r))

    meetings_by_thread: dict[str, list[str]] = defaultdict(list)
    for m in meetings:
        if m.get("thread_id"):
            meetings_by_thread[m["thread_id"]].append(m["id"])
    calendar_actions = CalendarActionRepository(db).find_many({})
    for a in (calendar_actions[:limit] if limit else calendar_actions):
        proposals.append(_propose_calendar_action(a, meetings_by_thread))

    counts = defaultdict(lambda: defaultdict(int))
    for p in proposals:
        counts[p["collection"]][p["status"]] += 1

    return {"proposals": proposals, "counts": {k: dict(v) for k, v in counts.items()}}


# --- Stage "canonical-references": execution (Phase 9) ----------------------------------


# The stage decomposes into 3 independently-checkpointable phases -- not one per
# collection (that would need a precise per-record cursor within a shared proposal
# loop, more complexity than "a simple migration checkpoint mechanism" calls for),
# but coarse enough that a resumed run never repeats a whole phase that already
# finished. Correctness under resume does NOT depend on this granularity: every
# individual write below is already idempotent (each checks "not already canonical"
# immediately before writing), so even re-scanning a phase from scratch on resume can
# never duplicate a write or a canonical relationship -- this checkpoint exists to
# avoid needless re-scanning and to make failure state observable, not to prevent
# duplication by itself.
_RELATIONSHIP_COLLECTIONS = ("commitments", "meetings", "follow_ups", "projects", "reply_drafts", "calendar_actions")


def _apply_relationship_proposal(db: Database, p: dict[str, Any], updated_counts: dict[str, int]) -> None:
    if p["collection"] == "commitments":
        doc = CommitmentRepository(db).find_one({"id": p["record_id"]})
        if doc and not doc.get("person_id"):
            CommitmentRepository(db).upsert_by_key(
                {"id": p["record_id"]}, {**doc, "person_id": p["proposed_person_id"], "org_id": p.get("proposed_org_id")}
            )
            updated_counts["commitments"] += 1
    elif p["collection"] == "meetings":
        doc = MeetingRepository(db).find_one({"id": p["record_id"]})
        if doc and not doc.get("person_ids"):
            MeetingRepository(db).upsert_by_key(
                {"id": p["record_id"]},
                {**doc, "person_ids": p["proposed_person_ids"], "org_id": p.get("proposed_org_id")},
            )
            updated_counts["meetings"] += 1
    elif p["collection"] == "follow_ups":
        doc = FollowUpRepository(db).find_one({"id": p["record_id"]})
        if doc and not doc.get("person_id"):
            FollowUpRepository(db).upsert_by_key(
                {"id": p["record_id"]}, {**doc, "person_id": p["proposed_person_id"], "org_id": p.get("proposed_org_id")}
            )
            updated_counts["follow_ups"] += 1
    elif p["collection"] == "projects":
        doc = ProjectRepository(db).find_one({"id": p["record_id"]})
        if doc and not doc.get("person_ids"):
            ProjectRepository(db).upsert_by_key(
                {"id": p["record_id"]},
                {**doc, "person_ids": p["proposed_person_ids"], "org_id": p.get("proposed_org_id")},
            )
            updated_counts["projects"] += 1
    elif p["collection"] == "reply_drafts":
        doc = ReplyDraftRepository(db).find_one({"source_email_id": p["record_id"]})
        if doc and not doc.get("person_id"):
            ReplyDraftRepository(db).upsert_by_key(
                {"source_email_id": p["record_id"]},
                {**doc, "person_id": p["proposed_person_id"], "org_id": p.get("proposed_org_id")},
            )
            updated_counts["reply_drafts"] += 1
    elif p["collection"] == "calendar_actions":
        thread_id, fingerprint = p["record_id"].split(":", 1)
        doc = CalendarActionRepository(db).find_one({"thread_id": thread_id, "meeting_fingerprint": fingerprint})
        if doc and not doc.get("person_id"):
            # Calendar safety, non-negotiable: never touch attendees. Checked BEFORE
            # writing so a pre-existing anomaly is never silently perpetuated.
            if doc["event"].get("attendees"):
                raise RuntimeError(
                    f"safety anomaly: calendar_action {p['record_id']} already had non-empty "
                    f"event.attendees BEFORE this migration touched it -- refusing to proceed silently"
                )
            CalendarActionRepository(db).upsert_by_key(
                {"thread_id": thread_id, "meeting_fingerprint": fingerprint},
                {
                    **doc, "person_id": p["proposed_person_id"], "org_id": p.get("proposed_org_id"),
                    "meeting_id": p.get("proposed_meeting_id"),
                },
            )
            updated_counts["calendar_actions"] += 1


def _execute_threads_phase(db: Database, limit: int | None, updated_counts: dict[str, int]) -> None:
    threads = ThreadRepository(db).find_many({})
    if limit:
        threads = threads[:limit]
    for t in threads:
        before = t.get("person_ids"), t.get("org_ids")
        _link_thread_to_entities(db, t["thread_id"])
        after = ThreadRepository(db).find_one({"thread_id": t["thread_id"]})
        if (after.get("person_ids"), after.get("org_ids")) != before:
            updated_counts["threads"] += 1


def _execute_relationships_phase(db: Database, dry: dict[str, Any], updated_counts: dict[str, int]) -> None:
    for p in dry["proposals"]:
        if p["status"] != "WOULD_UPDATE" or p["collection"] not in _RELATIONSHIP_COLLECTIONS:
            continue
        _apply_relationship_proposal(db, p, updated_counts)


def _execute_knowledge_phase(db: Database, limit: int | None, updated_counts: dict[str, int]) -> None:
    items = KnowledgeRepository(db).find_many({})
    if limit:
        items = items[:limit]
    for k in items:
        if k.get("person_id") or k.get("org_id"):
            continue
        before = k.get("person_id"), k.get("org_id")
        _link_knowledge_to_entities(db, k["thread_id"], (k.get("source_emails") or [None])[0])
        after = KnowledgeRepository(db).find_one({"knowledge_id": k["knowledge_id"]})
        if after and (after.get("person_id"), after.get("org_id")) != before:
            updated_counts["knowledge_items"] += 1


_CANONICAL_REFERENCE_PHASES = ("threads", "relationships", "knowledge_items")


def stage_canonical_references_execute(
    db: Database, limit: int | None = None, resume_run_id: str | None = None
) -> dict[str, Any]:
    """Only ever fills a missing canonical field -- reuses the exact same dry-run
    proposal logic above to decide WHAT to write, then performs ONLY the write,
    never re-deriving the decision differently between preview and execution. Threads
    and knowledge_items delegate to the application's own existing linking functions
    unchanged, exactly as they already run during live ingestion.

    Checkpointed in 3 phases (see _CANONICAL_REFERENCE_PHASES) using the SAME
    MigrationRunRepository/start_run/_update_run/complete_run machinery Stage 1
    already established -- not a second checkpoint system. A resumed run skips every
    phase already recorded complete; correctness under resume is guaranteed
    independently by each write's own "not already canonical" check, so even
    re-running an already-complete phase from scratch (which resume never does, but
    could) would still never duplicate a write.
    """
    run_id = resume_run_id or start_run(db, "canonical-references")
    run = get_run(db, run_id)
    if run is None:
        raise ValueError(f"no migration run found for run_id={run_id!r}")

    completed_phases: set[str] = set(run.get("completed_phases") or [])
    updated_counts: dict[str, int] = defaultdict(int, run.get("updated_counts_by_collection") or {})

    _DB_FOR_HELPERS[0] = db

    try:
        if "threads" not in completed_phases:
            _execute_threads_phase(db, limit, updated_counts)
            completed_phases.add("threads")
            _update_run(
                db, run_id, completed_phases=sorted(completed_phases),
                updated_counts_by_collection=dict(updated_counts),
            )

        if "relationships" not in completed_phases:
            dry = stage_canonical_references_dry_run(db, limit=limit)
            _execute_relationships_phase(db, dry, updated_counts)
            completed_phases.add("relationships")
            _update_run(
                db, run_id, completed_phases=sorted(completed_phases),
                updated_counts_by_collection=dict(updated_counts),
            )

        if "knowledge_items" not in completed_phases:
            _execute_knowledge_phase(db, limit, updated_counts)
            completed_phases.add("knowledge_items")
            _update_run(
                db, run_id, completed_phases=sorted(completed_phases),
                updated_counts_by_collection=dict(updated_counts),
            )
    except Exception as exc:
        _update_run(
            db, run_id, status="failed", completed_phases=sorted(completed_phases),
            updated_counts_by_collection=dict(updated_counts), error_count=run.get("error_count", 0) + 1,
            last_error=str(exc),
        )
        raise

    complete_run(db, run_id, status="completed")
    return {"run_id": run_id, "updated_counts": dict(updated_counts)}


# --- Stage "canonical-references": verification -----------------------------------------


def verify_stage_canonical_references(db: Database) -> dict[str, Any]:
    """Read-only. Checks that every person_id/person_ids/org_id/meeting_id written by
    this stage actually references a real, existing record -- never that every record
    HAS one (an ambiguous/no-match record correctly has none, by design).
    """
    person_ids = {p["id"] for p in PersonRepository(db).find_many({})}
    org_ids = {o["id"] for o in OrganizationRepository(db).find_many({})}
    failures: list[str] = []

    def check(
        label: str, key: Any, person_field: str, org_field: str = "org_id",
        is_list: bool = False, org_is_list: bool = False,
    ) -> None:
        for doc in docs_by_label[label]:
            person_value = doc.get(person_field)
            if person_value:
                for pid in (person_value if is_list else [person_value]):
                    if pid not in person_ids:
                        failures.append(f"{label} {key(doc)}: {person_field} references unknown person {pid!r}")
            org_value = doc.get(org_field)
            if org_value:
                for oid in (org_value if org_is_list else [org_value]):
                    if oid not in org_ids:
                        failures.append(f"{label} {key(doc)}: {org_field} references unknown organization {oid!r}")

    docs_by_label: dict[str, list[dict[str, Any]]] = {
        "threads": ThreadRepository(db).find_many({}),
        "commitments": CommitmentRepository(db).find_many({}),
        "meetings": MeetingRepository(db).find_many({}),
        "follow_ups": FollowUpRepository(db).find_many({}),
        "projects": ProjectRepository(db).find_many({}),
        "knowledge_items": KnowledgeRepository(db).find_many({}),
        "reply_drafts": ReplyDraftRepository(db).find_many({}),
        "calendar_actions": CalendarActionRepository(db).find_many({}),
    }

    check("threads", lambda d: d["thread_id"], "person_ids", "org_ids", is_list=True, org_is_list=True)
    check("commitments", lambda d: d["id"], "person_id")
    check("meetings", lambda d: d["id"], "person_ids", is_list=True)
    check("follow_ups", lambda d: d["id"], "person_id")
    check("projects", lambda d: d["id"], "person_ids", is_list=True)
    check("knowledge_items", lambda d: d["knowledge_id"], "person_id")
    check("reply_drafts", lambda d: d["source_email_id"], "person_id")
    check("calendar_actions", lambda d: f"{d['thread_id']}:{d['meeting_fingerprint']}", "person_id")

    # Follow-up inheritance: when both a follow-up and its parent commitment have a
    # person_id, they must agree (never independently re-derived).
    commitment_by_id = {c["id"]: c for c in docs_by_label["commitments"]}
    for f in docs_by_label["follow_ups"]:
        commitment = commitment_by_id.get(f.get("commitment_id"))
        if commitment and f.get("person_id") and commitment.get("person_id") and f["person_id"] != commitment["person_id"]:
            failures.append(
                f"follow_ups {f['id']}: person_id {f['person_id']!r} does not match parent "
                f"commitment {commitment['id']}'s person_id {commitment['person_id']!r}"
            )

    # meeting_id must reference a real Meeting.
    meeting_ids = {m["id"] for m in docs_by_label["meetings"]}
    for a in docs_by_label["calendar_actions"]:
        if a.get("meeting_id") and a["meeting_id"] not in meeting_ids:
            failures.append(f"calendar_actions {a['thread_id']}: meeting_id references unknown meeting {a['meeting_id']!r}")

    # Non-negotiable calendar safety: external attendees must never appear.
    external_attendees = [
        a for a in docs_by_label["calendar_actions"] if (a.get("event") or {}).get("attendees")
    ]
    for a in external_attendees:
        failures.append(f"calendar_actions {a['thread_id']}: SAFETY ANOMALY -- non-empty event.attendees found")

    return {
        "passed": len(failures) == 0,
        "failures": failures,
        "external_attendees_found": len(external_attendees),
        "records_checked": {label: len(docs) for label, docs in docs_by_label.items()},
    }


# --- Stage "indexes" (Phase 14, never auto-run) -----------------------------------------


_RECOMMENDED_INDEXES: list[tuple[str, list[tuple[str, int]] | str, dict[str, Any]]] = [
    ("people", "org_id", {}),
    ("organizations", "domain", {"unique": True}),
    ("threads", "person_ids", {}),
    ("threads", "org_ids", {}),
    ("commitments", "person_id", {}),
    ("commitments", "org_id", {}),
    ("meetings", "person_ids", {}),
    ("meetings", "org_id", {}),
    ("follow_ups", "person_id", {}),
    ("follow_ups", "org_id", {}),
    ("projects", "person_ids", {}),
    ("projects", "org_id", {}),
    ("knowledge_items", "person_id", {}),
    ("knowledge_items", "org_id", {}),
    ("reply_drafts", "person_id", {}),
    ("reply_drafts", "org_id", {}),
    ("calendar_actions", "person_id", {}),
    ("calendar_actions", "org_id", {}),
    ("calendar_actions", "meeting_id", {}),
]


def stage_indexes_dry_run(db: Database) -> dict[str, Any]:
    existing: list[str] = []
    missing: list[tuple[str, str]] = []
    for collection, field, _opts in _RECOMMENDED_INDEXES:
        info = db[collection].index_information()
        already_present = any(idx.get("key") == [(field, 1)] for idx in info.values())
        if already_present:
            existing.append(f"{collection}.{field}")
        else:
            missing.append((collection, field))
    return {"existing": existing, "missing": [f"{c}.{f}" for c, f in missing]}


def stage_indexes_execute(db: Database) -> dict[str, Any]:
    dry = stage_indexes_dry_run(db)
    created = []
    for collection, field, opts in _RECOMMENDED_INDEXES:
        if f"{collection}.{field}" in dry["missing"]:
            db[collection].create_index(field, **opts)
            created.append(f"{collection}.{field}")
    verify = stage_indexes_dry_run(db)
    return {"created": created, "verified_existing": verify["existing"], "still_missing": verify["missing"]}


# --- CLI --------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent -- controlled canonical-identity migration")
    parser.add_argument(
        "--stage", required=True,
        choices=["organizations", "canonical-references", "duplicate-review", "duplicate-consolidation", "indexes"],
    )
    parser.add_argument("--dry-run", action="store_true", help="Never write to MongoDB; report only")
    parser.add_argument("--limit", type=int, default=None, help="Process at most N records this run")
    parser.add_argument("--verify", action="store_true", help="Run verification instead of (or after) execution")
    parser.add_argument("--resume", metavar="RUN_ID", default=None, help="Resume a previous organizations run")
    parser.add_argument("--execute", action="store_true", help="duplicate-consolidation only: apply an approved plan")
    parser.add_argument("--plan", metavar="PATH", default=None, help="duplicate-consolidation --execute: approved plan JSON file")
    parser.add_argument(
        "--source-plan", metavar="PATH", default=None,
        help="duplicate-consolidation --execute: the frozen Phase 18 source plan the approvals were reviewed against "
        "(required -- approvals are validated against this file, never against a freshly regenerated plan)",
    )
    parser.add_argument(
        "--source-plan-hash", metavar="HASH", default=None,
        help="duplicate-consolidation --execute: expected hash of --source-plan; refuses to proceed if it doesn't match",
    )
    parser.add_argument(
        "--rollback-snapshot", metavar="PATH", default=None,
        help="duplicate-consolidation --execute: where to write the pre-execution rollback snapshot (recommended)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    if args.stage == "organizations":
        if args.verify:
            result = verify_stage_organizations(db)
            print("## ORGANIZATIONS VERIFICATION")
            print(f"Passed: {result['passed']}")
            for failure in result["failures"]:
                print(f"  FAIL: {failure}")
            return 0 if result["passed"] else 1
        if args.dry_run:
            result = stage_organizations_dry_run(db, limit=args.limit)
            print("## ORGANIZATIONS DRY RUN (no writes performed)")
            for key, value in result.items():
                if key in ("sample_mappings", "malformed_email_cases"):
                    print(f"{key}:")
                    for s in value:
                        print(f"  {s}")
                else:
                    print(f"{key}: {value}")
            return 0
        result = stage_organizations_execute(db, limit=args.limit, resume_run_id=args.resume)
        print("## ORGANIZATIONS EXECUTED")
        for key, value in result.items():
            print(f"{key}: {value}")
        return 0 if result["errors"] == 0 else 1

    if args.stage == "canonical-references":
        if args.verify:
            result = verify_stage_canonical_references(db)
            print("## CANONICAL-REFERENCES VERIFICATION")
            print(f"Passed: {result['passed']}")
            print(f"Records checked: {result['records_checked']}")
            print(f"External attendees found: {result['external_attendees_found']}")
            for failure in result["failures"]:
                print(f"  FAIL: {failure}")
            return 0 if result["passed"] else 1
        if args.dry_run:
            result = stage_canonical_references_dry_run(db, limit=args.limit)
            print("## CANONICAL-REFERENCES DRY RUN (no writes performed)")
            print(f"counts: {result['counts']}")
            return 0
        result = stage_canonical_references_execute(db, limit=args.limit, resume_run_id=args.resume)
        print("## CANONICAL-REFERENCES EXECUTED")
        print(f"run_id: {result['run_id']}")
        print(f"updated_counts: {result['updated_counts']}")
        return 0

    if args.stage == "duplicate-review":
        people = PersonRepository(db).find_many({})
        candidates = find_duplicate_candidates(people)
        print("## DUPLICATE PERSON REVIEW (report only -- nothing merged)")
        for c in candidates:
            print(f"  {c['source_person_id']} -> {c['candidate_canonical_person_id']} [{c['confidence']}]")
        print("NO PERSON MERGES WERE PERFORMED.")
        return 0

    if args.stage == "duplicate-consolidation":
        # Local import: app.duplicate_consolidation imports start_run/get_run/
        # _update_run/complete_run FROM this module, so a top-of-file import here
        # would be circular. Deferring it to call time (this branch runs long after
        # module load finishes) breaks the cycle without moving the checkpoint
        # helpers out of their established home.
        from app.duplicate_consolidation import (
            compute_unique_plan_impact, execute_approved_plan, generate_merge_plan, read_merge_plan,
        )

        if args.execute:
            if not args.plan:
                print("## DUPLICATE-CONSOLIDATION EXECUTE: refused -- --plan PATH (the approved plan) is required")
                return 1
            if not args.source_plan:
                print(
                    "## DUPLICATE-CONSOLIDATION EXECUTE: refused -- --source-plan PATH is required "
                    "(approvals must be validated against the frozen Phase 18 plan, never a freshly regenerated one)"
                )
                return 1
            approved_plan = read_merge_plan(args.plan)
            source_plan = read_merge_plan(args.source_plan)
            try:
                result = execute_approved_plan(
                    db, approved_plan, source_plan, resume_run_id=args.resume,
                    rollback_snapshot_path=args.rollback_snapshot, expected_source_plan_hash=args.source_plan_hash,
                )
            except (ValueError, RuntimeError) as exc:
                print(f"## DUPLICATE-CONSOLIDATION EXECUTE: ABORTED -- {exc}")
                return 1
            print("## DUPLICATE-CONSOLIDATION EXECUTED")
            for key, value in result.items():
                print(f"{key}: {value}")
            return 0

        # Default (and --dry-run): read-only plan generation + unique impact. This is
        # the ONLY behavior for this stage unless --execute --plan is explicitly given.
        plan = generate_merge_plan(db)
        impact = compute_unique_plan_impact(db, plan)
        print("## DUPLICATE-CONSOLIDATION DRY RUN (no writes performed)")
        print(f"candidates_in_plan: {len(plan)}")
        for mapping in plan:
            print(f"  {mapping['duplicate_person_id']} -> {mapping['canonical_person_id']} [{mapping['classification']}] approved={mapping['approved']}")
        print("unique_impact_per_collection:")
        for label, counts in impact["per_collection"].items():
            print(f"  {label}: {counts}")
        if impact["conflicting_mappings"]:
            print("conflicting_mappings (require manual review before approval):")
            for c in impact["conflicting_mappings"]:
                print(f"  {c}")
        print("NO ATLAS DATA WAS MODIFIED. NO PERSON WAS APPROVED OR MERGED.")
        return 0

    if args.stage == "indexes":
        if args.dry_run:
            result = stage_indexes_dry_run(db)
            print("## INDEXES DRY RUN")
            print(f"existing: {result['existing']}")
            print(f"missing: {result['missing']}")
            return 0
        result = stage_indexes_execute(db)
        print("## INDEXES CREATED")
        print(result)
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
