"""Phase 22A: typed contracts for the CoS Query & Retrieval Layer.

MongoDB is the source of truth (app.query.retrieval reads it); nothing here is ever
constructed from an LLM's free-text opinion of what a record contains. An LLM (or the
deterministic classifier in app.query.intent) may only ever produce a QueryRequest/
ParsedIntent -- never a QueryResult or an EvidenceItem, both of which are only ever
built from real, retrieved documents (see app.query.retrieval/app.query.evidence).
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class QueryIntentType(StrEnum):
    MEETINGS = "meetings"
    PERSON_CONTEXT = "person_context"
    ORGANIZATION_CONTEXT = "organization_context"
    PROJECT_CONTEXT = "project_context"
    COMMITMENTS = "commitments"
    FOLLOW_UPS = "follow_ups"
    EMAIL_ACTIVITY = "email_activity"
    THREAD_CONTEXT = "thread_context"
    KNOWLEDGE = "knowledge"
    REPLY_DRAFTS = "reply_drafts"
    CROSS_ENTITY_ACTIVITY = "cross_entity_activity"
    MEETING_PREPARATION = "meeting_preparation"
    UNSUPPORTED = "unsupported"


class IdentityBasis(StrEnum):
    """Mirrors app.entities.context's existing basis vocabulary (CANONICAL is split
    into canonical_person_id/canonical_org_id there; unified here for the query
    layer's evidence model) -- never invented independently of it."""

    CANONICAL = "canonical"
    LEGACY_THREAD_SCOPED = "legacy_thread_scoped"
    INFERRED = "inferred"
    AMBIGUOUS = "ambiguous"
    UNRESOLVED = "unresolved"


class ResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    LIFECYCLE_ERROR = "lifecycle_error"


class OwnershipDirection(StrEnum):
    """Phase 24.1: maps directly to the existing, already-stored
    Commitment.commitment_class values -- never a new state machine.
    'mine' -> USER_OWES, 'owed_to_me' -> OTHER_PERSON_OWES. 'theirs'/'recap' are
    neither direction and are excluded from both ownership-scoped queries."""

    USER_OWES = "user_owes"
    OTHER_PERSON_OWES = "other_owes"


class RelationshipKind(StrEnum):
    """Phase 24.6: distinguishes a directly stored foreign key from a same-
    thread/same-person coincidence -- never presented as equivalent."""

    DIRECT = "direct"
    INDIRECT_VIA_THREAD = "indirect_via_thread"


class MeetingClassification(StrEnum):
    """Phase 23.2: SALES/FINANCE/PROSPECT/PROJECT are part of the vocabulary for
    future extensibility but are NEVER assigned by app.query.meetings today -- no
    stored field reliably supports them (Meeting.project_or_pillar is declared but
    never populated by the live pipeline). Only INTERNAL/CUSTOMER are ever actually
    derived (from attendee email domains vs. the configured agent_email domain,
    both real stored/configured values), and UNKNOWN otherwise. Never guessed from
    a title or free text.
    """

    INTERNAL = "internal"
    CUSTOMER = "customer"
    SALES = "sales"
    FINANCE = "finance"
    PROSPECT = "prospect"
    PROJECT = "project"
    UNKNOWN = "unknown"


class DateRangeKind(StrEnum):
    TODAY = "today"
    TOMORROW = "tomorrow"
    YESTERDAY = "yesterday"
    THIS_WEEK = "this_week"
    NEXT_WEEK = "next_week"
    LAST_WEEK = "last_week"
    THIS_MONTH = "this_month"
    NEXT_MONTH = "next_month"
    BETWEEN = "between"
    BEFORE = "before"
    AFTER = "after"
    UPCOMING = "upcoming"
    OVERDUE = "overdue"
    RECENT = "recent"
    LATEST = "latest"
    ALL_TIME = "all_time"


class DateRange(BaseModel):
    """A resolved, concrete [start, end) window -- always computed from an explicit
    reference_datetime (see app.query.dates.resolve_date_range), never from
    datetime.now() directly, so query resolution stays deterministic and testable.
    start/end are None for open-ended kinds (UPCOMING has no end; OVERDUE/BEFORE has
    no start; ALL_TIME has neither)."""

    kind: DateRangeKind
    start: datetime | None = None
    end: datetime | None = None
    reference_datetime: datetime
    timezone: str


class EntityCandidate(BaseModel):
    """One possible match surfaced during an ambiguous name resolution -- enough
    information for a caller (human or a future clarification turn) to disambiguate,
    never enough to silently pick one."""

    id: str
    name: str
    email: str | None = None
    org: str | None = None


class EntityReference(BaseModel):
    """The result of resolving a person/organization mention -- never constructed by
    guessing; see app.query.entity_resolution. A MERGED Person is never returned as
    `resolved_id` -- resolution always follows resolve_canonical_person_id first."""

    kind: Literal["person", "organization", "project"]
    raw_text: str | None = None
    email_hint: str | None = None
    resolution_status: ResolutionStatus
    resolved_id: str | None = None
    candidates: list[EntityCandidate] = Field(default_factory=list)
    lifecycle_error: str | None = None


