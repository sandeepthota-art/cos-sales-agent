"""Phase 23: Meeting Intelligence. Read-only -- never creates a CalendarAction,
never writes CalendarEvent.attendees, never modifies a Meeting. Reuses
app.query.retrieval's existing bounded functions and app.entities.context's
Person/Organization 360 rather than building a second retrieval mechanism.

Attendee lifecycle handling is the one deliberate difference from Phase 19.1's
general Person-360 boundary: a MeetingBrief always resolves each attendee to its
CANONICAL active Person first (via app.entities.lifecycle.resolve_canonical_person_id)
before assembling that attendee's context -- so a brief never shows a retired
fragment's thinner view when the live, active Person's full context is what a human
preparing for the meeting actually needs. A broken/circular chain for one attendee
is recorded in `attendee_resolution_notes` and that attendee is skipped; it never
fails the whole brief or invents a replacement identity.
"""

from datetime import datetime
from typing import Any

from pymongo.database import Database

from app.database.repositories import CommitmentRepository, MeetingRepository, PersonRepository
from app.entities.context import get_organization_context, get_person_context
from app.entities.lifecycle import CanonicalResolutionError, resolve_canonical_person_id
from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.evidence import build_evidence_items
from app.query.schemas import DateRangeKind, MeetingBrief, MeetingClassification


def _domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.strip().lower().split("@", 1)[1] or None


def classify_meeting(db: Database, meeting: dict[str, Any], agent_email: str | None) -> MeetingClassification:
    """Only INTERNAL/CUSTOMER/UNKNOWN are ever actually derived -- see
    app.query.schemas.MeetingClassification for why SALES/FINANCE/PROSPECT/PROJECT
    are never assigned: no stored field reliably supports them.
    """
    person_ids = meeting.get("person_ids") or []
    if not person_ids:
        return MeetingClassification.UNKNOWN
    agent_domain = _domain_of(agent_email)
    if not agent_domain:
        return MeetingClassification.UNKNOWN

    attendee_domains = []
    for pid in person_ids:
        person = PersonRepository(db).find_one({"id": pid})
        if person and person.get("email"):
            attendee_domains.append(_domain_of(person["email"]))
    if not attendee_domains:
        return MeetingClassification.UNKNOWN
    if all(d == agent_domain for d in attendee_domains):
        return MeetingClassification.INTERNAL
    return MeetingClassification.CUSTOMER


def _resolve_canonical_attendees(db: Database, person_ids: list[str]) -> tuple[list[str], list[str]]:
    canonical_ids: list[str] = []
    notes: list[str] = []
    for pid in person_ids:
        try:
            canonical_id = resolve_canonical_person_id(db, pid)
        except CanonicalResolutionError as exc:
            notes.append(f"attendee {pid!r} could not be canonically resolved: {exc}")
            continue
        if canonical_id not in canonical_ids:
            canonical_ids.append(canonical_id)
    return canonical_ids, notes


