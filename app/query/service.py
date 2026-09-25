"""Phase 22A: the query-layer orchestrator.

execute_query is the ONLY function that ties classification -> entity resolution ->
retrieval -> evidence assembly together, following exactly the architecture Section A
requires:

    USER QUESTION -> QUERY UNDERSTANDING -> CANONICAL ENTITY RESOLUTION ->
    STRUCTURED RETRIEVAL -> CROSS-COLLECTION CONTEXT ASSEMBLY -> EVIDENCE -> QueryResult

It deliberately stops BEFORE LLM synthesis (see app.query.synthesis) -- this function
never calls an LLM and needs none to be fully exercised in tests.

Read-only, always: every branch below either returns early (NO_MATCH/AMBIGUOUS/ERROR)
or calls into app.query.retrieval, which only ever reads. Nothing here can create a
Person, Organization, Commitment, FollowUp, Meeting, ReplyDraft, or CalendarAction.
"""

import time
import uuid
from typing import Any

from pymongo.database import Database

from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.entity_resolution import resolve_organization_reference, resolve_person_reference, resolve_project_reference
from app.query.evidence import build_evidence_from_context_items, build_evidence_items
from app.query.intent import classify_intent_with_fallback
from app.query.meetings import get_meeting_brief
from app.query.schemas import (
    Ambiguity,
    DateRange,
    EntityReference,
    IdentityBasis,
    OwnershipDirection,
    QueryFilters,
    QueryIntentType,
    QueryMetadata,
    QueryRequest,
    QueryResult,
    QueryResultStatus,
    ResolutionStatus,
)

_OWNERSHIP_TO_CLASS = {OwnershipDirection.USER_OWES: "mine", OwnershipDirection.OTHER_PERSON_OWES: "owed_to_me"}

_PERSON_ORG_REQUIRED_INTENTS = {
    QueryIntentType.PERSON_CONTEXT, QueryIntentType.ORGANIZATION_CONTEXT,
}


def _resolve_date_range(request: QueryRequest, kind) -> DateRange | None:
    if request.filters.date_range is not None:
        return request.filters.date_range
    if kind is None:
        return None
    return resolve_date_range(kind, request.reference_datetime, request.timezone)


def _resolve_entity(
    db: Database, existing: EntityReference | None, raw_text: str | None,
    resolver,
) -> EntityReference | None:
    """Prefers an already-resolved reference the caller supplied directly (Section
    S/pipeline precedent: never re-resolve what's already known) over resolving
    fresh text extracted by the Stage-1 classifier.
    """
    if existing is not None:
        return existing
    if raw_text is None:
        return None
    return resolver(db, raw_text=raw_text)


def _ambiguous_result(intent: QueryIntentType, field: str, ref: EntityReference, query_id: str, started: float) -> QueryResult:
    return QueryResult(
        status=QueryResultStatus.AMBIGUOUS, intent=intent,
        ambiguities=[Ambiguity(field=field, raw_text=ref.raw_text, candidates=ref.candidates)],
        message=f"More than one {field} matches {ref.raw_text!r} -- clarify which one before answering.",
        metadata=QueryMetadata(
            query_id=query_id, intent=intent, resolution_status=ResolutionStatus.AMBIGUOUS,
            ambiguity_count=1, canonical_resolution_used=True, execution_time_ms=(time.perf_counter() - started) * 1000,
        ),
    )


def _not_found_result(intent: QueryIntentType, field: str, ref: EntityReference, query_id: str, started: float) -> QueryResult:
    return QueryResult(
        status=QueryResultStatus.NO_MATCH, intent=intent,
        message=f"No {field} matching {ref.raw_text!r} was found in the available data.",
        metadata=QueryMetadata(
            query_id=query_id, intent=intent, resolution_status=ResolutionStatus.NOT_FOUND,
            canonical_resolution_used=True, execution_time_ms=(time.perf_counter() - started) * 1000,
        ),
    )


def _lifecycle_error_result(intent: QueryIntentType, ref: EntityReference, query_id: str, started: float) -> QueryResult:
    return QueryResult(
        status=QueryResultStatus.ERROR, intent=intent,
        message=f"The stored identity chain for this reference is broken: {ref.lifecycle_error}",
        metadata=QueryMetadata(
            query_id=query_id, intent=intent, resolution_status=ResolutionStatus.LIFECYCLE_ERROR,
            canonical_resolution_used=True, execution_time_ms=(time.perf_counter() - started) * 1000,
        ),
    )


