import re
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.analysis.schemas import EmailAnalysis
from app.calendar.actions import build_calendar_action
from app.calendar.detector import detect_meeting
from app.config.settings import Settings
from app.context.diff import diff_context
from app.context.engine import apply_context_delta
from app.context.models import ContextDelta, ThreadContext
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.email.models import Email, parse_email
from app.entities.resolution import resolve_canonical_person_for_email
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.llm_provider import LLMProvider
from app.knowledge_lookup import KnowledgeLookup
from app.pipeline import (
    _GMAIL_INTERNAL_ID_PATTERN,
    _link_knowledge_to_entities,
    _link_thread_to_entities,
    _process_entities,
    _process_knowledge,
    ingest_raw_email,
    resolve_and_persist_thread,
    run_pipeline,
)
from app.processing.models import ProcessingStage
from app.providers.email.mock import MockEmailProvider
from app.providers.llm.mock import MockLLMProvider
from app.replies.models import ReplyDraft, ReplyDraftContent

_PENDING_CALENDAR_STATUSES = ("awaiting_approval", "needs_clarification")
_BODY_PREVIEW_LENGTH = 150

# Applied to every new list_*/search_* tool's `limit` parameter -- bounds worst-case
# query/response size regardless of what a caller passes in.
_MAX_QUERY_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_QUERY_LIMIT))


def _safe_processing_status(email: dict[str, Any]) -> dict[str, Any | None]:
    """Emails inserted by --mode=raw-file (or any other path that never calls
    EmailRepository.set_stage) have no `processing_status` field at all. stage=None
    here is never produced by the real pipeline (which always writes a concrete
    stage string, even on failure) -- so this stays unambiguously distinguishable
    from both a completed and a failed email, without fabricating either.
    """
    status = email.get("processing_status")
    if not status:
        return {"stage": None, "error": None}
    return {"stage": status.get("stage"), "error": status.get("error")}


def _thread_id_index(db: Database) -> dict[str, str]:
    """Maps message_id -> thread_id using the `threads` collection's own message_ids
    arrays -- the authoritative source (an email document's own `thread_id` field is
    only ever the raw, as-ingested value and is never updated once thread resolution
    assigns it to a possibly different existing thread; see app/pipeline.py).
    """
    index: dict[str, str] = {}
    for thread in ThreadRepository(db).find_many({}):
        for message_id in thread["message_ids"]:
            index[message_id] = thread["thread_id"]
    return index


def _empty_result(status: str, error: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "thread_id": None,
        "context_summary": None,
        "knowledge": [],
        "reply_draft": None,
        "calendar_proposal": None,
    }


def process_email(
    db: Database,
    email: Email,
    llm_provider: LLMProvider,
    calendar_provider: CalendarProvider,
    settings: Settings,
) -> dict[str, Any]:
    raw = email.model_dump(mode="json", by_alias=True)
    provider = MockEmailProvider(payloads=[raw])
    summary = run_pipeline(db, provider, llm_provider, calendar_provider, settings)

    if not summary.results:
        return _empty_result("failed", "pipeline produced no result for this email")

    result = summary.results[0]

    if result.final_stage == "FAILED":
        return _empty_result("failed", result.error)

    snapshot = ContextSnapshotRepository(db).find_one({"triggering_email_id": email.message_id})
    thread_id = snapshot["thread_id"] if snapshot else None

    knowledge = KnowledgeRepository(db).all_for_thread(thread_id) if thread_id else []
    draft = ReplyDraftRepository(db).find_one({"source_email_id": email.message_id})
    calendar_actions = (
        CalendarActionRepository(db).find_many({"thread_id": thread_id}) if thread_id else []
    )
    pending_calendar = next(
        (a for a in calendar_actions if a["status"] in _PENDING_CALENDAR_STATUSES), None
    )

    return {
        "status": result.final_stage.lower(),
        "error": None,
        "thread_id": thread_id,
        "context_summary": snapshot["context"]["summary"] if snapshot else None,
        "knowledge": [
            {
                "subject_key": k["subject_key"],
                "predicate": k["predicate"],
                "current_value": k["current_value"],
                "basis": k["basis"],
                "confidence": k["confidence"],
            }
            for k in knowledge
        ],
        "reply_draft": draft["draft"] if draft else None,
        "calendar_proposal": (
            {
                "status": pending_calendar["status"],
                "title": pending_calendar["event"]["title"],
                "start": pending_calendar["event"]["start"],
                "end": pending_calendar["event"]["end"],
                "timezone": pending_calendar["event"]["timezone"],
                "reason": pending_calendar.get("reason"),
            }
            if pending_calendar
            else None
        ),
    }