def get_meeting_brief(db: Database, meeting_id: str, agent_email: str | None = None) -> MeetingBrief | None:
    """The Phase 23 replacement for Phase 22A's simpler retrieve_meeting_preparation
    -- reuses retrieval.retrieve_meeting_by_id (the same lookup, not re-derived) and
    adds canonical attendee resolution, classification, previous meetings, open
    commitments, relevant follow-ups/knowledge, and existing reply drafts, all
    evidence-backed. Returns None if the meeting doesn't exist -- never fabricates one.
    """
    meeting = retrieval.retrieve_meeting_by_id(db, meeting_id)
    if meeting is None:
        return None

    canonical_ids, notes = _resolve_canonical_attendees(db, meeting.get("person_ids") or [])
    attendee_contexts = [ctx for pid in canonical_ids if (ctx := get_person_context(db, pid)) is not None]

    organization_context = get_organization_context(db, meeting["org_id"]) if meeting.get("org_id") else None
    thread_context = retrieval.retrieve_thread_context(db, meeting["thread_id"]) if meeting.get("thread_id") else None

    # Previous meetings: same thread, strictly earlier date -- a real, stored
    # relationship (shared thread_id), never a guessed one.
    previous_meetings: list[dict[str, Any]] = []
    if meeting.get("thread_id"):
        siblings = MeetingRepository(db).all_for_thread(meeting["thread_id"])
        previous_meetings = sorted(
            [m for m in siblings if m["id"] != meeting["id"] and (m.get("date") or "") < (meeting.get("date") or "")],
            key=lambda m: m.get("date") or "", reverse=True,
        )[:10]

    open_commitments: list[dict[str, Any]] = []
    relevant_follow_ups: list[dict[str, Any]] = []
    relevant_knowledge: list[dict[str, Any]] = []
    for pid in canonical_ids:
        open_commitments.extend(retrieval.retrieve_commitments(db, person_id=pid, limit=10))
        relevant_follow_ups.extend(retrieval.retrieve_follow_ups(db, person_id=pid, limit=10))
        relevant_knowledge.extend(retrieval.retrieve_knowledge(db, person_id=pid, limit=10))
    if meeting.get("org_id"):
        open_commitments.extend(retrieval.retrieve_commitments(db, org_id=meeting["org_id"], limit=10))

    existing_reply_drafts = (
        retrieval.retrieve_reply_drafts(db, thread_id=meeting["thread_id"], limit=10) if meeting.get("thread_id") else []
    )

    classification = classify_meeting(db, meeting, agent_email)

    evidence = (
        build_evidence_items("meetings", [meeting], "id", ["date", "attendees", "actionable"], timestamp_field="date")
        + build_evidence_items("commitments", open_commitments, "id", ["what", "class"], timestamp_field="made_on")
        + build_evidence_items("follow_ups", relevant_follow_ups, "id", ["commitment_id"])
        + build_evidence_items("knowledge_items", relevant_knowledge, "knowledge_id", ["subject_key", "predicate", "current_value", "basis"])
        + build_evidence_items("reply_drafts", existing_reply_drafts, "reply_id", ["status"], timestamp_field="created_at")
    )

    return MeetingBrief(
        meeting=meeting, classification=classification,
        canonical_attendee_ids=canonical_ids, attendee_contexts=attendee_contexts, attendee_resolution_notes=notes,
        organization_context=organization_context, project_context=None,  # Meeting has no explicit project link (Section 23A)
        thread_context=thread_context, previous_meetings=previous_meetings,
        open_commitments=open_commitments, relevant_follow_ups=relevant_follow_ups,
        relevant_knowledge=relevant_knowledge, existing_reply_drafts=existing_reply_drafts,
        evidence=evidence,
    )


def get_meeting_brief_for_person(
    db: Database, person_id: str, agent_email: str | None = None, limit: int = 10,
) -> list[MeetingBrief]:
    """Canonically resolves person_id first (a merged id must never silently return
    zero meetings just because its own person_ids entries were repointed away from
    it during consolidation) -- reuses retrieval.retrieve_meetings unchanged.
    """
    try:
        canonical_id = resolve_canonical_person_id(db, person_id)
    except CanonicalResolutionError:
        return []
    meetings = retrieval.retrieve_meetings(db, person_id=canonical_id, limit=limit)
    return [b for m in meetings if (b := get_meeting_brief(db, m["id"], agent_email)) is not None]


def get_upcoming_meeting_briefs(
    db: Database, reference_datetime: datetime, timezone: str, person_id: str | None = None,
    org_id: str | None = None, agent_email: str | None = None, limit: int = 10,
) -> list[MeetingBrief]:
    date_range = resolve_date_range(DateRangeKind.UPCOMING, reference_datetime, timezone)
    canonical_person_id = None
    if person_id:
        try:
            canonical_person_id = resolve_canonical_person_id(db, person_id)
        except CanonicalResolutionError:
            return []
    meetings = retrieval.retrieve_meetings(db, person_id=canonical_person_id, org_id=org_id, date_range=date_range, limit=limit)
    return [b for m in meetings if (b := get_meeting_brief(db, m["id"], agent_email)) is not None]
