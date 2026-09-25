"""Phase 22A: structured, bounded MongoDB retrieval -- one function per supported
intent. Every function here is read-only (find/find_many only) and reuses existing
repositories/context functions rather than re-deriving them:
  - Person/Organization 360: app.entities.context.get_person_context/
    get_organization_context, unchanged.
  - Project summary: app.mcp.tools.get_project_summary, unchanged.
  - Everything else: the same repository classes app.mcp.tools' own list_* functions
    already use, extended with the person_id/org_id/date-range filtering those
    existing MCP tools don't yet expose as parameters (this module ADDS that
    capability; it does not modify or duplicate the existing tools).

All results are bounded (a hard MAX_RESULT_LIMIT ceiling) and deterministically
ordered -- never an unbounded collection dump into an LLM's context (Section I/AB).
"""

from datetime import datetime
from typing import Any

from pymongo.database import Database

from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    PersonRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.entities.context import get_organization_context, get_person_context
from app.query.schemas import DateRange

MAX_RESULT_LIMIT = 200


def _clamp(limit: int) -> int:
    return max(1, min(limit, MAX_RESULT_LIMIT))


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _in_range(value: datetime | None, date_range: DateRange | None) -> bool:
    """A record with no date at all never matches a bounded range query -- it is
    neither included nor silently treated as "always current." ALL_TIME/None ranges
    match everything (including undated records), since no bound was requested.
    """
    if date_range is None or date_range.kind.value == "all_time":
        return True
    if value is None:
        return False
    if date_range.start is not None and value < date_range.start:
        return False
    if date_range.end is not None and value >= date_range.end:
        return False
    return True


# --- MEETINGS ---------------------------------------------------------------------------


