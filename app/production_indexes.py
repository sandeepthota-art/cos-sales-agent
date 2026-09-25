"""Phase 21: a single, authoritative production MongoDB index manifest, plus
read-only plan-building/validation and (separately, approval-gated) execution.

DESIGN, not automatic action: importing or reading this module never touches
MongoDB. `build_index_plan` performs only read-only queries (find/find_many/
index_information/explain). `execute_index_plan` is the ONLY function here that
writes (create_index) -- and only ever for indexes an approved plan explicitly
marks `"approved": true`, following the exact same approval-gate pattern
app.duplicate_consolidation already established for data changes.

Nothing in this module ever touches a document. It creates/inspects/rolls back
index DEFINITIONS only.
"""

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

# --- The manifest -----------------------------------------------------------------------
#
# Every entry here is justified by a real, currently-active application query path found
# by inspecting app/entities/*, app/pipeline.py, app/mcp/tools.py, app/entity_migration.py,
# app/entity_reconciliation.py, app/duplicate_impact_analysis.py, app/duplicate_consolidation.py,
# app/production_verification.py, and app/ui/data.py. A field that exists but is never
# populated by the live pipeline (see REJECTED_CANDIDATES below) is deliberately excluded,
# even though query code filtering on it exists.
#
# is_list=True fields are multikey by construction (Mongo indexes each array element).
# No compound index combines two array fields anywhere in this manifest -- MongoDB permits
# at most one array field per compound index, and no real query here ever filters on two of
# these fields jointly anyway (see rationale on each entry).