def _handle_resolution(intent: QueryIntentType, ref: EntityReference | None, field: str, query_id: str, started: float) -> QueryResult | None:
    """Returns an early QueryResult if `ref` isn't cleanly RESOLVED (or is None,
    for an intent that requires it), else None to let the caller continue with
    ref.resolved_id.
    """
    if ref is None:
        return QueryResult(
            status=QueryResultStatus.DATA_INCOMPLETE, intent=intent,
            message=f"This question needs a {field} to answer, and none was specified or resolvable from the text.",
            metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
        )
    if ref.resolution_status == ResolutionStatus.AMBIGUOUS:
        return _ambiguous_result(intent, field, ref, query_id, started)
    if ref.resolution_status == ResolutionStatus.NOT_FOUND:
        return _not_found_result(intent, field, ref, query_id, started)
    if ref.resolution_status == ResolutionStatus.LIFECYCLE_ERROR:
        return _lifecycle_error_result(intent, ref, query_id, started)
    return None


def execute_query(
    db: Database, request: QueryRequest, agent_email: str | None = None, llm_classify=None,
) -> QueryResult:  # noqa: PLR0911, PLR0912 -- one dispatch per intent, kept flat and explicit rather than fragmented further
    """agent_email (optional) is used only for app.query.meetings.classify_meeting's
    INTERNAL/CUSTOMER derivation -- omitting it simply leaves that classification
    UNKNOWN, never a guess. llm_classify (optional, Phase 22B.2) is a plain callable
    invoked ONLY when Stage-1 deterministic classification returns UNSUPPORTED; see
    app.query.intent.classify_intent_with_fallback for its exact contract and safety
    guarantees. Neither parameter changes read-only behavior in any way.
    """
    started = time.perf_counter()
    query_id = f"qry_{uuid.uuid4().hex[:12]}"
    parsed = classify_intent_with_fallback(request.text, llm_classify=llm_classify)
    intent = parsed.intent

    if intent == QueryIntentType.UNSUPPORTED:
        return QueryResult(
            status=QueryResultStatus.ERROR, intent=intent,
            message="This question's intent is not recognized by the current query layer.",
            metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
        )

    date_range = _resolve_date_range(request, parsed.date_range_kind)
    person_ref = _resolve_entity(db, request.filters.person_ref, parsed.person_text, resolve_person_reference)
    org_ref = _resolve_entity(db, request.filters.org_ref, parsed.org_text, resolve_organization_reference)
    project_ref = _resolve_entity(db, request.filters.project_ref, parsed.project_text, resolve_project_reference)
    limit = request.filters.limit

    if intent == QueryIntentType.PERSON_CONTEXT:
        early = _handle_resolution(intent, person_ref, "person", query_id, started)
        if early:
            return early
        context = retrieval.retrieve_person_context(db, person_ref.resolved_id)
        return _context_result(intent, context, "person", query_id, started, person_ref.resolved_id)

    if intent == QueryIntentType.ORGANIZATION_CONTEXT:
        early = _handle_resolution(intent, org_ref, "organization", query_id, started)
        if early:
            return early
        context = retrieval.retrieve_organization_context(db, org_ref.resolved_id)
        return _context_result(intent, context, "organization", query_id, started, org_ref.resolved_id)

    if intent == QueryIntentType.PROJECT_CONTEXT:
        early = _handle_resolution(intent, project_ref, "project", query_id, started)
        if early:
            return early
        context = retrieval.retrieve_project_context(db, project_ref.resolved_id)
        return _context_result(intent, context, "project", query_id, started, project_ref.resolved_id)

    if intent == QueryIntentType.MEETINGS:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        records = retrieval.retrieve_meetings(db, person_id=person_id, org_id=org_id, date_range=date_range, limit=limit)
        evidence = build_evidence_items("meetings", records, "id", ["date", "attendees", "actionable"], timestamp_field="date")
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["meetings"])

    if intent == QueryIntentType.COMMITMENTS:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        # Phase 24.1: a caller-supplied filter always wins over the deterministic
        # fixed-phrase hint (same "explicit beats extracted" precedent as person_ref).
        ownership = request.filters.ownership or parsed.ownership_hint
        commitment_class = _OWNERSHIP_TO_CLASS.get(ownership) if ownership else None
        records = retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, commitment_class=commitment_class, date_range=date_range, limit=limit)
        evidence = build_evidence_items("commitments", records, "id", ["what", "class", "status", "owed_by", "owed_to"], timestamp_field="made_on")
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["commitments"])

    if intent == QueryIntentType.FOLLOW_UPS:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        records = retrieval.retrieve_follow_ups(db, person_id=person_id, org_id=org_id, date_range=date_range, limit=limit)
        evidence = build_evidence_items("follow_ups", records, "id", ["commitment_id", "thread_id"])
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["follow_ups", "commitments"])

    if intent == QueryIntentType.KNOWLEDGE:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        records = retrieval.retrieve_knowledge(db, person_id=person_id, org_id=org_id, limit=limit)
        evidence = build_evidence_items("knowledge_items", records, "knowledge_id", ["subject_key", "predicate", "current_value", "basis", "confidence"], timestamp_field="last_confirmed_at")
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["knowledge_items"])

    if intent == QueryIntentType.REPLY_DRAFTS:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        records = retrieval.retrieve_reply_drafts(db, person_id=person_id, org_id=org_id, status=request.filters.status, limit=limit)
        evidence = build_evidence_items("reply_drafts", records, "reply_id", ["status", "draft"], timestamp_field="created_at")
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["reply_drafts"])

    if intent == QueryIntentType.THREAD_CONTEXT:
        # Phase 22B.4: an explicit thread_id (a caller/UI already has one) always
        # wins over the text-driven "latest thread" fallback -- never re-derived
        # from free text when an id is already known.
        if request.filters.thread_id:
            context = retrieval.retrieve_thread_context(db, request.filters.thread_id)
        else:
            context = retrieval.retrieve_latest_thread(db, person_id=person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None)
        if context is None:
            return QueryResult(
                status=QueryResultStatus.NO_MATCH, intent=intent, message="No thread data is available for this request.",
                metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
            )
        evidence = build_evidence_items("threads", [context["thread"]], "thread_id", ["normalized_subject", "last_message_at"])
        return QueryResult(
            status=QueryResultStatus.OK, intent=intent, records=[context], evidence=evidence,
            metadata=QueryMetadata(
                query_id=query_id, intent=intent, resolution_status=ResolutionStatus.RESOLVED,
                collections_accessed=["threads", "commitments", "meetings", "knowledge_items", "reply_drafts", "calendar_actions"],
                result_count=1, evidence_count=len(evidence),
                canonical_resolution_used=bool(person_ref), execution_time_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    if intent == QueryIntentType.MEETING_PREPARATION:
        # Phase 22B.4: meeting_id is not reliably text-extractable -- a caller/UI
        # must supply it directly via filters.
        if not request.filters.meeting_id:
            return QueryResult(
                status=QueryResultStatus.DATA_INCOMPLETE, intent=intent,
                message="Meeting preparation requires a specific meeting_id, which must be supplied directly (not extracted from free text).",
                metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
            )
        brief = get_meeting_brief(db, request.filters.meeting_id, agent_email=agent_email)
        if brief is None:
            return QueryResult(
                status=QueryResultStatus.NO_MATCH, intent=intent,
                message=f"No meeting with id {request.filters.meeting_id!r} was found in the available data.",
                metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
            )
        return QueryResult(
            status=QueryResultStatus.OK, intent=intent, records=[brief.model_dump(mode="json")], evidence=brief.evidence,
            metadata=QueryMetadata(
                query_id=query_id, intent=intent, resolution_status=ResolutionStatus.RESOLVED,
                collections_accessed=["meetings", "people", "organizations", "threads", "commitments", "follow_ups", "knowledge_items", "reply_drafts"],
                result_count=1, evidence_count=len(brief.evidence), canonical_resolution_used=True,
                execution_time_ms=(time.perf_counter() - started) * 1000,
            ),
        )

    if intent == QueryIntentType.EMAIL_ACTIVITY:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        thread_id = request.filters.thread_id
        records = retrieval.retrieve_email_activity(db, person_id=person_id, org_id=org_id, thread_id=thread_id, date_range=date_range, limit=limit)
        evidence = build_evidence_items("emails", records, "message_id", ["subject", "label_applied"], timestamp_field="timestamp")
        return _list_result(intent, records, evidence, query_id, started, bool(person_id or org_id), ["emails"])

    if intent == QueryIntentType.CROSS_ENTITY_ACTIVITY:
        person_id = person_ref.resolved_id if person_ref and person_ref.resolution_status == ResolutionStatus.RESOLVED else None
        org_id = org_ref.resolved_id if org_ref and org_ref.resolution_status == ResolutionStatus.RESOLVED else None
        if not person_id and not org_id:
            return QueryResult(
                status=QueryResultStatus.DATA_INCOMPLETE, intent=intent,
                message="An activity summary needs a person or organization to scope to.",
                metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
            )
        commitments = retrieval.retrieve_commitments(db, person_id=person_id, org_id=org_id, date_range=date_range, limit=limit)
        meetings = retrieval.retrieve_meetings(db, person_id=person_id, org_id=org_id, date_range=date_range, limit=limit)
        evidence = (
            build_evidence_items("commitments", commitments, "id", ["what", "class"], timestamp_field="made_on")
            + build_evidence_items("meetings", meetings, "id", ["date"], timestamp_field="date")
        )
        records: list[dict[str, Any]] = [*commitments, *meetings]
        return _list_result(intent, records, evidence, query_id, started, True, ["commitments", "meetings"])

    return QueryResult(
        status=QueryResultStatus.DATA_INCOMPLETE, intent=intent,
        message=f"{intent.value} is not yet implemented by the Phase 22A retrieval layer.",
        metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
    )


def _list_result(
    intent: QueryIntentType, records: list[dict[str, Any]], evidence, query_id: str, started: float,
    canonical_used: bool, collections: list[str],
) -> QueryResult:
    status = QueryResultStatus.OK if records else QueryResultStatus.NO_MATCH
    message = None if records else "No matching records were found in the available data for this request."
    return QueryResult(
        status=status, intent=intent, records=records, evidence=evidence, message=message,
        metadata=QueryMetadata(
            query_id=query_id, intent=intent, resolution_status=ResolutionStatus.RESOLVED if canonical_used else None,
            collections_accessed=collections, result_count=len(records), evidence_count=len(evidence),
            canonical_resolution_used=canonical_used, execution_time_ms=(time.perf_counter() - started) * 1000,
        ),
    )


def _context_result(intent: QueryIntentType, context: dict[str, Any] | None, kind: str, query_id: str, started: float, resolved_id: str) -> QueryResult:
    if context is None:
        return QueryResult(
            status=QueryResultStatus.DATA_INCOMPLETE, intent=intent,
            message=f"The {kind} was resolved (id={resolved_id}) but its context could not be assembled -- this indicates a data-integrity gap, not a genuinely missing {kind}.",
            metadata=QueryMetadata(query_id=query_id, intent=intent, execution_time_ms=(time.perf_counter() - started) * 1000),
        )
    if kind == "person":
        evidence = (
            build_evidence_from_context_items("commitments", context["commitments"], "id", ["what", "class"], timestamp_field="made_on")
            + build_evidence_from_context_items("meetings", context["meetings"], "id", ["date"], timestamp_field="date")
            + build_evidence_from_context_items("knowledge", context["knowledge"], "knowledge_id", ["subject_key", "predicate", "current_value", "basis"])
        )
        collections = ["people", "organizations", "threads", "commitments", "meetings", "follow_ups", "projects", "knowledge_items", "reply_drafts", "calendar_actions"]
    elif kind == "organization":
        evidence = build_evidence_items("people", context["people"]["data"], "id", ["name", "email"], basis=IdentityBasis.CANONICAL)
        collections = ["organizations", "people", "threads", "commitments", "meetings", "follow_ups", "projects", "knowledge_items", "reply_drafts", "calendar_actions"]
    else:  # project
        evidence = (
            build_evidence_items("commitments", context["related_commitments"], "id", ["what", "class"], timestamp_field="made_on")
            + build_evidence_items("follow_ups", context["related_follow_ups"], "id", ["commitment_id"])
        )
        collections = ["projects", "commitments", "follow_ups"]

    return QueryResult(
        status=QueryResultStatus.OK, intent=intent, records=[context], evidence=evidence,
        metadata=QueryMetadata(
            query_id=query_id, intent=intent, resolution_status=ResolutionStatus.RESOLVED,
            collections_accessed=collections, result_count=1, evidence_count=len(evidence),
            canonical_resolution_used=True, execution_time_ms=(time.perf_counter() - started) * 1000,
        ),
    )
