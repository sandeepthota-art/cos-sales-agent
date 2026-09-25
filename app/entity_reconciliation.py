"""Read-only Atlas diagnostic/reconciliation report for the canonical Person/
Organization identity work. Every function here only ever calls find()/find_one()
(directly, or through the existing repository classes) -- nothing in this module ever
writes to MongoDB, creates an index, or modifies application state. Not imported by,
and does not import, run_pipeline/app.scheduler/app.reminders -- a standalone,
independent diagnostic, exactly like app.knowledge_projector.

CLI usage:
    python -m app.entity_reconciliation --reconcile-entities
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
from app.entities.lifecycle import is_person_active
from app.knowledge.normalize import normalize_text


def _name_tokens(name: str) -> set[str]:
    return set(normalize_text(name or "").split())


def _domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.strip().lower().split("@", 1)[1] or None


# --- Section A: People ---------------------------------------------------------------


def audit_people(db: Database) -> dict[str, Any]:
    people = PersonRepository(db).find_many({})

    with_org_id = [p for p in people if p.get("org_id")]
    without_org_id = [p for p in people if not p.get("org_id")]
    email_no_org_id = [p for p in people if p.get("email") and not p.get("org_id")]

    email_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in people:
        if p.get("email"):
            email_groups[p["email"].strip().lower()].append(p)
    duplicate_emails = {k: v for k, v in email_groups.items() if len(v) > 1}

    name_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in people:
        name_groups[normalize_text(p.get("name") or "")].append(p)
    duplicate_names = {k: v for k, v in name_groups.items() if len(v) > 1}

    same_name_same_company = {}
    for norm_name, docs in duplicate_names.items():
        orgs = {normalize_text(d.get("org") or "") for d in docs}
        non_empty = {o for o in orgs if o}
        if len(non_empty) <= 1 and non_empty:
            same_name_same_company[norm_name] = docs

    return {
        "total": len(people),
        "with_org_id": with_org_id,
        "without_org_id": without_org_id,
        "email_but_no_org_id": email_no_org_id,
        "duplicate_emails": duplicate_emails,
        "duplicate_names": duplicate_names,
        "same_name_same_company": same_name_same_company,
        "all_people": people,
    }


# --- Section B: Organizations ---------------------------------------------------------


def audit_organizations(db: Database, people: list[dict[str, Any]]) -> dict[str, Any]:
    orgs = OrganizationRepository(db).find_many({})
    org_by_id = {o["id"]: o for o in orgs}

    members: dict[str, list[str]] = defaultdict(list)
    for p in people:
        if p.get("org_id"):
            members[p["org_id"]].append(p["id"])

    # Domains that a person's real email carries, but which have no Organization
    # document today (i.e. would be created by resolve_organization on next contact,
    # but haven't been yet -- e.g. the person predates this feature or has no org_id
    # backfilled).
    existing_domains = {o.get("domain") for o in orgs if o.get("domain")}
    missing_domain_counts: dict[str, int] = defaultdict(int)
    for p in people:
        if p.get("email") and not p.get("org_id"):
            domain = _domain_of(p["email"])
            if domain and domain not in existing_domains:
                missing_domain_counts[domain] += 1

    return {
        "organizations": orgs,
        "members_by_org": members,
        "would_be_created_domains": dict(missing_domain_counts),
    }


# --- Sections C-J: canonical-reference coverage per collection ------------------------


def _missing(docs: list[dict[str, Any]], fields: list[str]) -> list[dict[str, Any]]:
    def is_missing(doc: dict[str, Any]) -> bool:
        for field in fields:
            value = doc.get(field)
            if isinstance(value, list):
                if not value:
                    return True
            elif not value:
                return True
        return False

    return [d for d in docs if is_missing(d)]


def audit_threads(db: Database) -> dict[str, Any]:
    threads = ThreadRepository(db).find_many({})
    missing = _missing(threads, ["person_ids", "org_ids"])
    return {"total": len(threads), "all": threads, "missing_canonical_refs": missing}


def audit_commitments(db: Database) -> dict[str, Any]:
    docs = CommitmentRepository(db).find_many({})
    missing = _missing(docs, ["person_id", "org_id"])
    return {"total": len(docs), "all": docs, "missing_canonical_refs": missing}


def audit_meetings(db: Database) -> dict[str, Any]:
    docs = MeetingRepository(db).find_many({})
    missing = _missing(docs, ["person_ids", "org_id"])
    non_empty_attendees = [
        d for d in docs
        if isinstance(d.get("event", {}).get("attendees"), list) and d.get("event", {}).get("attendees")
    ]
    return {"total": len(docs), "all": docs, "missing_canonical_refs": missing, "unexpected_attendee_data": non_empty_attendees}


def audit_follow_ups(db: Database) -> dict[str, Any]:
    docs = FollowUpRepository(db).find_many({})
    missing = _missing(docs, ["person_id", "org_id"])
    return {"total": len(docs), "all": docs, "missing_canonical_refs": missing}


def audit_projects(db: Database) -> dict[str, Any]:
    docs = ProjectRepository(db).find_many({})
    missing = _missing(docs, ["person_ids", "org_id"])
    return {"total": len(docs), "all": docs, "missing_canonical_refs": missing}


def audit_knowledge(db: Database) -> dict[str, Any]:
    docs = KnowledgeRepository(db).find_many({})
    canonical_person = [d for d in docs if d.get("person_id")]
    canonical_org = [d for d in docs if d.get("org_id") and not d.get("person_id")]
    legacy = [d for d in docs if not d.get("person_id") and not d.get("org_id")]
    return {
        "total": len(docs),
        "canonical_person_id": canonical_person,
        "canonical_org_id": canonical_org,
        "legacy_thread_scoped_name_match": legacy,
    }


def audit_reply_drafts(db: Database) -> dict[str, Any]:
    docs = ReplyDraftRepository(db).find_many({})
    missing = _missing(docs, ["person_id", "org_id"])
    return {"total": len(docs), "all": docs, "missing_canonical_refs": missing}


def audit_calendar_actions(db: Database) -> dict[str, Any]:
    docs = CalendarActionRepository(db).find_many({})
    missing = _missing(docs, ["person_id", "org_id"])
    external_attendees = [d for d in docs if (d.get("event") or {}).get("attendees")]
    return {
        "total": len(docs),
        "all": docs,
        "missing_canonical_refs": missing,
        "external_attendees_found": external_attendees,  # MUST always be empty
    }


# --- Phase 3: read-only duplicate-candidate reconciliation ---------------------------


def _confidence_and_evidence(
    candidate: dict[str, Any], anchor: dict[str, Any]
) -> tuple[str, list[str], list[str]]:
    candidate_threads = set(candidate.get("open_threads", []))
    anchor_threads = set(anchor.get("open_threads", []))
    shared = sorted(candidate_threads & anchor_threads)

    candidate_org = normalize_text(candidate.get("org") or "")
    anchor_org = normalize_text(anchor.get("org") or "")
    org_matches = bool(candidate_org) and candidate_org == anchor_org

    evidence = [f"matching_name_evidence: {candidate['name']!r} ~ {anchor['name']!r}"]
    if shared:
        evidence.append(f"shared_thread_ids: {shared}")
    if org_matches:
        evidence.append(f"matching_org_evidence: {candidate.get('org')!r} == {anchor.get('org')!r}")
    if candidate.get("org_id") and candidate.get("org_id") == anchor.get("org_id"):
        evidence.append(f"matching_domain_evidence: shared org_id {candidate['org_id']}")

    if shared:
        confidence = "HIGH"
    elif org_matches or (candidate.get("org_id") and candidate.get("org_id") == anchor.get("org_id")):
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    return confidence, evidence, shared


def _is_combined_email(email: str) -> bool:
    return ";" in email or "," in email


def find_duplicate_candidates(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read-only candidate generation only -- never merges, never writes.

    Two independent detection axes:

    1. Orphan -> anchor: a person with an email is an "anchor" (Person's own
       email-exact-match resolution is already correct for these -- see the standing
       audit finding: 0 duplicate normalized emails). A no-email person ("orphan")
       whose normalized name is name-compatible with one or more anchors is proposed
       against the single BEST anchor -- name-compatible anchors are narrowed first by
       shared-thread overlap (the strongest, safest tie-breaker) before falling back to
       "ambiguous, skip" only when that still doesn't resolve to exactly one. This
       matters in practice: the real data contains two distinct real "Ashok Ganapam"
       anchors (different email domains) that are name-identical, and would be
       wrongly-skipped as ambiguous without this tie-break, even though most orphans
       clearly share a thread with only one of them.

    2. Anchor -> anchor: two people who BOTH already have an email, same normalized
       name and org, are still worth surfacing (e.g. one real person whose two
       different addresses were captured as two separate records, or a combined,
       malformed "a@x; b@y" email string that never dedupes against either real
       address alone -- see resolve_person's exact-match-only email lookup).

    Lifecycle-aware (Phase 19.1): a Person already consolidated (status="merged")
    never participates in EITHER role here -- not as a "source" duplicate candidate
    (it was already merged; re-proposing it is not new information) and not as a
    "canonical" target (a retired identity is never the right place to merge
    something new into -- see app.entities.lifecycle.resolve_canonical_person_id for
    how a mention that lands on a merged record gets redirected during ingestion).
    This is a single upfront filter, not scattered per-branch checks, and it is the
    ONLY thing that changed here for lifecycle-awareness -- the two detection axes
    and their evidence/confidence logic below are unchanged.
    """
    active_people = [p for p in people if is_person_active(p)]
    anchors = [p for p in active_people if p.get("email")]
    no_email = [p for p in active_people if not p.get("email")]

    candidates: list[dict[str, Any]] = []

    for person in no_email:
        person_tokens = _name_tokens(person["name"])
        if not person_tokens:
            continue
        name_matching_anchors = [
            a for a in anchors
            if person_tokens <= _name_tokens(a["name"]) or _name_tokens(a["name"]) <= person_tokens
        ]
        if not name_matching_anchors:
            continue
        chosen = name_matching_anchors
        if len(chosen) > 1:
            person_threads = set(person.get("open_threads", []))
            with_overlap = [a for a in chosen if person_threads & set(a.get("open_threads", []))]
            if len(with_overlap) == 1:
                chosen = with_overlap  # thread evidence safely breaks the name-only tie
            else:
                continue  # still ambiguous even with thread evidence -- never guess
        anchor = chosen[0]
        confidence, evidence, shared = _confidence_and_evidence(person, anchor)
        if len(name_matching_anchors) > 1:
            evidence.append(
                f"disambiguated_among_{len(name_matching_anchors)}_name_matching_anchors_via_shared_thread"
            )
        candidates.append(
            {
                "source_person_id": person["id"],
                "candidate_canonical_person_id": anchor["id"],
                "confidence": confidence,
                "evidence": evidence,
                "shared_thread_ids": shared,
                "recommended_action": (
                    "Reasonable candidate for a human-approved merge in a future phase -- NOT performed here."
                    if confidence == "HIGH"
                    else "Do NOT auto-merge -- requires explicit human confirmation before any future action."
                ),
            }
        )

    seen_anchor_pairs: set[frozenset[str]] = set()
    for i, a in enumerate(anchors):
        for b in anchors[i + 1:]:
            if normalize_text(a["name"]) != normalize_text(b["name"]):
                continue
            a_org, b_org = normalize_text(a.get("org") or ""), normalize_text(b.get("org") or "")
            if not (a_org and a_org == b_org):
                continue
            pair_key = frozenset({a["id"], b["id"]})
            if pair_key in seen_anchor_pairs:
                continue
            seen_anchor_pairs.add(pair_key)
            confidence, evidence, shared = _confidence_and_evidence(a, b)
            evidence.append(f"matching_domain_evidence: both anchors share org {a.get('org')!r}, different emails ({a['email']!r} vs {b['email']!r})")
            if _is_combined_email(a["email"]) or _is_combined_email(b["email"]):
                evidence.append("data_quality_flag: one stored email field contains multiple addresses (';'/',' separated)")
            candidates.append(
                {
                    "source_person_id": b["id"],
                    "candidate_canonical_person_id": a["id"],
                    "confidence": confidence if confidence != "LOW" else "MEDIUM",  # same org, both anchors -> at least MEDIUM
                    "evidence": evidence,
                    "shared_thread_ids": shared,
                    "recommended_action": "Do NOT auto-merge -- two different stored email addresses require explicit human confirmation of which is canonical.",
                }
            )
    return candidates