# --- Deterministic MCP boundary (Phase 1 of the Claude-Desktop-reasoning architecture) ---
#
# The five functions below let a caller that has ALREADY done the LLM reasoning itself
# (e.g. Claude Desktop, reading Gmail directly) persist the result through this
# project's existing deterministic application logic, without process_email's
# built-in external LLM API call. Every one of them is a thin orchestration wrapper
# around functions app.pipeline.run_pipeline itself already calls -- none of entity
# resolution, commitment resolution, follow-up derivation, meeting detection, date
# parsing, context merging, or draft persistence is reimplemented here.
#
# None of these five ever construct a real LLM provider or call
# ProviderFactory.create_llm_provider. process_email (above) is completely unchanged
# and remains the full LLM-driven path.


def ingest_email(db: Database, email: Email) -> dict[str, Any]:
    """Deterministic MCP entry point: everything run_pipeline does BEFORE any LLM
    call -- the existing message_id+COMPLETED duplicate check, raw persistence, and
    thread resolution -- reusing app.pipeline.ingest_raw_email and
    app.pipeline.resolve_and_persist_thread exactly as run_pipeline itself uses them.
    Never calls an LLM.

    Returns enough for a reasoning caller (e.g. Claude Desktop) to decide whether to
    bother reasoning about this email at all: if already_completed is true, the
    662-historical-email baseline (and any other previously completed email) is
    reported back untouched rather than reprocessed.
    """
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    context_repo = ContextSnapshotRepository(db)

    normalized, already_completed = ingest_raw_email(email_repo, email)

    if already_completed:
        thread_id = _thread_id_index(db).get(normalized.message_id)
        snapshot = context_repo.latest_for_thread(thread_id) if thread_id else None
        return {
            "message_id": normalized.message_id,
            "thread_id": thread_id,
            "already_completed": True,
            "previous_context": snapshot["context"] if snapshot else None,
        }

    thread_id = resolve_and_persist_thread(thread_repo, email_repo, normalized)
    snapshot = context_repo.latest_for_thread(thread_id)
    return {
        "message_id": normalized.message_id,
        "thread_id": thread_id,
        "already_completed": False,
        "previous_context": snapshot["context"] if snapshot else None,
    }


