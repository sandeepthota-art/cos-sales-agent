"""Phase 24: Commitment + Follow-Up Intelligence. Every function here is a thin,
deterministically-ordered wrapper over app.query.retrieval's existing
retrieve_commitments/retrieve_follow_ups -- no second retrieval mechanism, no new
state machine, no invented fields.

Ownership (Section 24.1) maps directly onto the ALREADY STORED
Commitment.commitment_class values -- 'mine' (the user/agent made this commitment)
and 'owed_to_me' (someone else owes the user) are the only two ownership-bearing
classes; 'theirs' and 'recap' are neither direction and are excluded from both
ownership-scoped queries below, never guessed into one bucket or the other.

Status (24.4) and priority (24.5): Commitment.status is returned exactly as stored
(in practice always "open" -- the live pipeline never transitions it, confirmed by
code inspection) and Commitment.importance is passed through unchanged when present,
never invented when absent. No derived DUE_SOON/OVERDUE status is added to the
Commitment itself; "overdue" is a QUERY (a date-range filter), never a new stored or
computed field on the record.
"""

from datetime import datetime
from typing import Any

from pymongo.database import Database

from app.database.repositories import CommitmentRepository
from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.schemas import DateRange, DateRangeKind, OwnershipDirection, RelationshipKind

_OWNERSHIP_TO_CLASS = {
    OwnershipDirection.USER_OWES: "mine",
    OwnershipDirection.OTHER_PERSON_OWES: "owed_to_me",
}


# --- Commitments -----------------------------------------------------------------------------