def retrieve_meetings(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if person_id:
        query["person_ids"] = person_id
    if org_id:
        query["org_id"] = org_id
    docs = MeetingRepository(db).find_many(query)
    docs = [d for d in docs if _in_range(_parse_iso(d.get("date")), date_range)]
    docs.sort(key=lambda d: d.get("date") or "", reverse=(date_range is not None and date_range.kind.value in ("latest", "recent", "overdue")))
    if date_range is not None and date_range.kind.value == "latest":
        docs = docs[:1]
    return docs[: _clamp(limit)]


# --- COMMITMENTS --------------------------------------------------------------------------


def retrieve_commitments(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    commitment_class: str | None = None, project_id: str | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """commitment_class follows the stored `class` values exactly as the model
    defines them (mine/owed_to_me/theirs/recap) -- never invented here. Date
    filtering uses committed_date when present, else made_on (committed_date is
    None for many real commitments -- see app.entities.models.Commitment).
    project_id (Phase 24.2) filters on the real stored field -- app.pipeline sets it
    only when the commitment's counterparty's org matches a project resolved in the
    same email (see app.pipeline._process_entities); this filter returns whatever is
    genuinely stored, never anything inferred here.
    """
    query: dict[str, Any] = {}
    if person_id:
        query["person_id"] = person_id
    if org_id:
        query["org_id"] = org_id
    if commitment_class:
        query["class"] = commitment_class
    if project_id:
        query["project_id"] = project_id
    docs = CommitmentRepository(db).find_many(query)
    docs = [d for d in docs if _in_range(_parse_iso(d.get("committed_date") or d.get("made_on")), date_range)]
    docs.sort(key=lambda d: d.get("committed_date") or d.get("made_on") or "", reverse=True)
    if date_range is not None and date_range.kind.value == "latest":
        docs = docs[:1]
    return docs[: _clamp(limit)]


# --- FOLLOW_UPS -----------------------------------------------------------------------------


def retrieve_follow_ups(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """FollowUp has no due-date or status field of its own (confirmed against
    app.entities.models.FollowUp and app.mcp.tools.list_follow_ups's own docstring)
    -- "overdue"/date-scoped follow-up questions can only be answered by joining to
    the PARENT Commitment's committed_date, a real stored relationship
    (FollowUp.commitment_id), never a fabricated one. A follow-up whose parent
    commitment has no committed_date (common in the real dataset) is excluded from
    any date-bounded query rather than guessed into or out of range.
    """
    query: dict[str, Any] = {}
    if person_id:
        query["person_id"] = person_id
    if org_id:
        query["org_id"] = org_id
    docs = FollowUpRepository(db).find_many(query)

    if date_range is not None and date_range.kind.value != "all_time":
        commitment_ids = [d["commitment_id"] for d in docs if d.get("commitment_id")]
        commitments_by_id = {c["id"]: c for c in CommitmentRepository(db).find_many({"id": {"$in": commitment_ids}})} if commitment_ids else {}
        docs = [
            d for d in docs
            if d.get("commitment_id") and _in_range(_parse_iso((commitments_by_id.get(d["commitment_id"]) or {}).get("committed_date")), date_range)
        ]

    docs.sort(key=lambda d: d.get("id", ""))
    return docs[: _clamp(limit)]


# --- KNOWLEDGE -------------------------------------------------------------------------------


def retrieve_knowledge(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    thread_id: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Never infers a new fact or upgrades basis="inferred" to a stated one --
    returns each item exactly as stored, including its basis, so the synthesis layer
    can preserve provenance (Section L).
    """
    query: dict[str, Any] = {}
    if person_id:
        query["person_id"] = person_id
    if org_id:
        query["org_id"] = org_id
    if thread_id:
        query["thread_id"] = thread_id
    docs = KnowledgeRepository(db).find_many(query)
    docs.sort(key=lambda d: d.get("last_confirmed_at") or "", reverse=True)
    return docs[: _clamp(limit)]


# --- REPLY_DRAFTS ----------------------------------------------------------------------------


def retrieve_reply_drafts(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    thread_id: str | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    query: dict[str, Any] = {}
    if person_id:
        query["person_id"] = person_id
    if org_id:
        query["org_id"] = org_id
    if thread_id:
        query["thread_id"] = thread_id
    if status:
        query["status"] = status
    docs = ReplyDraftRepository(db).find_many(query)
    docs.sort(key=lambda d: d.get("created_at") or "", reverse=True)
    return docs[: _clamp(limit)]


# --- THREAD_CONTEXT --------------------------------------------------------------------------


def retrieve_thread_context(db: Database, thread_id: str) -> dict[str, Any] | None:
    """Assembles a thread's own record plus every collection that carries thread_id
    -- reusing existing repository methods (all_for_thread) unchanged. Returns None
    if the thread doesn't exist; never fabricates one.
    """
    thread = ThreadRepository(db).find_one({"thread_id": thread_id})
    if thread is None:
        return None
    latest_snapshot = ContextSnapshotRepository(db).latest_for_thread(thread_id)
    return {
        "thread": thread,
        "latest_context_summary": (latest_snapshot or {}).get("context", {}).get("summary") if latest_snapshot else None,
        "commitments": CommitmentRepository(db).all_for_thread(thread_id),
        "meetings": MeetingRepository(db).all_for_thread(thread_id),
        "knowledge": KnowledgeRepository(db).all_for_thread(thread_id),
        "reply_drafts": ReplyDraftRepository(db).find_many({"thread_id": thread_id}),
        "calendar_actions": CalendarActionRepository(db).find_many({"thread_id": thread_id}),
    }


def retrieve_latest_thread(db: Database, person_id: str | None = None) -> dict[str, Any] | None:
    """"the latest thread" for a person is defined via that Person's own open_threads
    plus each thread's stored last_message_at -- never re-derived from raw email scans.
    With no person_id, falls back to the single most-recently-active thread overall.
    """
    if person_id:
        person = PersonRepository(db).find_one({"id": person_id})
        if person is None or not person.get("open_threads"):
            return None
        threads = ThreadRepository(db).find_many({"thread_id": {"$in": person["open_threads"]}})
    else:
        threads = ThreadRepository(db).find_many({})
    if not threads:
        return None
    latest = max(threads, key=lambda t: t.get("last_message_at") or "")
    return retrieve_thread_context(db, latest["thread_id"])


# --- PERSON_CONTEXT / ORGANIZATION_CONTEXT --------------------------------------------------
# Deliberately thin re-exports -- the query layer must not build a second Person/Org 360.


def retrieve_person_context(db: Database, person_id: str) -> dict[str, Any] | None:
    return get_person_context(db, person_id)


def retrieve_organization_context(db: Database, org_id: str) -> dict[str, Any] | None:
    return get_organization_context(db, org_id)


# --- PROJECT_CONTEXT -------------------------------------------------------------------------


def retrieve_project_context(db: Database, project_id: str) -> dict[str, Any] | None:
    from app.mcp.tools import get_project_summary  # local import: avoids a hard app.query -> app.mcp dependency for every other intent

    return get_project_summary(db, project_id)


# --- MEETING_PREPARATION ---------------------------------------------------------------------


def retrieve_meeting_preparation(db: Database, meeting_id: str) -> dict[str, Any] | None:
    """Assembles a read-only evidence package for one meeting: the meeting itself,
    its attendees' canonical Person/Organization context, the thread it belongs to,
    and any existing reply drafts for that thread. Never creates a calendar action
    or modifies the meeting.
    """
    meeting = None
    for m in MeetingRepository(db).find_many({"id": meeting_id}):
        meeting = m
        break
    if meeting is None:
        return None

    attendee_contexts = [
        ctx for pid in (meeting.get("person_ids") or [])
        if (ctx := get_person_context(db, pid)) is not None
    ]
    organization_context = get_organization_context(db, meeting["org_id"]) if meeting.get("org_id") else None
    thread_context = retrieve_thread_context(db, meeting["thread_id"]) if meeting.get("thread_id") else None

    return {
        "meeting": meeting,
        "attendee_contexts": attendee_contexts,
        "organization_context": organization_context,
        "thread_context": thread_context,
    }


def retrieve_meeting_by_id(db: Database, meeting_id: str) -> dict[str, Any] | None:
    """Direct id lookup, no free-text extraction (Section 22B.4) -- the one place
    app.query.meetings.get_meeting_brief and retrieve_meeting_preparation both fetch
    a meeting by id, so neither re-derives the other's lookup.
    """
    return next(iter(MeetingRepository(db).find_many({"id": meeting_id})), None)


# --- EMAIL_ACTIVITY --------------------------------------------------------------------------


def retrieve_email_activity(
    db: Database, person_id: str | None = None, org_id: str | None = None,
    thread_id: str | None = None, date_range: DateRange | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    """Bounded, read-only email retrieval using only fields the live pipeline
    actually populates: `entities_referenced.people` (the real canonical link every
    processed email already carries -- see app.entities.context.get_person_context's
    own emails query) for person scoping, and Thread.message_ids (NOT a `thread_id`
    field on the Email document itself, which the live pipeline never sets -- see
    app.mcp.tools' own established `EmailRepository.find_many({"message_id":
    {"$in": thread["message_ids"]}})` pattern) for thread scoping.

    Never fabricates sentiment, response status, sales stage, or priority -- returns
    each email's real stored fields only (label_applied is a real, LLM-set
    classification field and is passed through as-is, never invented here).
    """
    query: dict[str, Any] = {}
    if person_id:
        query["entities_referenced.people"] = person_id
    elif org_id:
        org_person_ids = [p["id"] for p in PersonRepository(db).find_many({"org_id": org_id})]
        query["entities_referenced.people"] = {"$in": org_person_ids}
    if thread_id:
        thread = ThreadRepository(db).find_one({"thread_id": thread_id})
        query["message_id"] = {"$in": (thread or {}).get("message_ids", [])}

    docs = EmailRepository(db).find_many(query)
    docs = [d for d in docs if _in_range(_parse_iso(d.get("timestamp")), date_range)]
    docs.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    if date_range is not None and date_range.kind.value == "latest":
        docs = docs[:1]
    return docs[: _clamp(limit)]