def persist_email_analysis(
    db: Database, message_id: str, analysis: EmailAnalysis, settings: Settings
) -> dict[str, Any]:
    """Deterministic MCP entry point: accepts an EmailAnalysis a reasoning caller
    (e.g. Claude Desktop) produced externally -- the SAME schema
    app.analysis.schemas.EmailAnalysis that analyze_email_with_validation already
    validates a real LLM's output against, no new schema -- and runs the exact same
    deterministic knowledge/entity/commitment/follow-up/meeting/personal-item
    persistence run_pipeline performs with one, by calling
    app.pipeline._process_knowledge and app.pipeline._process_entities unchanged.
    Meeting *calendar-invite* detection (app.calendar.detector.detect_meeting) is
    also run here, exactly as run_pipeline does -- it is pure regex over the stored
    email body and was never LLM-dependent in the first place.

    Never calls analyze_email/update_context/draft_reply or constructs a real,
    externally-configured LLM provider. The one place the existing pipeline touches an
    LLM downstream of analysis -- process_new_fact()'s ambiguous-similarity-band
    verify_same_fact() call, inside _process_knowledge -- is given a fresh
    MockLLMProvider() here instead of whatever LLM_PROVIDER is configured.
    MockLLMProvider.verify_same_fact is a pure rapidfuzz threshold comparison
    (app/providers/llm/mock.py) -- it makes no network call and needs no API key, so
    this path never silently reaches a real LLM API even for that one ambiguous-dedup
    case.

    Requires ingest_email to have already run for this message_id (raises ValueError
    otherwise, rather than silently fabricating a thread).
    """
    email_repo = EmailRepository(db)
    knowledge_repo = KnowledgeRepository(db)
    calendar_repo = CalendarActionRepository(db)

    stored = email_repo.find_one({"message_id": message_id})
    if stored is None:
        raise ValueError(f"no email found for message_id={message_id!r} -- call ingest_email first")

    thread_id = _thread_id_index(db).get(message_id)
    if thread_id is None:
        raise ValueError(
            f"message_id={message_id!r} has not been thread-resolved yet -- call ingest_email first"
        )

    email = parse_email(stored)
    reference_now = email.timestamp

    email_repo.set_stage(message_id, ProcessingStage.ANALYZED.value)

    _process_knowledge(
        knowledge_repo, thread_id, analysis, MockLLMProvider(), thread_id, message_id, datetime.now(timezone.utc)
    )
    email_repo.set_stage(message_id, ProcessingStage.KNOWLEDGE_PROCESSED.value)

    entities_referenced = _process_entities(
        db, thread_id, email, analysis, reference_now, settings.agent_email
    )
    source_link = (
        f"https://mail.google.com/mail/u/0/#all/{message_id}"
        if _GMAIL_INTERNAL_ID_PATTERN.match(message_id)
        else None
    )
    email_repo.set_entity_metadata(
        message_id=message_id,
        record_id=message_id,
        source_type="gmail",
        source_link=source_link,
        date=email.timestamp.date().isoformat(),
        entities_referenced=entities_referenced,
        goal_pillar=analysis.goal_pillar,
        label_applied=analysis.label_applied,
        confidence=analysis.confidence,
        priority=analysis.priority,
    )
    email_repo.set_stage(message_id, ProcessingStage.ENTITIES_PROCESSED.value)

    # Same additive linking passes run_pipeline itself runs, after entity resolution.
    _link_knowledge_to_entities(db, thread_id, message_id)
    _link_thread_to_entities(db, thread_id)

    # Phase 20.1: lifecycle-aware, not a raw email lookup -- see
    # app.entities.resolution.resolve_canonical_person_for_email.
    sender_person = resolve_canonical_person_for_email(db, email.from_.email)
    sender_person_id = sender_person["id"] if sender_person else None
    sender_org_id = sender_person.get("org_id") if sender_person else None

    detection = detect_meeting(email, thread_id, settings.timezone, reference_now)
    action = build_calendar_action(detection, thread_id, reference_now=reference_now)
    if action is not None:
        action = action.model_copy(
            update={
                "person_id": sender_person_id,
                "org_id": sender_org_id,
                "meeting_id": next(iter(entities_referenced.get("meetings", [])), None),
            }
        )
        calendar_key = {"thread_id": action.thread_id, "meeting_fingerprint": action.meeting_fingerprint}
        # Same guard run_pipeline itself uses: never blind-overwrite a calendar
        # action a human may already have approved/scheduled.
        if calendar_repo.find_one(calendar_key) is None:
            calendar_repo.upsert_by_key(calendar_key, action.model_dump(mode="json"))
    email_repo.set_stage(message_id, ProcessingStage.MEETING_PROCESSED.value)

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "entities_referenced": entities_referenced,
        "calendar_proposal": (
            {
                "status": action.status,
                "title": action.event.title,
                "start": action.event.start.isoformat(),
                "end": action.event.end.isoformat(),
                "timezone": action.event.timezone,
                "reason": action.reason,
            }
            if action is not None
            else None
        ),
    }