# --- Report assembly / CLI ------------------------------------------------------------


def build_report(db: Database) -> dict[str, Any]:
    people_report = audit_people(db)
    org_report = audit_organizations(db, people_report["all_people"])
    return {
        "people": people_report,
        "organizations": org_report,
        "threads": audit_threads(db),
        "commitments": audit_commitments(db),
        "meetings": audit_meetings(db),
        "follow_ups": audit_follow_ups(db),
        "projects": audit_projects(db),
        "knowledge": audit_knowledge(db),
        "reply_drafts": audit_reply_drafts(db),
        "calendar_actions": audit_calendar_actions(db),
        "duplicate_candidates": find_duplicate_candidates(people_report["all_people"]),
    }


def _print_report(report: dict[str, Any]) -> None:
    p = report["people"]
    o = report["organizations"]

    print("## ENTITY RECONCILIATION REPORT (READ-ONLY)")
    print()
    print("### A. PEOPLE")
    print(f"Total people: {p['total']}")
    print(f"  With org_id: {len(p['with_org_id'])}")
    print(f"  Without org_id: {len(p['without_org_id'])}")
    print(f"  With email but no org_id: {len(p['email_but_no_org_id'])}")
    print(f"  Duplicate normalized emails: {len(p['duplicate_emails'])}")
    print(f"  Duplicate normalized names: {len(p['duplicate_names'])}")
    print(f"  Same-name/same-company groups: {len(p['same_name_same_company'])}")
    print()

    print("### B. ORGANIZATIONS")
    print(f"Total organizations: {len(o['organizations'])}")
    for org in sorted(o["organizations"], key=lambda x: x["id"]):
        member_count = len(o["members_by_org"].get(org["id"], []))
        print(f"  {org['id']} | {org.get('name')} | domain={org.get('domain')} | members={member_count}")
    if o["would_be_created_domains"]:
        print("  Domains that would produce a NEW organization on next contact (not created here):")
        for domain, count in sorted(o["would_be_created_domains"].items()):
            print(f"    {domain}: {count} person(s) currently without org_id")
    print()

    for label, key, ref_fields in [
        ("C. THREADS", "threads", "person_ids/org_ids"),
        ("D. COMMITMENTS", "commitments", "person_id/org_id"),
        ("E. MEETINGS", "meetings", "person_ids/org_id"),
        ("F. FOLLOW-UPS", "follow_ups", "person_id/org_id"),
        ("G. PROJECTS", "projects", "person_ids/org_id"),
        ("I. REPLY DRAFTS", "reply_drafts", "person_id/org_id"),
        ("J. CALENDAR ACTIONS", "calendar_actions", "person_id/org_id"),
    ]:
        section = report[key]
        print(f"### {label}")
        print(f"Total: {section['total']}")
        print(f"Missing {ref_fields}: {len(section['missing_canonical_refs'])}")
        print()

    k = report["knowledge"]
    print("### H. KNOWLEDGE")
    print(f"Total: {k['total']}")
    print(f"  canonical_person_id: {len(k['canonical_person_id'])}")
    print(f"  canonical_org_id: {len(k['canonical_org_id'])}")
    print(f"  legacy_thread_scoped_name_match (neither): {len(k['legacy_thread_scoped_name_match'])}")
    print()

    ca = report["calendar_actions"]
    print("### CALENDAR SAFETY CHECK")
    print(
        f"Calendar actions with any external attendee data present: "
        f"{len(ca['external_attendees_found'])} (MUST be 0)"
    )
    print()

    print("### PHASE 3: DUPLICATE PERSON CANDIDATES (read-only, none merged)")
    candidates = report["duplicate_candidates"]
    by_conf = defaultdict(int)
    for c in candidates:
        by_conf[c["confidence"]] += 1
    print(f"Total candidates: {len(candidates)}")
    print(f"  HIGH: {by_conf['HIGH']}  MEDIUM: {by_conf['MEDIUM']}  LOW: {by_conf['LOW']}")
    print()
    for c in sorted(candidates, key=lambda x: (x["confidence"] != "HIGH", x["source_person_id"])):
        print(f"  {c['source_person_id']} -> {c['candidate_canonical_person_id']} [{c['confidence']}]")
        for line in c["evidence"]:
            print(f"      {line}")
        print(f"      recommended_action: {c['recommended_action']}")
    print()
    print("NO HISTORICAL MONGODB DATA WAS MODIFIED.")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CoS Sales Agent -- read-only entity/identity reconciliation report")
    parser.add_argument(
        "--reconcile-entities", action="store_true",
        help="Run the read-only reconciliation report against the configured MongoDB database",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    if not args.reconcile_entities:
        build_arg_parser().print_help()
        return 0

    settings = get_settings()
    configure_logging(settings.log_level)
    client = get_client(settings.mongodb_uri)
    db = initialize_database(client, settings.mongodb_database)

    report = build_report(db)
    _print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
