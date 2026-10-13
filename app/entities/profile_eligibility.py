"""Milestone 1 of the context-building reliability work: a deterministic,
LLM-free gate deciding whether a Person has enough stored evidence to warrant
a profile, replacing the prior design where that decision lived entirely in
an LLM's in-the-moment judgment during the context-building skill (with no
code-level enforcement and no recorded reason).

This module never composes profile text itself -- that remains Claude's job,
via persist_person_profile (app.mcp.tools). It only decides, and records,
whether now is a good time to.
"""

from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.database.repositories import (
    CommitmentRepository,
    KnowledgeRepository,
    MeetingRepository,
    PersonContextSnapshotRepository,
    PersonRepository,
)


def _gather_evidence(db: Database, person_id: str) -> dict[str, Any]:
    snapshots = PersonContextSnapshotRepository(db).all_for_person(person_id)
    source_email_ids = sorted({s["source_email_id"] for s in snapshots if s.get("source_email_id")})
    distinct_threads = {s["thread_id"] for s in snapshots if s.get("thread_id")}

    knowledge_count = len(KnowledgeRepository(db).find_many({"person_id": person_id}))
    # Commitment.person_id is a scalar canonical reference (see app.entities.models.
    # Commitment); Meeting.person_ids is a list -- querying a list field with a bare
    # scalar matches "list contains this value" (pymongo/Mongo's own array-contains
    # semantics), the correct query for a person being one of several attendees.
    commitment_count = len(CommitmentRepository(db).find_many({"person_id": person_id}))
    meeting_count = len(MeetingRepository(db).find_many({"person_ids": person_id}))

    return {
        "email_interaction_count": len(snapshots),
        "distinct_threads": len(distinct_threads),
        "knowledge_fact_count": knowledge_count,
        "commitment_count": commitment_count,
        "meeting_count": meeting_count,
        "source_email_ids": source_email_ids,
    }


def check_profile_eligibility(db: Database, person_id: str) -> dict[str, Any]:
    """Deterministic, zero-LLM-call eligibility decision. Raises ValueError if
    person_id doesn't exist.

    Eligible when there is at least one real email interaction AND either a
    substantive signal (a knowledge fact, commitment, or meeting) or an
    ongoing relationship (2+ distinct threads) -- a single generic touch with
    nothing else known is deliberately NOT enough to warrant a profile.

    Side effect: always stamps the Person with profile_checked_at (now) and a
    profile_status -- "insufficient_evidence" or "pending_generation" -- so
    the outcome of every check is recorded regardless of whether Claude goes
    on to actually compose a profile afterward. Never writes to
    profile_summary/recent_context/key_topics -- an ineligible result can
    never clear or overwrite an existing profile. The reason text explicitly
    distinguishes "no profile yet, still insufficient" from "existing profile
    preserved, no new substantive evidence since last check", so a caller (or
    a human reading the record) never misreads "insufficient_evidence" as "no
    profile exists" when one actually does.
    """
    person_repo = PersonRepository(db)
    person = person_repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    evidence = _gather_evidence(db, person_id)
    eligible = evidence["email_interaction_count"] >= 1 and (
        evidence["knowledge_fact_count"] >= 1
        or evidence["commitment_count"] >= 1
        or evidence["meeting_count"] >= 1
        or evidence["distinct_threads"] >= 2
    )

    has_existing_profile = bool(person.get("profile_summary"))
    now = datetime.now(timezone.utc)

    if eligible:
        status = "pending_generation"
        reason = (
            f"Sufficient evidence: {evidence['email_interaction_count']} email "
            f"interaction(s) across {evidence['distinct_threads']} thread(s), "
            f"{evidence['knowledge_fact_count']} knowledge fact(s), "
            f"{evidence['commitment_count']} commitment(s), "
            f"{evidence['meeting_count']} meeting(s)."
        )
    elif has_existing_profile:
        status = "insufficient_evidence"
        reason = (
            "Existing profile preserved -- no new substantive evidence since last "
            f"check ({evidence['email_interaction_count']} email interaction(s) "
            f"across {evidence['distinct_threads']} thread(s), no additional "
            "knowledge facts, commitments, or meetings)."
        )
    else:
        status = "insufficient_evidence"
        reason = (
            f"No profile yet -- insufficient evidence ({evidence['email_interaction_count']} "
            f"email interaction(s) across {evidence['distinct_threads']} thread(s), "
            "no knowledge facts, commitments, or meetings recorded)."
        )

    person_repo.upsert_by_key(
        {"id": person_id},
        {**person, "profile_status": status, "profile_status_reason": reason, "profile_checked_at": now},
    )

    return {
        "person_id": person_id,
        "eligible": eligible,
        "reason": reason,
        "evidence": evidence,
    }


def mark_profile_generation_failed(db: Database, person_id: str, reason: str) -> dict[str, Any]:
    """Explicit, retryable failure status: Claude attempted to compose a
    profile after an eligible check but persistence did not happen (an
    aborted composition, a tool error, ambiguous/conflicting evidence it
    declined to resolve). Without this, an eligible-but-not-yet-generated
    Person is indistinguishable from one that was simply never attempted --
    this makes "we tried and it didn't work" visible and queryable, rather
    than leaving the record silently stuck at "pending_generation" forever.
    Never claims a profile was generated; never touches profile_summary/
    recent_context/key_topics. Raises ValueError if person_id doesn't exist.
    """
    person_repo = PersonRepository(db)
    person = person_repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    now = datetime.now(timezone.utc)
    person_repo.upsert_by_key(
        {"id": person_id},
        {**person, "profile_status": "generation_failed", "profile_status_reason": reason, "profile_checked_at": now},
    )
    return {"person_id": person_id, "profile_status": "generation_failed", "reason": reason}