def persist_context_delta(db: Database, thread_id: str, message_id: str, delta: ContextDelta) -> dict[str, Any]:
    """Deterministic MCP entry point: accepts a ContextDelta a reasoning caller (e.g.
    Claude Desktop) produced externally -- the SAME schema app.context.models.
    ContextDelta that build_next_context already validates a real LLM's output
    against, no new schema -- and merges it via the exact same deterministic
    app.context.engine.apply_context_delta run_pipeline itself uses. Never calls
    update_context or constructs any LLM provider.

    Idempotent on (thread_id, triggering_email_id), exactly like run_pipeline's own
    existing_snapshot check: calling this twice for the same message_id returns the
    already-persisted snapshot unchanged (already_persisted=True) rather than
    creating a second context_version.
    """
    context_repo = ContextSnapshotRepository(db)
    email_repo = EmailRepository(db)

    existing_snapshot = context_repo.find_one({"thread_id": thread_id, "triggering_email_id": message_id})
    if existing_snapshot:
        return {
            "thread_id": thread_id,
            "context_version": existing_snapshot["context_version"],
            "context": existing_snapshot["context"],
            "already_persisted": True,
        }

    previous_snapshot = context_repo.latest_for_thread(thread_id)
    previous_context = (
        ThreadContext.model_validate(previous_snapshot["context"]) if previous_snapshot else ThreadContext()
    )
    next_context = apply_context_delta(previous_context, delta, message_id)
    changes = diff_context(previous_context, next_context, source_email_id=message_id)
    next_version = (previous_snapshot["context_version"] + 1) if previous_snapshot else 1

    context_repo.upsert_by_key(
        {"thread_id": thread_id, "triggering_email_id": message_id},
        {
            "thread_id": thread_id,
            "context_version": next_version,
            "triggering_email_id": message_id,
            "context": next_context.model_dump(mode="json"),
            "changes_from_previous_context": [c.model_dump(mode="json") for c in changes],
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    email_repo.set_stage(message_id, ProcessingStage.CONTEXT_BUILT.value)

    return {
        "thread_id": thread_id,
        "context_version": next_version,
        "context": next_context.model_dump(mode="json"),
        "already_persisted": False,
    }


def create_reply_draft(db: Database, message_id: str, thread_id: str, subject: str, body: str) -> dict[str, Any]:
    """Deterministic MCP entry point: persists a reply a reasoning caller (e.g. Claude
    Desktop) already drafted -- this function never generates text itself, never
    calls draft_reply or any LLM, and never sends anything. Reuses the exact same
    never-overwrite guard run_pipeline's own reply-drafting stage already uses
    (find-before-write on source_email_id, also backed by a unique index -- see
    app/database/indexes.py).
    """
    reply_repo = ReplyDraftRepository(db)
    email_repo = EmailRepository(db)
    reply_key = {"source_email_id": message_id}

    existing = reply_repo.find_one(reply_key)
    if existing:
        return {**existing, "already_existed": True}

    # A reply draft stays connected to the sender's CANONICAL Person regardless of
    # display name, and regardless of whether that address historically belonged to
    # a Person since consolidated into another one (Phase 20.1) -- see
    # app.entities.resolution.resolve_canonical_person_for_email, not a raw lookup.
    stored_email = email_repo.find_one({"message_id": message_id})
    sender_person = (
        resolve_canonical_person_for_email(db, stored_email["from"]["email"])
        if stored_email and stored_email.get("from", {}).get("email")
        else None
    )

    draft = ReplyDraft(
        reply_id=f"reply_{message_id}",
        thread_id=thread_id,
        source_email_id=message_id,
        status="awaiting_approval",
        draft=ReplyDraftContent(subject=subject, body=body),
        person_id=sender_person["id"] if sender_person else None,
        org_id=sender_person.get("org_id") if sender_person else None,
        created_at=datetime.now(timezone.utc),
    )
    reply_repo.upsert_by_key(reply_key, draft.model_dump(mode="json"))
    email_repo.set_stage(message_id, ProcessingStage.REPLY_PROCESSED.value)

    return {**draft.model_dump(mode="json"), "already_existed": False}


def mark_email_completed(db: Database, message_id: str) -> dict[str, Any]:
    """Deterministic MCP entry point: the final step of the Claude-Desktop-driven
    ingestion path. Transitions an email to COMPLETED only after verifying real
    evidence that persist_email_analysis has actually run for it (entities_referenced
    is populated) -- never on a caller's say-so alone. This is what makes
    ingest_email's already_completed check meaningful for emails processed through
    this new path: without an explicit, verified completion step, an email persisted
    via these tools would never reach COMPLETED and would be reconsidered "new" on
    every future ingest_email call.
    """
    email_repo = EmailRepository(db)
    stored = email_repo.find_one({"message_id": message_id})
    if stored is None:
        raise ValueError(f"no email found for message_id={message_id!r}")

    if stored.get("processing_status", {}).get("stage") == ProcessingStage.COMPLETED.value:
        return {"message_id": message_id, "stage": "COMPLETED", "already_completed": True}

    if not stored.get("entities_referenced"):
        raise ValueError(
            f"message_id={message_id!r} has not completed entity/commitment/meeting "
            "persistence yet -- call persist_email_analysis before mark_email_completed"
        )

    email_repo.set_stage(message_id, ProcessingStage.COMPLETED.value)
    return {"message_id": message_id, "stage": "COMPLETED", "already_completed": False}


def list_processed_emails(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    thread_id_by_message_id: dict[str, str] = {}
    for thread in ThreadRepository(db).find_many({}):
        for message_id in thread["message_ids"]:
            thread_id_by_message_id[message_id] = thread["thread_id"]

    emails = sorted(
        EmailRepository(db).find_many({}), key=lambda e: e["timestamp"], reverse=True
    )[:limit]

    context_repo = ContextSnapshotRepository(db)
    summary_by_thread_id: dict[str, str | None] = {}

    results: list[dict[str, Any]] = []
    for email in emails:
        thread_id = thread_id_by_message_id.get(email["message_id"])
        if thread_id is not None and thread_id not in summary_by_thread_id:
            snapshot = context_repo.latest_for_thread(thread_id)
            summary_by_thread_id[thread_id] = snapshot["context"]["summary"] if snapshot else None

        results.append(
            {
                "message_id": email["message_id"],
                "thread_id": thread_id,
                "from": email["from"],
                "to": email["to"],
                "cc": email["cc"],
                "subject": email["subject"],
                "timestamp": email["timestamp"],
                "processing_status": _safe_processing_status(email),
                "summary": summary_by_thread_id.get(thread_id),
                "body_preview": email["body"][:_BODY_PREVIEW_LENGTH],
                "record_id": email.get("record_id"),
                "source_type": email.get("source_type"),
                "source_link": email.get("source_link"),
                "date": email.get("date"),
                "entities_referenced": email.get(
                    "entities_referenced",
                    {
                        "people": [],
                        "projects": [],
                        "commitments": [],
                        "follow_ups": [],
                        "meetings": [],
                        "personal": [],
                    },
                ),
                "goal_pillar": email.get("goal_pillar"),
                "label_applied": email.get("label_applied"),
                "priority": email.get("priority"),
                "confidence": email.get("confidence"),
            }
        )

    return results


def search_emails(
    db: Database,
    from_email: str | None = None,
    subject_contains: str | None = None,
    label_applied: str | None = None,
    priority: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only search over the `emails` collection. Never calls an LLM, never writes
    anything. Returns full email content (not the 150-char preview list_processed_emails
    uses), since downstream questions about a specific email's content need the real
    body.
    """
    limit = _clamp_limit(limit)
    query: dict[str, Any] = {}
    if from_email:
        query["from.email"] = from_email.strip().lower()
    if subject_contains:
        query["subject"] = {"$regex": re.escape(subject_contains), "$options": "i"}
    if label_applied:
        query["label_applied"] = label_applied
    if priority:
        query["priority"] = priority

    emails = sorted(
        EmailRepository(db).find_many(query), key=lambda e: e["timestamp"], reverse=True
    )[:limit]

    thread_index = _thread_id_index(db)

    return [
        {
            "message_id": e["message_id"],
            "thread_id": thread_index.get(e["message_id"], e.get("thread_id")),
            "from": e["from"],
            "to": e["to"],
            "cc": e["cc"],
            "subject": e["subject"],
            "timestamp": e["timestamp"],
            "labels": e.get("labels", []),
            "body": e["body"],
            "processing_status": _safe_processing_status(e),
            "label_applied": e.get("label_applied"),
            "priority": e.get("priority"),
        }
        for e in emails
    ]


def get_thread(db: Database, thread_id: str) -> dict[str, Any] | None:
    """Read-only retrieval of one full conversation. Returns None if the thread doesn't
    exist -- never fabricates one. `latest_context`/`context_version` are None when no
    context_snapshot exists for this thread (e.g. a raw-only thread that was never run
    through the AI pipeline) -- never invented.
    """
    thread = ThreadRepository(db).find_one({"thread_id": thread_id})
    if thread is None:
        return None

    emails = sorted(
        EmailRepository(db).find_many({"message_id": {"$in": thread["message_ids"]}}),
        key=lambda e: e["timestamp"],
    )
    latest_context = ContextSnapshotRepository(db).latest_for_thread(thread_id)

    return {
        "thread_id": thread_id,
        "normalized_subject": thread["normalized_subject"],
        "participant_emails": thread["participant_emails"],
        "message_count": len(emails),
        "last_message_at": thread["last_message_at"],
        "messages": [
            {
                "message_id": e["message_id"],
                "from": e["from"],
                "to": e["to"],
                "cc": e["cc"],
                "subject": e["subject"],
                "timestamp": e["timestamp"],
                "labels": e.get("labels", []),
                "body": e["body"],
                "processing_status": _safe_processing_status(e),
            }
            for e in emails
        ],
        "latest_context": latest_context["context"] if latest_context else None,
        "context_version": latest_context["context_version"] if latest_context else None,
    }


def list_people(
    db: Database,
    org: str | None = None,
    email: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `people` collection. Never creates or
    resolves a Person -- that only ever happens inside app.entities.resolution, which
    this tool never calls.
    """
    query: dict[str, Any] = {}
    if org:
        query["org"] = org
    if email:
        query["email"] = email.strip().lower()
    if thread_id:
        query["open_threads"] = thread_id

    return PersonRepository(db).find_many(query)[: _clamp_limit(limit)]


def list_projects(
    db: Database, entity: str | None = None, goal_pillar: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `projects` collection."""
    query: dict[str, Any] = {}
    if entity:
        query["entity"] = entity
    if goal_pillar:
        query["goal_pillar"] = goal_pillar

    return ProjectRepository(db).find_many(query)[: _clamp_limit(limit)]


def list_commitments(
    db: Database,
    thread_id: str | None = None,
    class_: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `commitments` collection. `class_` filters
    on the stored "class" key (mine/owed_to_me/theirs/recap) -- named class_ here only
    to avoid shadowing the Python keyword. Status is returned exactly as stored
    (Commitment.status, default "open"); this tool never infers or changes it.
    """
    query: dict[str, Any] = {}
    if thread_id:
        query["thread_id"] = thread_id
    if class_:
        query["class"] = class_
    if project_id:
        query["project_id"] = project_id

    return CommitmentRepository(db).find_many(query)[: _clamp_limit(limit)]


def list_follow_ups(
    db: Database,
    commitment_id: str | None = None,
    thread_id: str | None = None,
    escalation_level: int | None = None,
    audience: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `follow_ups` collection. All fields
    (including escalation_level, audience, status, follow_up_earliest_at/latest_at) are
    returned exactly as stored -- none is fabricated here."""
    query: dict[str, Any] = {}
    if commitment_id:
        query["commitment_id"] = commitment_id
    if thread_id:
        query["thread_id"] = thread_id
    if escalation_level is not None:
        query["escalation_level"] = escalation_level
    if audience:
        query["audience"] = audience
    if status:
        query["status"] = status

    return FollowUpRepository(db).find_many(query)[: _clamp_limit(limit)]


def list_meetings(
    db: Database, thread_id: str | None = None, actionable: bool | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `meetings` collection. Never creates a
    calendar event and never calls a calendar provider/MCP.
    """
    query: dict[str, Any] = {}
    if thread_id:
        query["thread_id"] = thread_id
    if actionable is not None:
        query["actionable"] = actionable

    return MeetingRepository(db).find_many(query)[: _clamp_limit(limit)]


def list_reply_drafts(
    db: Database,
    thread_id: str | None = None,
    source_email_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `reply_drafts` collection. Optional filters
    (all exact match): thread_id, source_email_id, status. Returns each draft exactly
    as stored -- reply_id, thread_id, source_email_id, status, draft {subject, body},
    created_by, created_at, approved_by, sent_at -- so provenance and persistence can
    be verified. created_at is absent on any draft written before that field existed;
    never fabricated here. Read-only: never sends, approves, edits, or creates a draft.
    """
    query: dict[str, Any] = {}
    if thread_id:
        query["thread_id"] = thread_id
    if source_email_id:
        query["source_email_id"] = source_email_id
    if status:
        query["status"] = status

    return ReplyDraftRepository(db).find_many(query)[: _clamp_limit(limit)]


def get_reply_draft(db: Database, reply_id: str) -> dict[str, Any] | None:
    """Read-only retrieval of one reply draft by its reply_id. Returns None if it
    doesn't exist -- never fabricates one. Read-only: never sends, approves, edits, or
    creates a draft.
    """
    return ReplyDraftRepository(db).find_one({"reply_id": reply_id})


def get_project_summary(db: Database, project_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary, assembled with plain Mongo queries in
    application code -- no LLM involved. Returns None if the project doesn't exist.

    Relationship honesty: Commitment.project_id -> Project.id and
    FollowUp.commitment_id -> Commitment.id are the ONLY fields that actually link
    anything to a Project in the current schema. Meeting has no project reference
    (only a free-text `project_or_pillar` string, which is not a reliable id match and
    is deliberately NOT used here to avoid pretending a relationship exists).
    KnowledgeItem and Person have no project reference at all. Those three are always
    returned empty, with an explanation in `relationship_notes`, never fabricated.
    """
    project = ProjectRepository(db).find_one({"id": project_id})
    if project is None:
        return None

    related_commitments = CommitmentRepository(db).find_many({"project_id": project_id})
    commitment_ids = [c["id"] for c in related_commitments]
    related_follow_ups = (
        FollowUpRepository(db).find_many({"commitment_id": {"$in": commitment_ids}})
        if commitment_ids
        else []
    )

    return {
        "project": project,
        "related_commitments": related_commitments,
        "related_follow_ups": related_follow_ups,
        "related_meetings": [],
        "related_knowledge_items": [],
        "related_people": [],
        "relationship_notes": {
            "related_commitments": "direct: Commitment.project_id == Project.id",
            "related_follow_ups": "direct: FollowUp.commitment_id references a commitment already linked to this project",
            "related_meetings": "NOT SUPPORTED by the current schema: Meeting has no project_id field",
            "related_knowledge_items": "NOT SUPPORTED by the current schema: KnowledgeItem has no project reference",
            "related_people": "NOT SUPPORTED by the current schema: Project has no direct people reference",
        },
    }


def get_company_summary(db: Database, org: str) -> dict[str, Any]:
    """Read-only cross-collection summary for an organization/company name, assembled
    with plain Mongo queries in application code -- no LLM involved.

    Relationship honesty: Person.org == org and Project.entity == org are direct
    stored-field matches. Commitments/follow-ups are only reachable indirectly, through
    a project whose entity matches -- and in the current pipeline, Commitment.project_id
    is not actually populated (app/pipeline.py always passes project_id=None to
    resolve_commitment), so this indirect path is usually empty today even though the
    query itself is correct. Meeting and KnowledgeItem have no org/company reference of
    any kind and are always returned empty.
    """
    people = PersonRepository(db).find_many({"org": org})
    projects = ProjectRepository(db).find_many({"entity": org})

    project_ids = [p["id"] for p in projects]
    related_commitments = (
        CommitmentRepository(db).find_many({"project_id": {"$in": project_ids}})
        if project_ids
        else []
    )
    commitment_ids = [c["id"] for c in related_commitments]
    related_follow_ups = (
        FollowUpRepository(db).find_many({"commitment_id": {"$in": commitment_ids}})
        if commitment_ids
        else []
    )

    return {
        "org": org,
        "people": people,
        "projects": projects,
        "related_commitments": related_commitments,
        "related_follow_ups": related_follow_ups,
        "related_meetings": [],
        "related_knowledge_items": [],
        "relationship_notes": {
            "people": "direct: Person.org == org",
            "projects": "direct: Project.entity == org",
            "related_commitments": (
                "indirect: Commitment.project_id references a Project whose entity == org "
                "-- in practice usually empty, since the pipeline does not currently "
                "populate Commitment.project_id"
            ),
            "related_follow_ups": "indirect: derived from related_commitments above",
            "related_meetings": "NOT SUPPORTED by the current schema: Meeting has no org/company reference",
            "related_knowledge_items": "NOT SUPPORTED by the current schema: KnowledgeItem has no org/company reference",
        },
    }


def lookup_knowledge(
    knowledge_dir: str,
    entity_id: str | None = None,
    query: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Read-only traversal over the Markdown knowledge projection (data/knowledge/,
    written by app.knowledge_projector) -- a different backing store than every other
    tool in this module, which read MongoDB directly. Never touches MongoDB, never
    writes to Markdown. Exactly one of entity_id/query must be given.

    entity_id: a canonical id (PER-/PRJ-/COM-/FU-/MTG-/PSN-/ORG-), a message_id, or a
    thread_id -- looked up directly, then its neighbors traversed up to `depth` hops
    (bounded, cycle-safe).

    query: a name/email/project-name search (deterministic: exact id, exact email,
    exact normalized name, then safe substring -- never fuzzy, never an LLM, never a
    vector search). Every match's neighbors are traversed up to `depth` hops for the
    first (best) match only.

    Every relationship in the result carries its basis ("direct" or
    "derived" -- a derived/co-occurrence edge is never presented as if it were a
    direct one) and its provenance (source email/thread ids, or explicitly marked
    unavailable when the underlying Markdown has no per-edge provenance to offer).
    """
    if not entity_id and not query:
        raise ValueError("lookup_knowledge requires either entity_id or query")

    lookup = KnowledgeLookup(knowledge_dir)

    if entity_id:
        entity = lookup.find_entity(entity_id)
        if entity is None:
            return {"found": False, "entity_id": entity_id, "entity": None, "neighbors": None}
        return {
            "found": True,
            "entity_id": entity_id,
            "entity": entity,
            "neighbors": lookup.get_neighbors(entity_id, depth=depth),
        }

    matches = lookup.find_people(query) + lookup.find_projects(query)
    if not matches:
        return {"found": False, "query": query, "matches": [], "neighbors": None}
    primary = matches[0]
    return {
        "found": True,
        "query": query,
        "matches": [{"id": m["id"], "type": m["type"], "entity": m["entity"]} for m in matches],
        "primary_match": primary["id"],
        "neighbors": lookup.get_neighbors(primary["id"], depth=depth),
    }
