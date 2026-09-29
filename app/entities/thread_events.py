"""Thread Events -- an append-only, descriptive audit trail of what the existing
pipeline actually did while processing each email. This module NEVER performs
entity resolution or mutation itself:

    existing operation -> existing repository/service performs it -> record_event
    describes what just happened

It is not a second pipeline, not an event processor, and never replays events to
mutate anything. Every call site passes in an operation result the caller ALREADY
obtained from a real resolver (e.g. app.entities.resolution.resolve_person_with_operation)
-- this module never infers "created" vs "reused" by diffing database state.

Canonical ids only: thread_id/email_id/entity_id are always EML-nnn/THR-nnn/
PER-nnn/ORG-nnn/PRJ-nnn/OPP-nnn/CMT-nnn/FUP-nnn/MTG-nnn/PSN-nnn/a knowledge_id --
never a raw Gmail message/thread id.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo.database import Database

from app.database.repositories import CounterRepository, ThreadEventRepository
from app.entities.ids import next_id

EventType = Literal[
    "email_received",
    "entity_created", "entity_reused", "entity_updated", "entity_linked",
    "context_enriched",
    "knowledge_created", "knowledge_updated",
    "commitment_created", "commitment_reused",
    "meeting_created", "meeting_reused",
    "follow_up_created",
    "project_updated",
    "opportunity_created", "opportunity_reused",
    "person_context_enriched",
    "processing_failed",
]
EntityType = Literal[
    "email", "person", "organization", "project", "opportunity",
    "commitment", "meeting", "follow_up", "personal_item", "knowledge",
    "person_context", "pipeline", "thread_context",
]
Operation = Literal["created", "reused", "updated", "linked", "enriched", "failed"]


class ThreadEvent(BaseModel):
    id: str
    thread_id: str
    email_id: str | None = None
    sequence: int
    event_type: EventType
    entity_type: EntityType
    entity_id: str | None = None
    operation: Operation
    summary: str
    # Thread Events describe what the deterministic pipeline directly did -- always
    # "observed". Same 3-value vocabulary as app.entities.person_context.
    # PersonContextProvenance (never a competing one), not imported directly only
    # to avoid a circular import between the two modules -- the other two values
    # there, "reconstructed"/"inferred", describe LLM-facing interpretation, which
    # has no equivalent here.
    provenance: Literal["observed", "reconstructed", "inferred"] = "observed"
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)


def record_event(
    db: Database, thread_id: str, email_id: str | None, event_type: EventType, entity_type: EntityType,
    entity_id: str | None, operation: Operation, summary: str, metadata: dict[str, Any] | None = None,
) -> ThreadEvent:
    """Idempotent on (thread_id, email_id, event_type, entity_type, entity_id) --
    see app.database.indexes for the unique index this relies on. A retried email
    that re-observes the exact same logical operation upserts over the same
    document (keeping its original id/sequence/created_at); a genuinely later
    email produces a new event with a new sequence, never mutating an earlier
    email's own event. Never raises for a duplicate -- upsert_by_key handles it.
    """
    repo = ThreadEventRepository(db)
    key = {
        "thread_id": thread_id, "email_id": email_id, "event_type": event_type,
        "entity_type": entity_type, "entity_id": entity_id,
    }
    existing = repo.find_one(key)
    if existing:
        event_id = existing["id"]
        sequence = existing["sequence"]
        created_at = datetime.fromisoformat(existing["created_at"])
    else:
        event_id = next_id(db, "TEV-")
        # Per-thread monotonic sequence, reusing the exact same atomic
        # find_one_and_update counter mechanism as EML-/THR-/PER-/etc (see
        # app.entities.ids.next_id / CounterRepository.increment_and_get) --
        # just keyed per-thread instead of globally, so concurrent processing of
        # two different threads never contends on the same counter document, and
        # concurrent processing of the SAME thread can never produce a duplicate
        # sequence value (increment_and_get is atomic).
        sequence = CounterRepository(db).increment_and_get(f"TEV_SEQ:{thread_id}")
        created_at = datetime.now(timezone.utc)

    event = ThreadEvent(
        id=event_id, thread_id=thread_id, email_id=email_id, sequence=sequence,
        event_type=event_type, entity_type=entity_type, entity_id=entity_id,
        operation=operation, summary=summary, created_at=created_at, metadata=metadata or {},
    )
    repo.upsert_by_key(key, event.model_dump(mode="json"))
    return event


def try_record_event(
    db: Database, thread_id: str, email_id: str | None, event_type: EventType, entity_type: EntityType,
    entity_id: str | None, operation: Operation, summary: str, metadata: dict[str, Any] | None = None,
) -> ThreadEvent | None:
    """Same as record_event, but never raises -- Phase 6's explicit requirement
    that Thread Event persistence must never break core email processing. Any
    failure here is swallowed; the caller (app.pipeline) proceeds exactly as if
    no event had been requested. Returns None on failure."""
    try:
        return record_event(db, thread_id, email_id, event_type, entity_type, entity_id, operation, summary, metadata)
    except Exception:  # noqa: BLE001 -- audit logging must never crash the real pipeline
        return None


def get_thread_event_trail(db: Database, thread_id: str) -> list[dict[str, Any]]:
    """Deterministic, ordered (sequence ASC) reconstruction of everything Thread
    Events recorded for one thread -- no LLM summarization, no inference beyond
    what was already recorded verbatim by record_event calls."""
    return ThreadEventRepository(db).for_thread(thread_id)