class QueryFilters(BaseModel):
    date_range: DateRange | None = None
    person_ref: EntityReference | None = None
    org_ref: EntityReference | None = None
    project_ref: EntityReference | None = None
    status: str | None = None
    limit: int = 50
    # Phase 22B.4: explicit ID lookups a caller (UI/MCP) may already have in hand --
    # never extracted from free text. When set, these bypass name-based resolution
    # entirely for MEETING_PREPARATION / THREAD_CONTEXT.
    meeting_id: str | None = None
    thread_id: str | None = None
    # Phase 24.1: explicit ownership direction for a COMMITMENTS query -- never
    # guessed from wording; set only by a caller, or by app.query.intent's
    # deterministic fixed-phrase keyword rules (the same pattern already used for
    # date_range_kind), never inferred from arbitrary text.
    ownership: OwnershipDirection | None = None


class QueryRequest(BaseModel):
    """The single entry point into the query layer -- app.query.service.execute_query
    accepts exactly this and nothing else. `text` is the raw natural-language
    question; reference_datetime/timezone make relative-date resolution
    deterministic and test-injectable (Section F)."""

    text: str
    reference_datetime: datetime
    timezone: str = "UTC"
    filters: QueryFilters = Field(default_factory=QueryFilters)


class ParsedIntent(BaseModel):
    """Stage-1 (deterministic) or Stage-2 (LLM, strictly validated against this same
    schema) classification output -- see app.query.intent. Never itself a MongoDB
    query; app.query.retrieval's trusted builders are the only code that turns this
    into a filter."""

    intent: QueryIntentType
    person_text: str | None = None
    org_text: str | None = None
    project_text: str | None = None
    date_range_kind: DateRangeKind | None = None
    ownership_hint: OwnershipDirection | None = None
    confidence: float = 1.0


class EvidenceItem(BaseModel):
    """One structured, auditable record backing a synthesized answer. Never
    constructed from LLM output -- only from a document app.query.retrieval actually
    read out of MongoDB."""

    collection: str
    record_id: str
    basis: IdentityBasis
    thread_id: str | None = None
    person_id: str | None = None
    org_id: str | None = None
    project_id: str | None = None
    source_message_id: str | None = None
    timestamp: datetime | None = None
    summary_fields: dict[str, Any] = Field(default_factory=dict)


class Ambiguity(BaseModel):
    field: Literal["person", "organization", "project"]
    raw_text: str | None = None
    candidates: list[EntityCandidate]


class QueryResultStatus(StrEnum):
    OK = "ok"
    NO_MATCH = "no_match"
    DATA_INCOMPLETE = "data_incomplete"
    AMBIGUOUS = "ambiguous"
    ERROR = "error"


class QueryMetadata(BaseModel):
    query_id: str
    intent: QueryIntentType
    resolution_status: ResolutionStatus | None = None
    collections_accessed: list[str] = Field(default_factory=list)
    result_count: int = 0
    evidence_count: int = 0
    ambiguity_count: int = 0
    canonical_resolution_used: bool = False
    execution_time_ms: float | None = None


class QueryResult(BaseModel):
    """The retrieval layer's complete, structured output -- an LLM synthesizer (see
    app.query.synthesis) may only ever read from this; it is never given raw MongoDB
    access or a free-form prompt to "look things up" itself."""

    status: QueryResultStatus
    intent: QueryIntentType
    records: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    message: str | None = None  # e.g. "the available data does not establish this"
    metadata: QueryMetadata


class AnswerContext(BaseModel):
    query_result: QueryResult
    synthesized_text: str | None = None


class MeetingBrief(BaseModel):
    """Phase 23.1: a fully evidence-backed meeting-preparation package. Every field
    is either a real stored record or an explicitly-labeled derived/absent value --
    never fabricated. attendee_resolution_notes records any attendee whose canonical
    chain could not be resolved (broken/circular merged_into), so a partial brief is
    still returned rather than the whole brief failing.
    """

    meeting: dict[str, Any]
    classification: MeetingClassification
    canonical_attendee_ids: list[str] = Field(default_factory=list)
    attendee_contexts: list[dict[str, Any]] = Field(default_factory=list)
    attendee_resolution_notes: list[str] = Field(default_factory=list)
    organization_context: dict[str, Any] | None = None
    project_context: dict[str, Any] | None = None
    thread_context: dict[str, Any] | None = None
    previous_meetings: list[dict[str, Any]] = Field(default_factory=list)
    open_commitments: list[dict[str, Any]] = Field(default_factory=list)
    relevant_follow_ups: list[dict[str, Any]] = Field(default_factory=list)
    relevant_knowledge: list[dict[str, Any]] = Field(default_factory=list)
    existing_reply_drafts: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)
