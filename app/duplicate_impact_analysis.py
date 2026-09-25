"""Phase 17: read-only impact analysis for HIGH-confidence duplicate-Person
candidates. This is NOT a merge tool -- nothing here ever writes to MongoDB, and the
highest conclusion any candidate can reach is SAFE_TO_REVIEW, never "safe to merge".

Reuses existing infrastructure only:
  - app.entity_reconciliation.find_duplicate_candidates for the candidate list itself
    (no second identity-matching algorithm).
  - app.entity_migration._is_malformed_email for the exact same malformed-email
    detection Stage 1 already uses.
  - app.knowledge.normalize.normalize_text for name tokenization, same as everywhere
    else in this codebase.

CLI usage:
    python -m app.duplicate_impact_analysis --high-confidence-only
"""

import argparse
import sys
from collections import defaultdict
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
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entity_migration import _is_malformed_email
from app.entity_reconciliation import find_duplicate_candidates
from app.knowledge.normalize import normalize_text

_SHORT_NICKNAME_MAX_LENGTH = 3
_COLLECTIONS = (
    "threads", "commitments", "meetings", "follow_ups", "projects",
    "knowledge_items", "reply_drafts", "calendar_actions",
)


def _name_tokens(name: str) -> set[str]:
    return set(normalize_text(name or "").split())


def _org_name(db: Database, org_id: str | None) -> str | None:
    if not org_id:
        return None
    org = OrganizationRepository(db).find_one({"id": org_id})
    return org["name"] if org else None


# --- Classification (Part 2) -----------------------------------------------------------


