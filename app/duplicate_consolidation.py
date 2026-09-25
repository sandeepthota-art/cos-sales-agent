"""Phase 18: controlled, approval-gated Person consolidation.

This module has TWO strictly separated halves:

  1. PLANNING (generate_merge_plan, compute_unique_plan_impact) -- always read-only,
     reuses app.duplicate_impact_analysis's Phase 17 classification verbatim. No new
     identity-resolution algorithm. SAFE_TO_REVIEW is the only classification that
     ever enters a plan; it is NEVER treated as approval.

  2. EXECUTION (execute_merge_plan) -- the only code path in this module that writes
     to MongoDB, and only ever for mappings an approval artifact explicitly marks
     `"approved": true`. Checkpointed via the SAME MigrationRunRepository/start_run/
     _update_run/complete_run machinery Stage 1/2 already established -- no second
     checkpoint system.

A duplicate Person is never physically deleted here: consolidation sets
`status="merged"` and `merged_into=<canonical_person_id>` (see app.entities.models.
Person), preserving historical identity. The canonical Person's own document
(email/org_id/name) is never touched by this module at all.

CLI usage (added as a new --stage on the existing app.entity_migration CLI, not a
competing one):
    python -m app.entity_migration --stage duplicate-consolidation --dry-run
    python -m app.entity_migration --stage duplicate-consolidation --execute --plan approved_merge_plan.json
    python -m app.entity_migration --stage duplicate-consolidation --execute --plan approved_merge_plan.json --resume <run_id>
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
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
from app.duplicate_impact_analysis import check_calendar_safety, classify_all_high_confidence_candidates, compute_downstream_impact
from app.entity_migration import _update_run, complete_run, get_run, start_run

_COLLECTIONS = (
    "threads", "commitments", "meetings", "follow_ups", "projects",
    "knowledge_items", "reply_drafts", "calendar_actions",
)

# (label, repository, id-extraction, person field, is-list-field)
_COLLECTION_SPECS = [
    ("threads", ThreadRepository, lambda d: d["thread_id"], "person_ids", True),
    ("commitments", CommitmentRepository, lambda d: d["id"], "person_id", False),
    ("meetings", MeetingRepository, lambda d: d["id"], "person_ids", True),
    ("follow_ups", FollowUpRepository, lambda d: d["id"], "person_id", False),
    ("projects", ProjectRepository, lambda d: d["id"], "person_ids", True),
    ("knowledge_items", KnowledgeRepository, lambda d: d["knowledge_id"], "person_id", False),
    ("reply_drafts", ReplyDraftRepository, lambda d: d["source_email_id"], "person_id", False),
    (
        "calendar_actions", CalendarActionRepository,
        lambda d: f"{d['thread_id']}:{d['meeting_fingerprint']}", "person_id", False,
    ),
]


# --- Part A: merge plan generation ------------------------------------------------------


def generate_merge_plan(db: Database) -> list[dict[str, Any]]:
    """Reuses Phase 17's classification verbatim -- filters to SAFE_TO_REVIEW only.
    BLOCKED_DATA_CONFLICT / BLOCKED_AMBIGUOUS / NEEDS_MANUAL_REVIEW never enter the
    plan. Every mapping starts with approved: false -- generating a plan is not
    approving it.
    """
    classified = classify_all_high_confidence_candidates(db)
    plan = []
    for c in classified:
        if c["classification"] != "SAFE_TO_REVIEW":
            continue
        plan.append(
            {
                "duplicate_person_id": c["duplicate_person_id"],
                "duplicate_name": c["duplicate_name"],
                "duplicate_email": c["duplicate_email"],
                "duplicate_org_id": c["duplicate_org_id"],
                "canonical_person_id": c["canonical_person_id"],
                "canonical_name": c["canonical_name"],
                "canonical_email": c["canonical_email"],
                "canonical_org_id": c["canonical_org_id"],
                "confidence": c["confidence"],
                "evidence": c["evidence"],
                "classification": c["classification"],
                "downstream_impact": compute_downstream_impact(db, c["duplicate_person_id"], c["canonical_person_id"]),
                "approved": False,
            }
        )
    return plan


def write_merge_plan(plan: list[dict[str, Any]], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, default=str)


def read_merge_plan(path: str) -> list[dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- Part B: unique impact across the whole plan ----------------------------------------


def _downstream_ids(db: Database, duplicate_id: str, canonical_id: str) -> dict[str, dict[str, set[str]]]:
    result: dict[str, dict[str, set[str]]] = {}
    for label, repo_cls, key_fn, field, is_list in _COLLECTION_SPECS:
        points_to_duplicate: set[str] = set()
        points_to_canonical: set[str] = set()
        contains_both: set[str] = set()
        for d in repo_cls(db).find_many({}):
            value = d.get(field)
            # A real Atlas document can have a list-valued field stored as an
            # explicit None (not merely absent) -- .get() returns that None as-is,
            # so `value or []` is required here, not just the is_list branch alone.
            values = (value or []) if is_list else ([value] if value else [])
            has_dup, has_canon = duplicate_id in values, canonical_id in values
            doc_id = key_fn(d)
            if has_dup and has_canon:
                contains_both.add(doc_id)
            elif has_dup:
                points_to_duplicate.add(doc_id)
            elif has_canon:
                points_to_canonical.add(doc_id)
        result[label] = {
            "points_to_duplicate": points_to_duplicate,
            "points_to_canonical": points_to_canonical,
            "contains_both": contains_both,
        }
    return result


def compute_unique_plan_impact(db: Database, plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Unions per-mapping impact by document id -- a document affected by more than
    one mapping in the plan is counted exactly once, per collection, as the task
    requires. Also detects genuine conflicting mappings: the same document would be
    repointed to two DIFFERENT canonical targets by two different mappings in this
    plan (as opposed to two mappings that happen to share the same canonical target,
    which is the normal, expected case for e.g. several Ashok fragments).
    """
    # Keyed by (duplicate, canonical) pair, NOT by duplicate_person_id alone -- a plan
    # can legitimately contain the same duplicate mapped toward more than one
    # canonical (that's precisely the conflicting-mapping case this function must
    # detect), and keying by duplicate_person_id would silently drop all but one such
    # mapping before the conflict check ever saw it.
    per_mapping = [
        (m["duplicate_person_id"], m["canonical_person_id"], _downstream_ids(db, m["duplicate_person_id"], m["canonical_person_id"]))
        for m in plan
    ]

    per_collection: dict[str, dict[str, int]] = {}
    conflicts: list[dict[str, Any]] = []

    for label, *_ in _COLLECTION_SPECS:
        already_canonical: set[str] = set()
        requires_repoint: set[str] = set()
        ambiguous: set[str] = set()
        doc_to_targets: dict[str, set[str]] = defaultdict(set)

        for _duplicate_id, canonical_id, ids in per_mapping:
            already_canonical |= ids[label]["points_to_canonical"]
            requires_repoint |= ids[label]["points_to_duplicate"]
            ambiguous |= ids[label]["contains_both"]
            for doc_id in ids[label]["points_to_duplicate"]:
                doc_to_targets[doc_id].add(canonical_id)

        conflicting_docs = {doc_id: sorted(targets) for doc_id, targets in doc_to_targets.items() if len(targets) > 1}
        if conflicting_docs:
            conflicts.append({"collection": label, "conflicting_documents": conflicting_docs})

        unique_affected = already_canonical | requires_repoint | ambiguous
        per_collection[label] = {
            "unique_documents_affected": len(unique_affected),
            "already_canonical": len(already_canonical - requires_repoint - ambiguous),
            "requires_repoint": len(requires_repoint),
            "ambiguous": len(ambiguous),
            "blocked": 0,
        }

    return {"per_collection": per_collection, "conflicting_mappings": conflicts}