def get_commitments_for_person(
    db: Database, person_id: str, ownership: OwnershipDirection | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    commitment_class = _OWNERSHIP_TO_CLASS.get(ownership) if ownership else None
    return retrieval.retrieve_commitments(db, person_id=person_id, commitment_class=commitment_class, date_range=date_range, limit=limit)


def get_commitments_for_org(
    db: Database, org_id: str, ownership: OwnershipDirection | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    commitment_class = _OWNERSHIP_TO_CLASS.get(ownership) if ownership else None
    return retrieval.retrieve_commitments(db, org_id=org_id, commitment_class=commitment_class, date_range=date_range, limit=limit)


def get_commitments_for_project(db: Database, project_id: str, limit: int = 50) -> list[dict[str, Any]]:
    """app.pipeline populates Commitment.project_id when the commitment's own
    counterparty's org matches a project already resolved for the same email (see
    app.pipeline._process_entities) -- unset whenever that evidence isn't available,
    never guessed. Returns whatever is genuinely linked."""
    return retrieval.retrieve_commitments(db, project_id=project_id, limit=limit)


def get_open_commitments(db: Database, person_id: str | None = None, org_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Filters on the real, stored Commitment.status field -- returns whatever is
    actually marked 'open', which in the current live dataset is effectively every
    commitment (status never transitions elsewhere in the pipeline)."""
    docs = retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, limit=retrieval.MAX_RESULT_LIMIT)
    return [d for d in docs if d.get("status") == "open"][:limit]


def get_user_commitments(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """'What do I owe?' -- explicitly stored class='mine' only."""
    return retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, commitment_class="mine", date_range=date_range, limit=limit)


def get_commitments_owed_to_user(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """'Who am I waiting on?' -- explicitly stored class='owed_to_me' only."""
    return retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, commitment_class="owed_to_me", date_range=date_range, limit=limit)


def get_overdue_commitments(
    db: Database, reference_datetime: datetime, timezone: str,
    person_id: str | None = None, org_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    overdue = resolve_date_range(DateRangeKind.OVERDUE, reference_datetime, timezone)
    return retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, date_range=overdue, limit=limit)


# --- Follow-ups ------------------------------------------------------------------------------


def get_followups_for_person(db: Database, person_id: str, date_range: DateRange | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return retrieval.retrieve_follow_ups(db, person_id=person_id, date_range=date_range, limit=limit)


def get_followups_for_org(db: Database, org_id: str, date_range: DateRange | None = None, limit: int = 50) -> list[dict[str, Any]]:
    return retrieval.retrieve_follow_ups(db, org_id=org_id, date_range=date_range, limit=limit)


def get_open_followups(db: Database, person_id: str | None = None, org_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Filters on the real, stored FollowUp.status field -- "open" means status=="active"
    (a FollowUp not yet resolved or dropped), mirroring get_open_commitments' own
    status-filter pattern. A legacy FollowUp document written before this field
    existed (the key entirely absent, not just null) is treated as "active" too --
    the same missing-means-active convention app.entities.lifecycle already applies
    to Person.status -- so nothing here silently excludes pre-existing records."""
    docs = retrieval.retrieve_follow_ups(db, person_id=person_id, org_id=org_id, limit=retrieval.MAX_RESULT_LIMIT)
    return [d for d in docs if d.get("status", "active") == "active"][:limit]


def get_followups_by_escalation_level(
    db: Database, level: int, person_id: str | None = None, org_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Filters on the real, stored FollowUp.escalation_level (1-4). Returns whatever is
    genuinely stored -- nothing here advances or infers a level."""
    docs = retrieval.retrieve_follow_ups(db, person_id=person_id, org_id=org_id, limit=retrieval.MAX_RESULT_LIMIT)
    return [d for d in docs if d.get("escalation_level") == level][:limit]


def get_followups_by_audience(
    db: Database, audience: str, person_id: str | None = None, org_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Filters on the real, stored FollowUp.audience (BRD 6.4: internal/
    client_fixed_date/client_open_window/his_own_question). A FollowUp with
    audience=None (e.g. derived from a "mine" commitment, which BRD 6.4's timing rules
    never apply to) never matches any of these four values."""
    docs = retrieval.retrieve_follow_ups(db, person_id=person_id, org_id=org_id, limit=retrieval.MAX_RESULT_LIMIT)
    return [d for d in docs if d.get("audience") == audience][:limit]


_ESCALATION_LEVEL_DESCRIPTIONS = {
    1: "Surfaced in a brief.",
    2: "At the top of the queue, with an explicit line that it needs a decision.",
    3: "Has its own email and a 15-minute hold on his calendar.",
    4: "Stopped -- he acted, or said to drop it.",
}


def describe_escalation_level(level: int) -> str:
    """Pure, read-only description of what a given escalation level (1-4) means, in
    the BRD's own words (6.4) -- never computes or advances a level itself. Raises
    ValueError for anything outside 1-4, the only values FollowUp.escalation_level
    can hold."""
    if level not in _ESCALATION_LEVEL_DESCRIPTIONS:
        raise ValueError(f"escalation level must be 1-4, got {level!r}")
    return _ESCALATION_LEVEL_DESCRIPTIONS[level]


def next_escalation_level(current: int) -> int:
    """The next escalation level after `current`, capped at 4 (the BRD's terminal
    level) -- purely structural (1->2->3->4->4), never time-based. No scheduler in
    this codebase calls this yet; it exists so a future one has a single, correct place
    to ask "what level comes next" rather than reimplementing the ladder inline."""
    if current not in _ESCALATION_LEVEL_DESCRIPTIONS:
        raise ValueError(f"escalation level must be 1-4, got {current!r}")
    return min(current + 1, 4)


def get_overdue_followups(
    db: Database, reference_datetime: datetime, timezone: str,
    person_id: str | None = None, org_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Uses the parent Commitment's committed_date via the real, stored
    FollowUp.commitment_id relationship (app.query.retrieval.retrieve_follow_ups
    already implements this join) -- a FollowUp whose parent has no committed_date
    is excluded, never guessed into "overdue"."""
    overdue = resolve_date_range(DateRangeKind.OVERDUE, reference_datetime, timezone)
    return retrieval.retrieve_follow_ups(db, person_id=person_id, org_id=org_id, date_range=overdue, limit=limit)


def get_followups_for_commitment(db: Database, commitment_id: str, limit: int = 50) -> list[dict[str, Any]]:
    from app.database.repositories import FollowUpRepository

    return FollowUpRepository(db).find_many({"commitment_id": commitment_id})[:limit]


# --- Meeting <-> Commitment relationship (Section 24.6) ---------------------------------------


def get_meeting_related_commitments(db: Database, meeting_id: str) -> dict[str, Any]:
    """Meeting and Commitment have NO direct stored foreign key to each other
    (confirmed: Commitment has no meeting_id field, Meeting has no commitments
    list) -- the only real connection is a SHARED thread_id. This is always
    reported as RelationshipKind.INDIRECT_VIA_THREAD, never presented as a direct
    relationship the schema doesn't actually have.
    """
    meeting = retrieval.retrieve_meeting_by_id(db, meeting_id)
    if meeting is None or not meeting.get("thread_id"):
        return {"commitments": [], "relationship": RelationshipKind.INDIRECT_VIA_THREAD.value, "note": "no meeting or no thread_id to join on"}
    commitments = CommitmentRepository(db).all_for_thread(meeting["thread_id"])
    return {
        "commitments": commitments,
        "relationship": RelationshipKind.INDIRECT_VIA_THREAD.value,
        "note": "Commitment has no direct meeting reference; these share the meeting's thread_id only.",
    }