def classify_high_confidence_candidate(
    db: Database, candidate: dict[str, Any], all_high_candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Classifies ONE HIGH-confidence candidate into exactly one of SAFE_TO_REVIEW /
    BLOCKED_DATA_CONFLICT / BLOCKED_AMBIGUOUS / NEEDS_MANUAL_REVIEW. Never returns
    anything stronger than SAFE_TO_REVIEW -- this is an input to a future human
    decision, not permission to merge.
    """
    duplicate = PersonRepository(db).find_one({"id": candidate["source_person_id"]})
    canonical = PersonRepository(db).find_one({"id": candidate["candidate_canonical_person_id"]})

    base = {
        "duplicate_person_id": duplicate["id"],
        "duplicate_name": duplicate["name"],
        "duplicate_email": duplicate.get("email"),
        "duplicate_org_id": duplicate.get("org_id"),
        "duplicate_org_name": _org_name(db, duplicate.get("org_id")),
        "canonical_person_id": canonical["id"],
        "canonical_name": canonical["name"],
        "canonical_email": canonical.get("email"),
        "canonical_org_id": canonical.get("org_id"),
        "canonical_org_name": _org_name(db, canonical.get("org_id")),
        "confidence": candidate["confidence"],
        "evidence": candidate["evidence"],
        "shared_thread_ids": candidate.get("shared_thread_ids", []),
    }

    # BLOCKED_AMBIGUOUS: this exact duplicate is ALSO proposed against a different
    # canonical target elsewhere in the candidate list -- more than one plausible
    # target exists, even though find_duplicate_candidates itself never guesses
    # between them for a single call (this check catches the case where the SAME
    # source person appears in two separate candidate rows, e.g. from the two
    # independent detection axes -- orphan->anchor and anchor->anchor).
    same_source = [c for c in all_high_candidates if c["source_person_id"] == candidate["source_person_id"]]
    distinct_targets = {c["candidate_canonical_person_id"] for c in same_source}
    if len(distinct_targets) > 1:
        other_targets = sorted(distinct_targets - {candidate["candidate_canonical_person_id"]})
        return {
            **base, "mergeable": False, "classification": "BLOCKED_AMBIGUOUS",
            "blocking_reason": f"{duplicate['id']} is also proposed as a duplicate of {other_targets} -- more than one plausible canonical target exists",
        }

    # BLOCKED_DATA_CONFLICT: the "duplicate" has its own real email, distinct from the
    # canonical's -- two competing identities, not a clean no-email-orphan case.
    if duplicate.get("email"):
        if _is_malformed_email(duplicate["email"]):
            return {
                **base, "mergeable": False, "classification": "BLOCKED_DATA_CONFLICT",
                "blocking_reason": (
                    f"{duplicate['id']}'s own email is malformed/combined ({duplicate['email']!r}) -- "
                    "cannot determine a single authoritative address; never guess which one is real"
                ),
            }
        if duplicate["email"].lower() != (canonical.get("email") or "").lower():
            return {
                **base, "mergeable": False, "classification": "BLOCKED_DATA_CONFLICT",
                "blocking_reason": (
                    f"{duplicate['id']} has its own distinct real email ({duplicate['email']!r}), "
                    f"different from {canonical['id']}'s ({canonical.get('email')!r}) -- two competing "
                    "identities, not an orphan-to-anchor case; requires a human decision on which email is authoritative"
                ),
            }

    # NEEDS_MANUAL_REVIEW: the duplicate's own name is a short, single-word,
    # nickname-length match (<= 3 characters) against the canonical's fuller name --
    # structurally weaker evidence than an exact name match or a longer given name,
    # since short nicknames are more likely to be shared by genuinely different real
    # people (the real, verified PER-608 "Ash" vs PER-513 "Ash Allen" case: a
    # genuinely different DataBeat person, despite matching thread/org evidence).
    duplicate_tokens = _name_tokens(duplicate["name"])
    canonical_tokens = _name_tokens(canonical["name"])
    is_exact_name_match = duplicate_tokens == canonical_tokens
    is_short_nickname = (
        not is_exact_name_match
        and len(duplicate_tokens) == 1
        and len(next(iter(duplicate_tokens))) <= _SHORT_NICKNAME_MAX_LENGTH
    )
    if is_short_nickname:
        return {
            **base, "mergeable": False, "classification": "NEEDS_MANUAL_REVIEW",
            "blocking_reason": (
                f"{duplicate['id']}'s name ({duplicate['name']!r}) is a short, generic nickname-length "
                f"match against {canonical['name']!r} -- thread/org overlap alone is not strong enough "
                "evidence for a name this short; could plausibly be a different real person"
            ),
        }

    return {**base, "mergeable": True, "classification": "SAFE_TO_REVIEW", "blocking_reason": None}


def classify_all_high_confidence_candidates(db: Database) -> list[dict[str, Any]]:
    people = PersonRepository(db).find_many({})
    all_candidates = find_duplicate_candidates(people)
    high_candidates = [c for c in all_candidates if c["confidence"] == "HIGH"]
    return [classify_high_confidence_candidate(db, c, high_candidates) for c in high_candidates]


# --- Downstream impact analysis (Part 3) ------------------------------------------------


def _person_ref_matches(doc: dict[str, Any], field: str, person_id: str) -> bool:
    value = doc.get(field)
    if isinstance(value, list):
        return person_id in value
    return value == person_id


def compute_downstream_impact(db: Database, duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    """For one candidate pair, computes exactly what WOULD be affected if the
    duplicate were later consolidated into the canonical person -- never modifies
    anything. Every count is derived from data already in Atlas; nothing is
    invented or guessed here.
    """
    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    duplicate_threads = set((duplicate or {}).get("open_threads", []))

    impact: dict[str, dict[str, Any]] = {}

    specs = [
        ("threads", ThreadRepository(db), "thread_id", "person_ids", True),
        ("commitments", CommitmentRepository(db), "id", "person_id", False),
        ("meetings", MeetingRepository(db), "id", "person_ids", True),
        ("follow_ups", FollowUpRepository(db), "id", "person_id", False),
        ("projects", ProjectRepository(db), "id", "person_ids", True),
        ("knowledge_items", KnowledgeRepository(db), "knowledge_id", "person_id", False),
        ("reply_drafts", ReplyDraftRepository(db), "source_email_id", "person_id", False),
        ("calendar_actions", CalendarActionRepository(db), None, "person_id", False),
    ]

    for label, repo, _id_field, person_field, _is_list in specs:
        all_docs = repo.find_many({})
        pointing_to_canonical = [d for d in all_docs if _person_ref_matches(d, person_field, canonical_id)]
        pointing_to_duplicate = [d for d in all_docs if _person_ref_matches(d, person_field, duplicate_id)]

        if label == "threads":
            no_person_but_matching = [
                d for d in all_docs
                if d["thread_id"] in duplicate_threads and not _person_ref_matches(d, person_field, duplicate_id)
                and not _person_ref_matches(d, person_field, canonical_id)
            ]
        else:
            no_person_but_matching = [
                d for d in all_docs
                if not d.get(person_field) and d.get("thread_id") in duplicate_threads
            ]

        # Ambiguous: a list-valued field that already references BOTH the duplicate
        # and the canonical together -- consolidating would collapse two entries into
        # one, worth a human's eyes even though it's not unsafe.
        ambiguous = [
            d for d in all_docs
            if _person_ref_matches(d, person_field, duplicate_id) and _person_ref_matches(d, person_field, canonical_id)
        ]

        impact[label] = {
            "total_affected": len(pointing_to_canonical) + len(pointing_to_duplicate) + len(no_person_but_matching),
            "already_pointing_to_canonical": len(pointing_to_canonical),
            "pointing_to_duplicate": len(pointing_to_duplicate),
            "no_person_id_but_matching_duplicate_threads": len(no_person_but_matching),
            "ambiguous_records": len(ambiguous),
            "would_require_no_change": len(pointing_to_canonical),
            "would_require_deterministic_repointing": len(pointing_to_duplicate),
        }

    return impact


# --- Calendar safety (Part 4) ------------------------------------------------------------


def check_calendar_safety(db: Database, duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    actions = [
        a for a in CalendarActionRepository(db).find_many({})
        if a.get("person_id") in (duplicate_id, canonical_id)
    ]
    external_attendee_conflicts = [a for a in actions if (a.get("event") or {}).get("attendees")]
    return {
        "affected_calendar_actions": len(actions),
        "external_attendee_conflicts": len(external_attendee_conflicts),
        "conflicting_records": [
            {"thread_id": a["thread_id"], "meeting_fingerprint": a["meeting_fingerprint"]}
            for a in external_attendee_conflicts
        ],
        "safe": len(external_attendee_conflicts) == 0,
    }


# --- Knowledge safety (Part 5) ------------------------------------------------------------


def categorize_knowledge_items(db: Database, duplicate_id: str, canonical_id: str) -> dict[str, Any]:
    duplicate = PersonRepository(db).find_one({"id": duplicate_id})
    duplicate_threads = set((duplicate or {}).get("open_threads", []))

    canonical_person: list[dict[str, Any]] = []
    canonical_org: list[dict[str, Any]] = []
    thread_level: list[dict[str, Any]] = []
    legacy_unresolved: list[dict[str, Any]] = []

    for item in KnowledgeRepository(db).find_many({}):
        if item.get("thread_id") not in duplicate_threads and not _person_ref_matches(item, "person_id", duplicate_id) and not _person_ref_matches(item, "person_id", canonical_id):
            continue
        if item.get("person_id") in (duplicate_id, canonical_id):
            canonical_person.append(item)
        elif item.get("org_id"):
            canonical_org.append(item)
        elif normalize_text(item.get("subject_key", "")) == normalize_text((item.get("thread_id") or "")):
            thread_level.append(item)
        else:
            legacy_unresolved.append(item)

    return {
        "canonical_person_knowledge": len(canonical_person),
        "canonical_org_knowledge": len(canonical_org),
        "thread_level_knowledge": len(thread_level),
        "legacy_unresolved_knowledge": len(legacy_unresolved),
        "note": "thread-level knowledge is never force-assigned to a person, by design",
    }


# --- Full report assembly ------------------------------------------------------------------


def build_high_confidence_report(db: Database) -> dict[str, Any]:
    classified = classify_all_high_confidence_candidates(db)

    by_classification: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in classified:
        by_classification[c["classification"]].append(c)

    impact_by_pair: dict[str, dict[str, Any]] = {}
    calendar_safety_by_pair: dict[str, dict[str, Any]] = {}
    knowledge_by_pair: dict[str, dict[str, Any]] = {}
    for c in by_classification["SAFE_TO_REVIEW"]:
        key = f"{c['duplicate_person_id']}->{c['canonical_person_id']}"
        impact_by_pair[key] = compute_downstream_impact(db, c["duplicate_person_id"], c["canonical_person_id"])
        calendar_safety_by_pair[key] = check_calendar_safety(db, c["duplicate_person_id"], c["canonical_person_id"])
        knowledge_by_pair[key] = categorize_knowledge_items(db, c["duplicate_person_id"], c["canonical_person_id"])

    downstream_summary: dict[str, dict[str, int]] = {
        label: {"affected": 0, "deterministic": 0, "ambiguous": 0, "blocked": 0} for label in _COLLECTIONS
    }
    for impact in impact_by_pair.values():
        for label in _COLLECTIONS:
            downstream_summary[label]["affected"] += impact[label]["total_affected"]
            downstream_summary[label]["deterministic"] += impact[label]["would_require_deterministic_repointing"]
            downstream_summary[label]["ambiguous"] += impact[label]["ambiguous_records"]
    for key, cal in calendar_safety_by_pair.items():
        if not cal["safe"]:
            downstream_summary["calendar_actions"]["blocked"] += cal["external_attendee_conflicts"]

    return {
        "total_high_candidates": len(classified),
        "classified": classified,
        "counts": {label: len(items) for label, items in by_classification.items()},
        "downstream_impact_by_pair": impact_by_pair,
        "calendar_safety_by_pair": calendar_safety_by_pair,
        "knowledge_by_pair": knowledge_by_pair,
        "downstream_summary": downstream_summary,
    }


def _special_case_report(report: dict[str, Any], duplicate_ids: list[str]) -> list[dict[str, Any]]:
    return [c for c in report["classified"] if c["duplicate_person_id"] in duplicate_ids]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="CoS Sales Agent -- read-only HIGH-confidence duplicate impact analysis"
    )
    parser.add_argument("--high-confidence-only", action="store_true", help="Analyze HIGH-confidence duplicate candidates only")
    return parser


def main(argv: list[str] | None = None) -> int:
    build_arg_parser().parse_args(argv)  # single mode today; flag reserved for clarity/future use
    settings = get_settings()
    configure_logging(settings.log_level)
    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    report = build_high_confidence_report(db)

    print("## HIGH-CONFIDENCE DUPLICATE IMPACT ANALYSIS (READ-ONLY)")
    print()
    print(f"Total HIGH candidates analyzed: {report['total_high_candidates']}")
    for label in ("SAFE_TO_REVIEW", "BLOCKED_DATA_CONFLICT", "BLOCKED_AMBIGUOUS", "NEEDS_MANUAL_REVIEW"):
        print(f"  {label}: {report['counts'].get(label, 0)}")
    print()
    print("duplicate_id | canonical_id | name | confidence | classification | reason")
    for c in report["classified"]:
        print(
            f"{c['duplicate_person_id']} | {c['canonical_person_id']} | {c['duplicate_name']} | "
            f"{c['confidence']} | {c['classification']} | {c['blocking_reason'] or '(none)'}"
        )
    print()
    print("DOWNSTREAM IMPACT SUMMARY")
    print("collection | affected | deterministic | ambiguous | blocked")
    for label, counts in report["downstream_summary"].items():
        print(f"{label} | {counts['affected']} | {counts['deterministic']} | {counts['ambiguous']} | {counts['blocked']}")
    print()
    print("NO ATLAS DATA WAS MODIFIED. NO MERGES WERE PERFORMED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