# --- Parts D/E/F: downstream repointing + safety, execution ------------------------------


def _repoint_list_field(doc: dict[str, Any], field: str, duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    values = doc.get(field) or []
    if duplicate_id not in values:
        return {}
    replaced = [canonical_id if v == duplicate_id else v for v in values]
    # dict.fromkeys preserves first-seen order while deduplicating -- a document that
    # already contained BOTH [duplicate, canonical] collapses to just [canonical].
    return {field: list(dict.fromkeys(replaced))}


def _execute_one_mapping(db: Database, mapping: dict[str, Any]) -> dict[str, Any]:
    duplicate_id = mapping["duplicate_person_id"]
    canonical_id = mapping["canonical_person_id"]

    # Calendar safety gate FIRST, before any write for this mapping: if any calendar
    # action this mapping would touch already has external attendee data, the WHOLE
    # mapping is blocked -- never partially applied.
    safety = check_calendar_safety(db, duplicate_id, canonical_id)
    if not safety["safe"]:
        return {"status": "BLOCKED", "reason": "calendar_external_attendee_conflict", "detail": safety}

    updated_counts: dict[str, int] = defaultdict(int)

    for t in ThreadRepository(db).find_many({"person_ids": duplicate_id}):
        update = _repoint_list_field(t, "person_ids", duplicate_id, canonical_id)
        if update:
            ThreadRepository(db).upsert_by_key({"thread_id": t["thread_id"]}, {**t, **update})
            updated_counts["threads"] += 1

    for c in CommitmentRepository(db).find_many({"person_id": duplicate_id}):
        CommitmentRepository(db).upsert_by_key({"id": c["id"]}, {**c, "person_id": canonical_id})
        updated_counts["commitments"] += 1

    for m in MeetingRepository(db).find_many({"person_ids": duplicate_id}):
        update = _repoint_list_field(m, "person_ids", duplicate_id, canonical_id)
        if update:
            MeetingRepository(db).upsert_by_key({"id": m["id"]}, {**m, **update})
            updated_counts["meetings"] += 1

    for f in FollowUpRepository(db).find_many({"person_id": duplicate_id}):
        FollowUpRepository(db).upsert_by_key({"id": f["id"]}, {**f, "person_id": canonical_id})
        updated_counts["follow_ups"] += 1

    for p in ProjectRepository(db).find_many({"person_ids": duplicate_id}):
        update = _repoint_list_field(p, "person_ids", duplicate_id, canonical_id)
        if update:
            ProjectRepository(db).upsert_by_key({"id": p["id"]}, {**p, **update})
            updated_counts["projects"] += 1

    # Knowledge safety (Part E): ONLY an explicit existing person_id == duplicate_id
    # is ever repointed. Thread-level and org-only knowledge items are never touched,
    # and no new person relationship is ever inferred from subject/text/org here.
    for k in KnowledgeRepository(db).find_many({"person_id": duplicate_id}):
        KnowledgeRepository(db).upsert_by_key({"knowledge_id": k["knowledge_id"]}, {**k, "person_id": canonical_id})
        updated_counts["knowledge_items"] += 1

    for r in ReplyDraftRepository(db).find_many({"person_id": duplicate_id}):
        ReplyDraftRepository(db).upsert_by_key({"source_email_id": r["source_email_id"]}, {**r, "person_id": canonical_id})
        updated_counts["reply_drafts"] += 1

    for a in CalendarActionRepository(db).find_many({"person_id": duplicate_id}):
        # event.attendees is never touched -- only person_id changes; meeting_id and
        # org_id (already validated consistent by the safety gate above) are left as
        # they are, exactly as stored.
        CalendarActionRepository(db).upsert_by_key(
            {"thread_id": a["thread_id"], "meeting_fingerprint": a["meeting_fingerprint"]},
            {**a, "person_id": canonical_id},
        )
        updated_counts["calendar_actions"] += 1

    # Retire the duplicate LAST, only after every downstream repoint succeeded. The
    # canonical Person's own document is never written by this function at all.
    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    if duplicate and duplicate.get("status") != "merged":
        PersonRepository(db).upsert_by_key(
            {"id": duplicate_id}, {**duplicate, "status": "merged", "merged_into": canonical_id}
        )

    return {"status": "COMPLETED", "updated_counts": dict(updated_counts)}


# --- Parts I/J/K: approval-gated, checkpointed, resumable execution -----------------------


def _plan_hash(plan: list[dict[str, Any]]) -> str:
    pairs = sorted(f"{m['duplicate_person_id']}->{m['canonical_person_id']}" for m in plan)
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()[:16]


def execute_merge_plan(db: Database, plan: list[dict[str, Any]], resume_run_id: str | None = None) -> dict[str, Any]:
    """Executes ONLY mappings with approved == True. SAFE_TO_REVIEW is never treated
    as approval by itself -- a plan freshly produced by generate_merge_plan() has
    every entry at approved: false and this function does nothing for it.

    Atomicity note (Part K): MongoDB multi-document transactions require a replica
    set and are not assumed available here (this code has never been run against the
    live Atlas cluster to verify its tier/capability, and none of this phase's work
    touches Atlas). Instead, safety relies entirely on the same idempotency pattern
    already used throughout this project: each individual write only ever repoints a
    document that still points at the duplicate, so a crash between two writes within
    one mapping leaves the mapping's remaining documents exactly as findable and
    repointable on the next attempt -- nothing is left "half applied" in a way a
    resume can't recover, and nothing is ever double-applied.
    """
    approved = [m for m in plan if m.get("approved") is True]
    if not approved:
        raise ValueError(
            "no approved mappings in this plan -- SAFE_TO_REVIEW is not approval; "
            "refusing to execute an unapproved plan"
        )

    plan_hash = _plan_hash(plan)
    run_id = resume_run_id or start_run(db, "duplicate-consolidation")
    run = get_run(db, run_id)
    if run is None:
        raise ValueError(f"no migration run found for run_id={run_id!r}")
    if run.get("plan_hash") and run["plan_hash"] != plan_hash:
        raise ValueError(
            f"run {run_id} was started for a different plan (hash {run['plan_hash']}) -- "
            f"refusing to resume it against a different plan (hash {plan_hash})"
        )

    completed: set[str] = set(run.get("completed_mappings") or [])
    blocked: list[dict[str, Any]] = list(run.get("blocked_mappings") or [])
    updated_counts: dict[str, int] = defaultdict(int, run.get("updated_counts_by_collection") or {})

    _update_run(db, run_id, plan_hash=plan_hash)

    try:
        for mapping in approved:
            key = f"{mapping['duplicate_person_id']}->{mapping['canonical_person_id']}"
            if key in completed:
                continue  # already executed in a prior run -- never reapplied
            _update_run(db, run_id, current_mapping=key)
            result = _execute_one_mapping(db, mapping)
            if result["status"] == "BLOCKED":
                blocked.append({"mapping": key, "reason": result["reason"]})
            else:
                for label, count in result["updated_counts"].items():
                    updated_counts[label] += count
            completed.add(key)
            _update_run(
                db, run_id, completed_mappings=sorted(completed), blocked_mappings=blocked,
                updated_counts_by_collection=dict(updated_counts),
            )
    except Exception as exc:
        _update_run(
            db, run_id, status="failed", last_error=str(exc),
            completed_mappings=sorted(completed), blocked_mappings=blocked,
        )
        raise

    complete_run(db, run_id, status="completed")
    return {
        "run_id": run_id, "plan_hash": plan_hash,
        "completed_mappings": sorted(completed), "blocked_mappings": blocked,
        "updated_counts": dict(updated_counts),
    }


# ============================================================================================
# Phase 19: approved-plan execution with plan-integrity checks, a rollback snapshot, hard
# per-mapping abort conditions, and post-mapping/final verification. This section NEVER
# decides approval -- approved_plan is always an artifact a human already produced, and
# every function below only checks it, executes it, or reverses it. It reuses
# _execute_one_mapping and _plan_hash above rather than re-deriving repointing logic, and
# reuses start_run/get_run/_update_run/complete_run (app.entity_migration) for checkpointing
# rather than inventing a second migration/checkpoint system.
# ============================================================================================


# --- Plan integrity validation (no Atlas access; pure structural/consistency check) --------


def validate_approved_plan(approved_plan: list[dict[str, Any]], source_plan: list[dict[str, Any]]) -> list[str]:
    """Checks an approval artifact against the frozen Phase 18 source plan it must be
    derived from. Returns human-readable errors; an empty list means the approved
    entries are structurally sound and faithful to the source -- NOT that they are
    safe to run against current Atlas state (see verify_preexecution_state for that).
    Never mutates approved_plan and never decides which entries should be approved.
    """
    errors: list[str] = []
    source_by_pair = {(m["duplicate_person_id"], m["canonical_person_id"]): m for m in source_plan}
    approved = [m for m in approved_plan if m.get("approved") is True]

    if not approved:
        return ["no approved mappings in this plan"]

    seen_pairs: set[tuple[str, str]] = set()
    duplicate_targets: dict[str, set[str]] = defaultdict(set)

    for m in approved:
        pair = (m["duplicate_person_id"], m["canonical_person_id"])
        if pair in seen_pairs:
            errors.append(f"duplicate mapping entry for {pair[0]} -> {pair[1]} appears more than once in approved plan")
        seen_pairs.add(pair)
        duplicate_targets[pair[0]].add(pair[1])

        if pair[0] == pair[1]:
            errors.append(f"self-mapping: {pair[0]} -> itself is not a valid mapping")

        source = source_by_pair.get(pair)
        if source is None:
            errors.append(f"mapping {pair[0]} -> {pair[1]} does not exist in the Phase 18 source plan -- new/unexpected mapping")
            continue
        if source["classification"] != "SAFE_TO_REVIEW":
            errors.append(f"mapping {pair[0]} -> {pair[1]} has source classification {source['classification']!r} -- blocked/manual candidates can never execute")
        if source["canonical_person_id"] != m["canonical_person_id"]:
            errors.append(f"mapping for {pair[0]} has a different canonical target than the source plan ({source['canonical_person_id']!r} -> {m['canonical_person_id']!r})")

    for duplicate_id, targets in duplicate_targets.items():
        if len(targets) > 1:
            errors.append(f"{duplicate_id} maps to multiple canonical targets in the approved plan: {sorted(targets)}")

    duplicate_ids = {p[0] for p in seen_pairs}
    canonical_ids = {p[1] for p in seen_pairs}
    chained = duplicate_ids & canonical_ids
    if chained:
        errors.append(
            f"circular/chained mapping detected -- these person IDs are both a duplicate and a "
            f"canonical target within the approved set: {sorted(chained)}"
        )

    return errors


def validate_source_plan_hash(source_plan: list[dict[str, Any]], expected_hash: str) -> list[str]:
    actual = _plan_hash(source_plan)
    if actual != expected_hash:
        return [f"source plan hash mismatch: expected {expected_hash!r}, computed {actual!r} -- refusing to proceed against a changed identity plan"]
    return []


# --- Pre-execution Atlas state verification (read-only) --------------------------------------


def verify_preexecution_state(db: Database, approved_plan: list[dict[str, Any]]) -> list[str]:
    """Confirms live Atlas state still matches what the approved plan assumes, right
    before the first write. An idempotent rerun (duplicate already merged into the
    SAME canonical) is explicitly NOT an error here -- only a state that conflicts
    with what this plan expects is.
    """
    errors: list[str] = []
    for m in [x for x in approved_plan if x.get("approved") is True]:
        duplicate_id, canonical_id = m["duplicate_person_id"], m["canonical_person_id"]
        duplicate = PersonRepository(db).find_one({"id": duplicate_id})
        canonical = PersonRepository(db).find_one({"id": canonical_id})

        if duplicate is None:
            errors.append(f"duplicate person {duplicate_id} no longer exists in Atlas")
        if canonical is None:
            errors.append(f"canonical person {canonical_id} no longer exists in Atlas")
        if duplicate is None or canonical is None:
            continue

        if canonical.get("status") == "merged":
            errors.append(f"canonical person {canonical_id} is itself status='merged' (merged_into={canonical.get('merged_into')!r}) -- cannot be a merge target")
        if duplicate.get("status") == "merged" and duplicate.get("merged_into") != canonical_id:
            errors.append(f"duplicate person {duplicate_id} is already merged into {duplicate.get('merged_into')!r}, not {canonical_id!r} -- state conflict")
        if canonical.get("org_id") and OrganizationRepository(db).find_one({"id": canonical["org_id"]}) is None:
            errors.append(f"canonical person {canonical_id}'s org_id {canonical['org_id']!r} does not reference an existing organization")

    return errors


# --- Rollback snapshot (captured/stored locally; Atlas has no migration-backup collection) --


def capture_rollback_snapshot(db: Database, approved_plan: list[dict[str, Any]], migration_run_id: str) -> dict[str, Any]:
    """Captures the exact pre-execution document for every record any approved
    mapping could touch. Stored outside Atlas (this project has no dedicated
    migration-backup mechanism) -- see write_rollback_snapshot.
    """
    entries: list[dict[str, Any]] = []
    for m in [x for x in approved_plan if x.get("approved") is True]:
        duplicate_id, canonical_id = m["duplicate_person_id"], m["canonical_person_id"]
        mapping_key = f"{duplicate_id}->{canonical_id}"

        duplicate_person = PersonRepository(db).find_one({"id": duplicate_id})
        if duplicate_person:
            entries.append({"collection": "people", "document_id": duplicate_id, "original_document": duplicate_person, "mapping": mapping_key})

        for label, repo_cls, key_fn, field, is_list in _COLLECTION_SPECS:
            for d in repo_cls(db).find_many({}):
                values = d.get(field)
                values = (values or []) if is_list else ([values] if values else [])
                if duplicate_id in values:
                    entries.append({"collection": label, "document_id": key_fn(d), "original_document": d, "mapping": mapping_key})

    snapshot = {
        "migration_run_id": migration_run_id,
        "plan_hash": _plan_hash(approved_plan),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "entries": entries,
    }
    snapshot["checksum"] = hashlib.sha256(json.dumps(entries, sort_keys=True, default=str).encode()).hexdigest()
    return snapshot


def write_rollback_snapshot(snapshot: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, indent=2, default=str)


def read_rollback_snapshot(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


_SCALAR_PERSON_FIELD_COLLECTIONS = {
    "commitments": "person_id", "follow_ups": "person_id", "knowledge_items": "person_id",
    "reply_drafts": "person_id", "calendar_actions": "person_id",
}
_LIST_PERSON_FIELD_COLLECTIONS = {"threads": "person_ids", "meetings": "person_ids", "projects": "person_ids"}

_REPO_BY_COLLECTION: dict[str, tuple[type, str]] = {
    "people": (PersonRepository, "id"),
    "threads": (ThreadRepository, "thread_id"),
    "commitments": (CommitmentRepository, "id"),
    "meetings": (MeetingRepository, "id"),
    "follow_ups": (FollowUpRepository, "id"),
    "projects": (ProjectRepository, "id"),
    "knowledge_items": (KnowledgeRepository, "knowledge_id"),
    "reply_drafts": (ReplyDraftRepository, "source_email_id"),
}


def _expected_post_change(label: str, original: dict[str, Any], duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    if label == "people":
        return {**original, "status": "merged", "merged_into": canonical_id}
    if label in _SCALAR_PERSON_FIELD_COLLECTIONS:
        return {**original, _SCALAR_PERSON_FIELD_COLLECTIONS[label]: canonical_id}
    if label in _LIST_PERSON_FIELD_COLLECTIONS:
        field = _LIST_PERSON_FIELD_COLLECTIONS[label]
        return {**original, **_repoint_list_field(original, field, duplicate_id, canonical_id)}
    raise ValueError(f"unknown rollback collection label {label!r}")


def rollback_mapping_from_snapshot(db: Database, snapshot: dict[str, Any], duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    """Restores ONLY the documents captured for one specific mapping, and only when a
    document's CURRENT state still matches exactly what this migration would have
    produced -- i.e. it was not independently modified by anything else since
    execution. A document that no longer matches is left untouched and flagged for
    manual recovery; this function never blindly overwrites unrelated, newer changes,
    and it never restores an entire collection.
    """
    mapping_key = f"{duplicate_id}->{canonical_id}"
    entries = [e for e in snapshot["entries"] if e["mapping"] == mapping_key]
    restored: list[dict[str, Any]] = []
    needs_manual_recovery: list[dict[str, Any]] = []

    for e in entries:
        label, doc_id, original = e["collection"], e["document_id"], e["original_document"]
        if label == "calendar_actions":
            thread_id, fingerprint = doc_id.split(":", 1)
            repo = CalendarActionRepository(db)
            key = {"thread_id": thread_id, "meeting_fingerprint": fingerprint}
        else:
            repo_cls, key_field = _REPO_BY_COLLECTION[label]
            repo = repo_cls(db)
            key = {key_field: doc_id}

        current = repo.find_one(key)
        if current is None:
            needs_manual_recovery.append({"collection": label, "document_id": doc_id, "reason": "document no longer exists"})
            continue

        expected_after = _expected_post_change(label, original, duplicate_id, canonical_id)
        if current != expected_after:
            needs_manual_recovery.append(
                {"collection": label, "document_id": doc_id, "reason": "document was independently modified since migration -- refusing to blindly overwrite"}
            )
            continue

        repo.upsert_by_key(key, original)
        restored.append({"collection": label, "document_id": doc_id})

    return {"restored": restored, "needs_manual_recovery": needs_manual_recovery}


# --- Post-mapping and final verification (read-only) ------------------------------------------


def verify_mapping_postconditions(db: Database, mapping: dict[str, Any], actual_counts: dict[str, int] | None = None) -> list[str]:
    """Read-only check run immediately after one mapping executes. Confirms no
    explicit reference to the duplicate remains anywhere, the duplicate is correctly
    retired (never deleted), the canonical Person's own identity fields are
    untouched, calendar safety still holds, and (when actual_counts is supplied) the
    real repoint counts match what the plan's own downstream_impact predicted.
    """
    duplicate_id, canonical_id = mapping["duplicate_person_id"], mapping["canonical_person_id"]
    errors: list[str] = []

    for label, repo_cls, key_fn, field, is_list in _COLLECTION_SPECS:
        for d in repo_cls(db).find_many({}):
            values = d.get(field)
            values = (values or []) if is_list else ([values] if values else [])
            if duplicate_id in values:
                errors.append(f"{label} {key_fn(d)} still references duplicate {duplicate_id} in {field!r} after execution")

    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    if duplicate is None:
        errors.append(f"duplicate person {duplicate_id} no longer exists -- must never be deleted")
    else:
        if duplicate.get("status") != "merged":
            errors.append(f"duplicate person {duplicate_id} does not have status='merged' after execution")
        if duplicate.get("merged_into") != canonical_id:
            errors.append(f"duplicate person {duplicate_id} has merged_into={duplicate.get('merged_into')!r}, expected {canonical_id!r}")

    canonical = PersonRepository(db).find_one({"id": canonical_id})
    if canonical is None:
        errors.append(f"canonical person {canonical_id} no longer exists")
    else:
        expected = (mapping.get("canonical_name"), mapping.get("canonical_email"), mapping.get("canonical_org_id"))
        actual = (canonical.get("name"), canonical.get("email"), canonical.get("org_id"))
        if expected != actual:
            errors.append(f"canonical person {canonical_id}'s identity fields changed during execution -- expected {expected}, found {actual}")

    cal = check_calendar_safety(db, duplicate_id, canonical_id)
    if not cal["safe"]:
        errors.append(f"calendar safety violated after execution: {cal['conflicting_records']}")

    if actual_counts is not None:
        impact = mapping.get("downstream_impact") or {}
        for label, predicted in impact.items():
            expected_count = predicted.get("would_require_deterministic_repointing", 0)
            actual_count = actual_counts.get(label, 0)
            if actual_count != expected_count:
                errors.append(f"{label}: dry-run plan predicted {expected_count} deterministic repoints, execution produced {actual_count}")

    return errors


def run_final_verification(db: Database, approved_plan: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate post-merge verification across every approved mapping -- the same
    per-mapping checks as verify_mapping_postconditions, rolled up into one report.
    """
    approved = [m for m in approved_plan if m.get("approved") is True]
    per_mapping: dict[str, list[str]] = {}
    for m in approved:
        key = f"{m['duplicate_person_id']}->{m['canonical_person_id']}"
        per_mapping[key] = verify_mapping_postconditions(db, m)
    all_errors = [e for errs in per_mapping.values() for e in errs]
    return {"passed": len(all_errors) == 0, "per_mapping_errors": {k: v for k, v in per_mapping.items() if v}, "total_errors": len(all_errors)}


# --- Orchestrator: approval-gated execution with abort-on-verification-failure ----------------


def execute_approved_plan(
    db: Database,
    approved_plan: list[dict[str, Any]],
    source_plan: list[dict[str, Any]],
    resume_run_id: str | None = None,
    rollback_snapshot_path: str | None = None,
    expected_source_plan_hash: str | None = None,
) -> dict[str, Any]:
    """Phase 19 orchestrator. Layers hard, whole-run integrity/state checks and a
    rollback snapshot on top of Phase 18's execute_merge_plan primitives -- it does
    not reimplement repointing (still uses _execute_one_mapping) or checkpoint
    storage (still uses MigrationRunRepository via start_run/_update_run/
    complete_run). approved_plan must already carry explicit approved=True entries;
    this function never sets that field itself, and never regenerates a fresh
    identity plan -- source_plan must be the frozen Phase 18 plan the approvals were
    reviewed against.

    Atomicity (Part 9): MongoDB transactions require a replica set, which Atlas
    provides, so they are technically available. This function does not use them,
    for three concrete reasons: (1) mongomock -- the only environment this project's
    rules permit for testing destructive/write logic -- has no reliable multi-
    document transaction support, so a transactional code path here could never be
    verified by this test suite; (2) every write below is already independently
    idempotent (each checks "does this document still reference the duplicate"
    immediately before writing), so a crash mid-mapping is always safely resumable
    without a transaction; (3) adding transactions here would be a second atomicity
    mechanism layered on top of the existing checkpoint/idempotency approach, which
    is explicitly what this phase must not do. A mapping is only ever marked
    completed after ALL its writes succeed AND verify_mapping_postconditions passes.

    Raises ValueError before any write if plan integrity or pre-execution state
    checks fail. Raises RuntimeError (and marks the run "failed", preserving the
    checkpoint) if a mapping's post-execution verification fails -- execution stops
    immediately and does not continue to the next mapping.
    """
    if expected_source_plan_hash is not None:
        hash_errors = validate_source_plan_hash(source_plan, expected_source_plan_hash)
        if hash_errors:
            raise ValueError("source plan hash validation failed: " + "; ".join(hash_errors))

    integrity_errors = validate_approved_plan(approved_plan, source_plan)
    if integrity_errors:
        raise ValueError("plan integrity validation failed: " + "; ".join(integrity_errors))

    state_errors = verify_preexecution_state(db, approved_plan)
    if state_errors:
        raise ValueError("pre-execution state validation failed: " + "; ".join(state_errors))

    approved = [m for m in approved_plan if m.get("approved") is True]
    plan_hash = _plan_hash(approved_plan)

    run_id = resume_run_id or start_run(db, "duplicate-consolidation")
    run = get_run(db, run_id)
    if run is None:
        raise ValueError(f"no migration run found for run_id={run_id!r}")
    if run.get("plan_hash") and run["plan_hash"] != plan_hash:
        raise ValueError(
            f"run {run_id} was started for a different approved plan (hash {run['plan_hash']}) -- "
            f"refusing to resume it against a different plan (hash {plan_hash})"
        )

    if not resume_run_id:
        snapshot = capture_rollback_snapshot(db, approved_plan, run_id)
        if rollback_snapshot_path:
            write_rollback_snapshot(snapshot, rollback_snapshot_path)
        _update_run(
            db, run_id, plan_hash=plan_hash,
            rollback_snapshot_checksum=snapshot["checksum"], rollback_snapshot_path=rollback_snapshot_path,
        )

    completed: set[str] = set(run.get("completed_mappings") or [])
    blocked: list[dict[str, Any]] = list(run.get("blocked_mappings") or [])
    updated_counts: dict[str, int] = defaultdict(int, run.get("updated_counts_by_collection") or {})
    execution_log: list[dict[str, Any]] = list(run.get("execution_log") or [])

    for i, mapping in enumerate(approved, start=1):
        key = f"{mapping['duplicate_person_id']}->{mapping['canonical_person_id']}"
        if key in completed:
            continue  # already executed in a prior run -- never reapplied

        _update_run(db, run_id, current_mapping=key)
        # Captured BEFORE executing: distinguishes a genuine first execution (where
        # actual repoint counts must match the plan's dry-run prediction) from an
        # idempotent no-op re-execution of an already-completed mapping (where 0
        # actual changes is correct and expected, even though the plan's prediction
        # -- frozen at generate_merge_plan time, before ANY execution -- says
        # otherwise). Part 11 requires the latter to be a silent no-op, not an abort.
        already_merged_before_this_call = (PersonRepository(db).find_one({"id": mapping["duplicate_person_id"]}) or {}).get("status") == "merged"
        result = _execute_one_mapping(db, mapping)

        if result["status"] == "BLOCKED":
            blocked.append({"mapping": key, "reason": result["reason"]})
            completed.add(key)
            execution_log.append(
                {
                    "mapping_number": i, "duplicate_person_id": mapping["duplicate_person_id"],
                    "canonical_person_id": mapping["canonical_person_id"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "status": "BLOCKED", "reason": result["reason"],
                }
            )
            _update_run(
                db, run_id, completed_mappings=sorted(completed), blocked_mappings=blocked,
                updated_counts_by_collection=dict(updated_counts), execution_log=execution_log,
            )
            continue

        count_check = None if already_merged_before_this_call else result["updated_counts"]
        verification_errors = verify_mapping_postconditions(db, mapping, actual_counts=count_check)
        log_entry = {
            "mapping_number": i, "duplicate_person_id": mapping["duplicate_person_id"],
            "canonical_person_id": mapping["canonical_person_id"],
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "collections_changed": sorted(result["updated_counts"]), "documents_changed": result["updated_counts"],
            "verification_result": "PASSED" if not verification_errors else "FAILED",
        }

        if verification_errors:
            log_entry["verification_errors"] = verification_errors
            execution_log.append(log_entry)
            _update_run(
                db, run_id, status="failed",
                last_error=f"ABORT: post-mapping verification failed for {key}: {verification_errors}",
                completed_mappings=sorted(completed), blocked_mappings=blocked,
                updated_counts_by_collection=dict(updated_counts), execution_log=execution_log,
            )
            raise RuntimeError(f"ABORT: post-mapping verification failed for {key}: {verification_errors}")

        for label, count in result["updated_counts"].items():
            updated_counts[label] += count
        completed.add(key)
        log_entry["checkpoint_result"] = "RECORDED"
        execution_log.append(log_entry)
        _update_run(
            db, run_id, completed_mappings=sorted(completed), blocked_mappings=blocked,
            updated_counts_by_collection=dict(updated_counts), execution_log=execution_log,
        )

    complete_run(db, run_id, status="completed")
    return {
        "run_id": run_id, "plan_hash": plan_hash,
        "mappings_attempted": len(approved), "completed_mappings": sorted(completed),
        "blocked_mappings": blocked, "updated_counts": dict(updated_counts),
        "execution_log": execution_log,
    }