INDEX_MANIFEST: list[dict[str, Any]] = [
    {
        "collection": "organizations", "name": "idx_organizations_id_unique", "key": [("id", 1)],
        "unique": True, "sparse": False, "is_list": False, "required": True,
        "reason": "Primary lookup key for every Organization read (get_organization_context, "
        "duplicate_consolidation's canonical-target checks, context.py's Person-360 org lookup).",
        "query_paths": ["app.entities.context.get_person_context", "app.entities.context.get_organization_context",
                         "app.duplicate_consolidation.verify_preexecution_state"],
        "cardinality_note": "One document per organization; id is minted via app.entities.ids.next_id and never reused.",
    },
    {
        "collection": "organizations", "name": "idx_organizations_domain_unique", "key": [("domain", 1)],
        "unique": True, "sparse": False, "is_list": False, "required": True,
        "reason": "app.entities.resolution.resolve_organization looks up by domain on every single "
        "envelope/mention resolution that has an email -- the highest call-frequency query in this "
        "manifest, currently an unindexed full scan.",
        "query_paths": ["app.entities.resolution.resolve_organization"],
        "cardinality_note": "One organization per real email domain; domain is always set when an "
        "Organization is created (resolve_organization never creates one without a domain).",
    },
    {
        "collection": "people", "name": "idx_people_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Every Organization-360 call and every merged-person org-consistency check filters "
        "people by org_id. Not sparse: Person.org_id is always stored as an explicit key (null when "
        "absent, never omitted), so a sparse index would exclude nothing extra.",
        "query_paths": ["app.entities.context.get_organization_context", "app.production_verification.verify_organization_invariants"],
        "cardinality_note": "org_id is null for people with no resolved organization; non-null values "
        "cluster into ~1 org's worth of people per org_id.",
    },
    {
        "collection": "threads", "name": "idx_threads_person_ids", "key": [("person_ids", 1)],
        "unique": False, "sparse": False, "is_list": True, "required": True,
        "reason": "Cross-collection referential-integrity audits and any future person->threads lookup "
        "filter on array membership; app.pipeline._link_thread_to_entities recomputes this field on "
        "every entity-processing pass.",
        "query_paths": ["app.production_verification.verify_cross_collection_referential_integrity"],
        "cardinality_note": "Multikey; a document with person_ids stored as None (a real historical case "
        "found in Phase 18) indexes as a single null entry, never breaking equality queries for a real id.",
    },
    {
        "collection": "threads", "name": "idx_threads_org_ids", "key": [("org_ids", 1)],
        "unique": False, "sparse": False, "is_list": True, "required": True,
        "reason": "Symmetric with person_ids above; no query anywhere filters person_ids AND org_ids "
        "together, and MongoDB forbids more than one array field in a single compound index anyway, so "
        "this stays a separate simple index rather than a compound with person_ids.",
        "query_paths": ["app.production_verification.verify_cross_collection_referential_integrity"],
        "cardinality_note": "Multikey; same null-tolerance as person_ids.",
    },
    {
        "collection": "commitments", "name": "idx_commitments_person_id", "key": [("person_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.entities.context.get_person_context and app.duplicate_consolidation's per-mapping "
        "repointing both filter commitments by person_id.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "commitments", "name": "idx_commitments_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.entities.context.get_organization_context filters commitments by org_id.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "meetings", "name": "idx_meetings_person_ids", "key": [("person_ids", 1)],
        "unique": False, "sparse": False, "is_list": True, "required": True,
        "reason": "Person-360 and duplicate-consolidation repointing both filter meetings by person_ids array membership.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Multikey; None-list-safe (Phase 18 regression already handled at the query layer).",
    },
    {
        "collection": "meetings", "name": "idx_meetings_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360 filters meetings by org_id.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "follow_ups", "name": "idx_follow_ups_person_id", "key": [("person_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Person-360 and duplicate-consolidation repointing.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "follow_ups", "name": "idx_follow_ups_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "projects", "name": "idx_projects_person_ids", "key": [("person_ids", 1)],
        "unique": False, "sparse": False, "is_list": True, "required": True,
        "reason": "Person-360 and duplicate-consolidation repointing.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Multikey; None-list-safe.",
    },
    {
        "collection": "projects", "name": "idx_projects_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "knowledge_items", "name": "idx_knowledge_items_person_id", "key": [("person_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Person-360, knowledge-safety verification, and duplicate-consolidation repointing all "
        "filter knowledge_items by person_id -- the largest collection in the system (2500+ documents "
        "in the real dataset), currently a full scan for every one of these calls.",
        "query_paths": ["app.entities.context.get_person_context", "app.production_verification.verify_knowledge_safety",
                         "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Scalar, stored null for thread-level/unresolved facts.",
    },
    {
        "collection": "knowledge_items", "name": "idx_knowledge_items_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "reply_drafts", "name": "idx_reply_drafts_person_id", "key": [("person_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Person-360 and duplicate-consolidation repointing.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "reply_drafts", "name": "idx_reply_drafts_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "reply_drafts", "name": "idx_reply_drafts_reply_id_unique", "key": [("reply_id", 1)],
        "unique": True, "sparse": False, "is_list": False, "required": True,
        "reason": "app.mcp.tools.get_reply_draft is a point lookup by reply_id with NO existing index "
        "support today (only source_email_id is indexed). Safe to make unique: reply_id is always "
        "deterministically derived as f'reply_{message_id}' (app.pipeline.run_pipeline / "
        "app.mcp.tools.create_reply_draft), and message_id already carries its own unique index on "
        "emails, so two reply_drafts can never legitimately share a reply_id.",
        "query_paths": ["app.mcp.tools.get_reply_draft"],
        "cardinality_note": "1:1 with source_email_id by construction.",
    },
    {
        "collection": "reply_drafts", "name": "idx_reply_drafts_thread_id", "key": [("thread_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.mcp.tools.list_reply_drafts, app.entities.context.get_person_context's "
        "reply_drafts legacy-fallback query, and app.entity_migration's verification all filter by thread_id.",
        "query_paths": ["app.mcp.tools.list_reply_drafts", "app.entities.context.get_person_context"],
        "cardinality_note": "Scalar, always present (every reply draft belongs to exactly one thread).",
    },
    {
        "collection": "reply_drafts", "name": "idx_reply_drafts_status", "key": [("status", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.ui.data.dashboard_metrics runs count_documents({'status': 'awaiting_approval'}) on "
        "EVERY dashboard page load, plus app.mcp.tools.list_reply_drafts's own status filter -- the "
        "highest access-frequency candidate in this manifest outside identity resolution itself.",
        "query_paths": ["app.ui.data.dashboard_metrics", "app.mcp.tools.list_reply_drafts"],
        "cardinality_note": "8 possible values (no_reply_required/awaiting_approval/approved/edited/"
        "rejected/cancelled/simulated_sent/sent); moderate selectivity, high call frequency.",
    },
    {
        "collection": "calendar_actions", "name": "idx_calendar_actions_person_id", "key": [("person_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Person-360 and duplicate-consolidation repointing.",
        "query_paths": ["app.entities.context.get_person_context", "app.duplicate_consolidation._execute_one_mapping"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "calendar_actions", "name": "idx_calendar_actions_org_id", "key": [("org_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "Organization-360.",
        "query_paths": ["app.entities.context.get_organization_context"],
        "cardinality_note": "Scalar, stored null when unresolved.",
    },
    {
        "collection": "calendar_actions", "name": "idx_calendar_actions_meeting_id", "key": [("meeting_id", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.production_verification's referential-integrity audit and app.pipeline's own "
        "meeting-linking both reference this field; no direct point-lookup query exists today, but "
        "it's a real canonical reference validated on every referential-integrity pass.",
        "query_paths": ["app.production_verification.verify_cross_collection_referential_integrity"],
        "cardinality_note": "Scalar, stored null when no single resolvable meeting exists for the thread.",
    },
    {
        "collection": "calendar_actions", "name": "idx_calendar_actions_status", "key": [("status", 1)],
        "unique": False, "sparse": False, "is_list": False, "required": True,
        "reason": "app.ui.data.dashboard_metrics runs count_documents({'status': {'$in': [...]}}) on "
        "every dashboard load.",
        "query_paths": ["app.ui.data.dashboard_metrics"],
        "cardinality_note": "7 possible values; high call frequency.",
    },
    {
        "collection": "migration_runs", "name": "idx_migration_runs_run_id_unique", "key": [("run_id", 1)],
        "unique": True, "sparse": False, "is_list": False, "required": True,
        "reason": "app.entity_migration.get_run (the ONLY read pattern against this collection) is a "
        "point lookup by run_id. Currently unindexed. Collection stays small indefinitely, but this is "
        "the sole, always-used lookup key, and uniqueness reinforces run_id's own construction guarantee "
        "(uuid4-based) rather than relying on it alone.",
        "query_paths": ["app.entity_migration.get_run"],
        "cardinality_note": "One document per migration run; grows very slowly over the system's lifetime.",
    },
]

# Candidates evaluated and explicitly rejected -- kept here (not silently dropped) so the
# reasoning is visible and reviewable, per Phase 21's "document why it is rejected" requirement.
REJECTED_CANDIDATES: list[dict[str, str]] = [
    {
        "collection": "commitments", "field": "project_id",
        "reason": "Real query code exists (app.mcp.tools.list_commitments/get_project_summary/"
        "get_company_summary), but app.pipeline.py always passes project_id=None to resolve_commitment "
        "-- the live pipeline never populates this field today (confirmed by get_company_summary's own "
        "docstring). Indexing an always-null field provides no value; revisit if a future feature starts "
        "populating it.",
    },
    {
        "collection": "projects", "field": "entity",
        "reason": "Real query path (app.mcp.tools.get_company_summary), but this is the legacy free-text "
        "company-name join the canonical org_id architecture (Phases 1-20) is meant to supersede. Low "
        "cardinality (~53 distinct organizations) makes a full scan negligible today; the properly-"
        "justified projects.org_id index above already covers the architecturally-preferred version of "
        "this same lookup.",
    },
    {
        "collection": "people", "field": "org",
        "reason": "Same rationale as projects.entity above -- legacy free-text join used only by "
        "get_company_summary; people.org_id is the architecturally-preferred, already-proposed index.",
    },
    {
        "collection": "meetings", "field": "actionable",
        "reason": "Real filter (app.mcp.tools.list_meetings), but a boolean field has poor standalone "
        "selectivity, and this query is typically combined with thread_id, which already has an index. "
        "No evidence of a real access pattern that needs this alone.",
    },
    {
        "collection": "commitments", "field": "class",
        "reason": "Real filter (app.mcp.tools.list_commitments), but no evidence of hot-path/high-"
        "frequency usage comparable to the dashboard's status-count queries. Deferred pending real "
        "access-frequency evidence, not created speculatively.",
    },
    {
        "collection": "threads", "field": "person_ids+org_ids (compound)",
        "reason": "No real query filters both fields jointly, and MongoDB does not permit more than one "
        "array field in a single compound index -- this candidate is both unjustified and illegal.",
    },
]


# --- Deterministic plan hashing -----------------------------------------------------------


def _canonical_spec(entry: dict[str, Any]) -> tuple:
    return (
        entry["collection"], entry["name"], tuple(entry["key"]),
        bool(entry.get("unique", False)), bool(entry.get("sparse", False)),
    )


def compute_manifest_hash(manifest: list[dict[str, Any]]) -> str:
    """Deterministic over the actual index specification only -- collection, name, key,
    unique, sparse -- explicitly excluding reason/query_paths/cardinality_note text and
    any timestamp, so rewording a comment never changes the hash.
    """
    specs = sorted(_canonical_spec(e) for e in manifest)
    return hashlib.sha256(json.dumps(specs, sort_keys=True, default=str).encode()).hexdigest()[:16]


# --- Read-only: existing-index inventory + diff --------------------------------------------


def list_existing_indexes(db: Database, collections: list[str]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for collection in collections:
        info = db[collection].index_information()
        result[collection] = [
            {
                "name": name, "key": list(spec["key"]),
                "unique": bool(spec.get("unique", False)), "sparse": bool(spec.get("sparse", False)),
            }
            for name, spec in info.items()
        ]
    return result


def diff_manifest_against_existing(db: Database, manifest: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Read-only. Classifies every manifest entry as:
      - already_satisfied: an index with this exact key+unique+sparse already exists (any name)
      - missing: no equivalent index exists yet -- safe to create
      - conflicting_name: the proposed NAME already exists, but with a DIFFERENT key/unique/
        sparse spec -- creating it would either fail (Mongo refuses a name reused with a
        different spec) or silently mean something different; execution must abort on this.
    """
    already_satisfied, missing, conflicting = [], [], []
    for entry in manifest:
        info = db[entry["collection"]].index_information()
        entry_key = list(entry["key"])
        equivalent_exists = any(
            list(spec["key"]) == entry_key
            and bool(spec.get("unique", False)) == entry["unique"]
            and bool(spec.get("sparse", False)) == entry["sparse"]
            for spec in info.values()
        )
        if equivalent_exists:
            already_satisfied.append(entry)
            continue
        existing_with_same_name = info.get(entry["name"])
        if existing_with_same_name is not None:
            conflicting.append(
                {
                    "manifest_entry": entry,
                    "existing_spec": {
                        "key": list(existing_with_same_name["key"]),
                        "unique": bool(existing_with_same_name.get("unique", False)),
                        "sparse": bool(existing_with_same_name.get("sparse", False)),
                    },
                }
            )
            continue
        missing.append(entry)
    return {"already_satisfied": already_satisfied, "missing": missing, "conflicting_name": conflicting}


# --- Read-only: unique-index data-quality safety --------------------------------------------


def validate_unique_candidate(db: Database, collection: str, field: str) -> dict[str, Any]:
    """Read-only scan for real data that would violate a proposed unique index: two
    documents sharing the same non-null value for `field`. A missing/None value is
    never counted as a duplicate (matches sparse/null semantics MongoDB itself applies).
    """
    by_value: dict[Any, list[Any]] = defaultdict(list)
    null_or_missing = 0
    total = 0
    for doc in db[collection].find({}, {field: 1, "id": 1}):
        total += 1
        value = doc.get(field)
        if value is None:
            null_or_missing += 1
            continue
        by_value[value].append(doc.get("id") or str(doc.get("_id")))

    duplicates = {value: ids for value, ids in by_value.items() if len(ids) > 1}
    return {
        "collection": collection, "field": field, "total_documents": total,
        "null_or_missing_count": null_or_missing, "duplicate_values": duplicates,
        "safe_for_unique_index": len(duplicates) == 0,
    }


# --- Read-only: explain-plan baseline ---------------------------------------------------


def capture_explain_baseline(db: Database, collection: str, filter_: dict[str, Any], label: str) -> dict[str, Any]:
    """Read-only: runs .explain('executionStats') for one representative query shape.
    Never writes; explain() only plans and (for find) executes the read itself, which is
    the same read the application would already be doing.
    """
    explanation = db[collection].find(filter_).explain()
    stats = explanation.get("executionStats", {})
    winning_plan = explanation.get("queryPlanner", {}).get("winningPlan", {})
    return {
        "label": label, "collection": collection, "filter": filter_,
        "winning_plan_stage": winning_plan.get("stage") or (winning_plan.get("inputStage") or {}).get("stage"),
        "total_keys_examined": stats.get("totalKeysExamined"),
        "total_docs_examined": stats.get("totalDocsExamined"),
        "execution_time_millis": stats.get("executionTimeMillis"),
        "n_returned": stats.get("nReturned"),
    }


# --- Plan I/O ------------------------------------------------------------------------------


def write_index_plan(plan: dict[str, Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2, default=str)


def read_index_plan(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_index_plan(db: Database) -> dict[str, Any]:
    """Assembles the full Phase 21A plan: manifest + diff against current indexes +
    unique-safety validation for every unique candidate + rejected candidates. Purely
    read-only. Every manifest entry defaults to approved: false -- being in the plan is
    not approval, exactly like Phase 18's duplicate merge plan.
    """
    collections = sorted({e["collection"] for e in INDEX_MANIFEST})
    diff = diff_manifest_against_existing(db, INDEX_MANIFEST)

    unique_safety = [
        validate_unique_candidate(db, e["collection"], e["key"][0][0])
        for e in INDEX_MANIFEST
        if e["unique"]
    ]
    blocked = [u for u in unique_safety if not u["safe_for_unique_index"]]

    plan_entries = []
    for e in INDEX_MANIFEST:
        entry = dict(e)
        is_blocked = any(u["collection"] == e["collection"] and u["field"] == e["key"][0][0] and not u["safe_for_unique_index"] for u in unique_safety)
        entry["approved"] = False
        entry["blocked"] = is_blocked
        plan_entries.append(entry)

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),  # metadata only -- excluded from the hash
        "manifest_hash": compute_manifest_hash(INDEX_MANIFEST),
        "entries": plan_entries,
        "rejected_candidates": REJECTED_CANDIDATES,
        "existing_indexes": list_existing_indexes(db, collections),
        "diff": {
            "already_satisfied": [e["name"] for e in diff["already_satisfied"]],
            "missing": [e["name"] for e in diff["missing"]],
            "conflicting_name": diff["conflicting_name"],
        },
        "unique_index_safety": unique_safety,
        "blocked_candidates": blocked,
    }


# --- Execution (Phase 21B -- approval-gated, never invoked in Phase 21A) -------------------


def execute_index_plan(db: Database, plan: dict[str, Any]) -> dict[str, Any]:
    """The ONLY function in this module that writes to MongoDB, and only ever for plan
    entries explicitly marked approved=true AND not blocked. Idempotent: re-running
    against a database where some approved indexes already exist creates only what's
    still missing (create_index itself is also idempotent for an identical spec, but
    this still checks first to give an accurate, honest execution record rather than
    relying on that alone). Aborts the WHOLE run before creating anything if any
    approved entry's name conflicts with an existing index of a different spec.
    """
    if compute_manifest_hash(INDEX_MANIFEST) != plan.get("manifest_hash"):
        raise ValueError("plan hash mismatch -- refusing to execute a plan that doesn't match the current manifest")

    approved = [e for e in plan["entries"] if e.get("approved") is True]
    if not approved:
        raise ValueError("no approved index entries in this plan")

    for entry in approved:
        if entry.get("blocked"):
            raise ValueError(f"refusing to execute: {entry['name']} is marked blocked (unique-index data-quality violation)")

    diff = diff_manifest_against_existing(db, approved)
    if diff["conflicting_name"]:
        names = [c["manifest_entry"]["name"] for c in diff["conflicting_name"]]
        raise ValueError(f"refusing to execute: index name(s) already exist with a different spec: {names}")

    created: list[str] = []
    already_satisfied = {e["name"] for e in diff["already_satisfied"]}
    try:
        for entry in approved:
            if entry["name"] in already_satisfied:
                continue
            kwargs: dict[str, Any] = {"name": entry["name"]}
            if entry["unique"]:
                kwargs["unique"] = True
            if entry["sparse"]:
                kwargs["sparse"] = True
            db[entry["collection"]].create_index(entry["key"], **kwargs)
            created.append(entry["name"])
    except Exception as exc:
        # Surfaced, not swallowed: whatever succeeded before the failure is still
        # attached to the error so a caller can build an accurate rollback/execution
        # record instead of losing track of partial progress.
        raise RuntimeError(f"index creation failed after creating {created}: {exc}") from exc

    return {
        "created": created,
        "already_satisfied": sorted(already_satisfied & {e["name"] for e in approved}),
        "attempted": len(approved),
    }


# --- Rollback (index definitions only -- never touches documents) --------------------------


def capture_pre_execution_index_snapshot(db: Database, plan: dict[str, Any]) -> dict[str, Any]:
    """Read-only: records every index that exists BEFORE execution, for every collection
    the plan touches -- so rollback can tell a pre-existing index (never touched) from
    one this execution created (safe to drop if needed).
    """
    collections = sorted({e["collection"] for e in plan["entries"]})
    return {"captured_at": datetime.now(timezone.utc).isoformat(), "indexes_by_collection": list_existing_indexes(db, collections)}


def rollback_created_indexes(db: Database, snapshot: dict[str, Any], created_names: list[str]) -> dict[str, Any]:
    """Drops ONLY indexes named in `created_names` that did NOT exist in `snapshot` --
    i.e. only what this phase's own execution actually created. Never drops a
    pre-existing index, even if its name happens to appear in `created_names` by
    coincidence (defense in depth, though execute_index_plan's own conflict check
    already prevents that from happening in the first place).
    """
    pre_existing_names = {
        idx["name"] for indexes in snapshot["indexes_by_collection"].values() for idx in indexes
    }
    dropped, skipped = [], []
    for entry in INDEX_MANIFEST:
        if entry["name"] not in created_names:
            continue
        if entry["name"] in pre_existing_names:
            skipped.append({"name": entry["name"], "reason": "pre-existing before this phase -- never rolled back"})
            continue
        db[entry["collection"]].drop_index(entry["name"])
        dropped.append(entry["name"])
    return {"dropped": dropped, "skipped": skipped}
