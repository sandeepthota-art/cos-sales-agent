import re
from datetime import datetime, timezone
from typing import Any

from pymongo.database import Database

from app.analysis.schemas import EmailAnalysis
from app.calendar.actions import build_calendar_action
from app.calendar.detector import detect_meeting
from app.config.settings import Settings
from app.duplicate_consolidation import _execute_one_mapping, _execute_one_org_mapping, generate_merge_plan
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
    OpportunityRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
    RawEmailDumpRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.email.models import Email, parse_email
from app.entities.person_context import enrich_person_context_from_email
from app.entities.resolution import resolve_canonical_person_for_email
from app.entities.thread_events import get_thread_event_trail
from app.query import retrieval
from app.query.dates import resolve_date_range
from app.query.meetings import classify_meeting
from app.query.schemas import DateRangeKind, MeetingClassification, QueryRequest
from app.query.service import execute_query
from app.knowledge.models import HistoryEntry, KnowledgeItem
from app.knowledge.normalize import slugify
from app.knowledge_lookup import KnowledgeLookup
from app.pipeline import (
    _link_knowledge_to_entities,
    _link_thread_to_entities,
    _process_entities,
    _process_knowledge,
    build_thread_timeline,
    ingest_raw_email,
    resolve_and_persist_thread,
)
from app.processing.models import ProcessingStage
from app.providers.llm.mock import _MINE_COMMITMENT_PATTERN, _OWED_TO_ME_COMMITMENT_PATTERN, MockLLMProvider
from app.replies.models import ReplyDraft, ReplyDraftContent

_BODY_PREVIEW_LENGTH = 150

# Applied to every new list_*/search_* tool's `limit` parameter -- bounds worst-case
# query/response size regardless of what a caller passes in.
_MAX_QUERY_LIMIT = 500


def _clamp_limit(limit: int) -> int:
    return max(1, min(limit, _MAX_QUERY_LIMIT))


# BRD gap-analysis FR-04 (integration gap closure): the only category values
# list_meetings will ever filter on -- exactly the ones app.query.meetings.
# classify_meeting actually assigns. PROSPECT/PROJECT are deliberately excluded:
# they exist in MeetingClassification's vocabulary for future extensibility, but
# no stored field reliably supports them today, so accepting them here would
# silently return zero results forever instead of surfacing that real limitation.
_SUPPORTED_MEETING_CATEGORIES: dict[str, MeetingClassification] = {
    "sales": MeetingClassification.SALES,
    "finance": MeetingClassification.FINANCE,
    "internal": MeetingClassification.INTERNAL,
    "customer": MeetingClassification.CUSTOMER,
    "unknown": MeetingClassification.UNKNOWN,
}


def _validate_meeting_category(category: str) -> MeetingClassification:
    normalized = category.strip().lower()
    if normalized not in _SUPPORTED_MEETING_CATEGORIES:
        raise ValueError(
            f"category must be one of {sorted(_SUPPORTED_MEETING_CATEGORIES)} "
            f"(case-insensitive); got {category!r}. 'prospect'/'project' are not "
            "accepted here -- no stored field reliably supports them yet, so "
            "classify_meeting never assigns them (see app.query.meetings)."
        )
    return _SUPPORTED_MEETING_CATEGORIES[normalized]


def _parse_required_iso(value: str, param_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{param_name} is not a valid ISO 8601 date/datetime: {value!r}") from exc
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _meeting_date_in_bounds(date_value: Any, start_dt: datetime | None, end_dt: datetime | None) -> bool:
    """Same half-open, undated-excluded-from-a-bounded-query contract as
    app.query.retrieval._in_range -- start inclusive, end exclusive, and a meeting
    with no `date` at all never matches once a bound was actually requested.
    """
    if start_dt is None and end_dt is None:
        return True
    if not date_value:
        return False
    try:
        parsed = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if start_dt is not None and parsed < start_dt:
        return False
    if end_dt is not None and parsed >= end_dt:
        return False
    return True


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


# --- Deterministic MCP boundary (Phase 1 of the Claude-Desktop-reasoning architecture) ---
#
# The functions below let a caller that has ALREADY done the LLM reasoning itself
# (e.g. Claude Desktop, reading Gmail directly) persist the result through this
# project's existing deterministic application logic, with no server-side LLM API
# call at all. Every one of them is a thin orchestration wrapper around functions
# app.pipeline.run_pipeline itself already calls -- none of entity resolution,
# commitment resolution, follow-up derivation, meeting detection, date parsing,
# context merging, or draft persistence is reimplemented here.
#
# None of these ever construct a real LLM provider or call
# ProviderFactory.create_llm_provider. The single-LLM-call path (process_email) that
# used to sit here was removed -- it depended on a server-side LLM_API_KEY that was
# unreliable in this project (expired-key/401 errors), and every real caller had
# already moved to the granular tools below instead.


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

    message_id/thread_id ARE the human-readable EML-nnn/THR-nnn ids -- there is no
    separate `email_internal_id`/`thread_internal_id` field anymore (emails and
    threads collection schema cleanup: both were always identical to
    message_id/thread_id respectively, true duplicates, verified by searching
    every reader across the codebase; see
    scripts/remove_email_id_record_id_date_fields.py and
    scripts/remove_thread_id_field.py).

    thread_timeline (only present when already_completed is False) is every PRIOR
    message in this thread, oldest first, capped to the most recent 20 -- read this
    before reasoning about the new email so the classification reflects what it's
    actually replying to, not just its own isolated content. Empty for a thread's
    first message. Separate from, and in addition to, previous_context (the
    existing compressed rolling summary) -- neither replaces the other.
    """
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    context_repo = ContextSnapshotRepository(db)

    normalized, already_completed = ingest_raw_email(email_repo, email, db)

    if already_completed:
        thread_id = _thread_id_index(db).get(normalized.message_id)
        snapshot = context_repo.latest_for_thread(thread_id) if thread_id else None
        return {
            "message_id": normalized.message_id,
            "thread_id": thread_id,
            "already_completed": True,
            "previous_context": snapshot["context"] if snapshot else None,
        }

    thread_id = resolve_and_persist_thread(thread_repo, email_repo, normalized, db)
    snapshot = context_repo.latest_for_thread(thread_id)
    thread_timeline = build_thread_timeline(
        email_repo, thread_repo, thread_id, exclude_message_id=normalized.message_id
    )
    return {
        "message_id": normalized.message_id,
        "thread_id": thread_id,
        "already_completed": False,
        "previous_context": snapshot["context"] if snapshot else None,
        "thread_timeline": thread_timeline,
    }


def ingest_raw_email_only(db: Database, email: Email) -> dict[str, Any]:
    """Ingestion-dump branch: pure persistence, zero analysis. Stores the raw
    Gmail email exactly as received into its own `raw_emails_dump` collection
    (RawEmailDumpRepository) -- completely isolated from the `emails`
    collection, ProcessingStage, and every analysis-path function in this
    module (_process_entities, _process_knowledge, persist_email_analysis,
    persist_context_delta, persist_organization_research,
    persist_person_profile, create_reply_draft, detect_meeting,
    build_calendar_action). Never canonicalizes message_id to an EML-nnn id
    (that's app.pipeline.ingest_raw_email's job for the analyzed pipeline
    only, not called here) -- the Gmail-provided id is stored as-is, in both
    `message_id` and `source_message_id` (the latter via Email's own
    `_shim_source_ids` validator when the caller doesn't supply one
    separately).

    Idempotent: a `source_message_id` already present is left untouched and
    returned as-is with `already_existed: True` -- never re-upserted, never
    duplicated (backed by raw_emails_dump's unique index on that field, see
    app.database.indexes.initialize_indexes, which is the final guarantee
    even if two ingestion attempts race each other). `already_existed` is
    also how a caller doing incremental ingestion over several messages
    tallies "duplicates skipped" vs "new emails stored" without needing a
    separate batch/counting tool -- the same per-message-call shape every
    other tool in this module already uses.

    This function calls nothing else in this module, in app.pipeline, or in
    any LLM provider -- there is no path from here to any analysis code.
    """
    repo = RawEmailDumpRepository(db)
    existing = repo.find_one({"source_message_id": email.source_message_id})
    if existing:
        return {**existing, "already_existed": True}

    document = email.model_dump(mode="json", by_alias=True)
    document["source"] = "gmail"
    document["ingested_at"] = datetime.now(timezone.utc).isoformat()
    stored = repo.upsert_by_key({"source_message_id": email.source_message_id}, document)
    return {**stored, "already_existed": False}


def get_raw_ingestion_status(db: Database) -> dict[str, Any]:
    """Ingestion-dump branch: read-only checkpoint for the raw-only path,
    completely separate from get_last_ingested_email (which reads the
    *analyzed* `emails` collection -- a different collection, a different
    pipeline, never shared with this one).

    Never decides a fallback start date itself -- that stays owned by the
    gmail-raw-dump skill's own prose (mirroring how get_last_ingested_email
    stays a pure data reader and gmail-initial-ingest's Mode C owns the
    Oct-1-2026 fallback), so this function has exactly one job: report what's
    already in raw_emails_dump.

    `latest_email_timestamp` is the latest dumped email's own Email.timestamp
    (its real send/receive date -- what a Gmail date-range search needs),
    compared chronologically via the same _parse_timestamp_for_sort
    get_last_ingested_email already uses, never lexicographically.
    `latest_ingested_at` is this collection's own bookkeeping field (when we
    happened to run the dump) -- reported for visibility only, and
    deliberately never used to decide where to resume a Gmail search:
    Gmail's own send/receive date is the correct axis for "what mail is
    newer than what we already have," not when we happened to run this tool.

    No `processing_status`/stage concept applies here (raw-dumped documents
    never have one), so unlike get_last_ingested_email there is no
    "earliest unprocessed" to track -- every raw-dumped email is equally
    "done" the moment it's stored.
    """
    emails = RawEmailDumpRepository(db).find_many({})
    if not emails:
        return {"has_existing_data": False, "latest_email_timestamp": None, "latest_ingested_at": None}
    latest = max(emails, key=lambda e: _parse_timestamp_for_sort(e.get("timestamp")))
    return {
        "has_existing_data": True,
        "latest_email_timestamp": latest.get("timestamp"),
        "latest_ingested_at": latest.get("ingested_at"),
    }


def persist_email_analysis(
    db: Database, message_id: str, analysis: EmailAnalysis, settings: Settings, skip_knowledge: bool = False
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

    skip_knowledge (default False, unchanged production behavior when omitted): when
    True, nothing writes to knowledge_items for this email -- not _process_knowledge,
    not _process_entities' own person_facts_mentioned handling (_process_person_facts),
    and not this function's own explicit Person->WORKS_AT->Organization fact below.
    Canonical entity resolution (people/projects/commitments/follow_ups/meetings/
    opportunities/personal_items) still runs exactly as it always does -- only the
    knowledge layer is skipped. Exists for the raw-dump-replay skill, testing entity
    resolution against your own email set without also generating knowledge facts you
    don't care about for that test. The ANALYZED -> KNOWLEDGE_PROCESSED ->
    ENTITIES_PROCESSED stage sequence on the email doc is unaffected either way --
    KNOWLEDGE_PROCESSED is still recorded as a stage transition even when the
    knowledge step itself was skipped, so stage ordering stays meaningful.
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

    if not skip_knowledge:
        _process_knowledge(
            db, knowledge_repo, thread_id, analysis, MockLLMProvider(), thread_id, message_id, datetime.now(timezone.utc)
        )
    email_repo.set_stage(message_id, ProcessingStage.KNOWLEDGE_PROCESSED.value)

    entities_referenced = _process_entities(
        db, thread_id, email, analysis, reference_now, settings.agent_email, MockLLMProvider(), settings.agent_name,
        skip_knowledge=skip_knowledge,
    )
    email_repo.set_entity_metadata(
        message_id=message_id,
        entities_referenced=entities_referenced,
        goal_pillar=analysis.goal_pillar,
        label_applied=analysis.label_applied,
    )
    email_repo.set_stage(message_id, ProcessingStage.ENTITIES_PROCESSED.value)

    # Same additive linking passes run_pipeline itself runs, after entity resolution.
    _link_knowledge_to_entities(db, thread_id, message_id)
    _link_thread_to_entities(db, thread_id)

    # Person Context, Phase 2/5: after entity resolution AND knowledge linking (so
    # KnowledgeItem.person_id is already set for this email's facts) -- one
    # incremental, idempotent snapshot per person this email actually involved.
    # Bug fix: this previously only ran inside run_pipeline (file-mode/demo/
    # scheduler ingestion), never from this granular path -- which is what the
    # real Gmail-driven skill actually calls -- so no PersonContextSnapshot was
    # ever built for a real, Gmail-ingested email.
    enrich_person_context_from_email(db, thread_id, email, analysis, entities_referenced, reference_now)

    # New organizations needing research (Knowledge Layer): collect every org_id
    # this email's resolved people actually belong to, then surface whichever of
    # those organizations has never been researched. Computed fresh from
    # researched_at on every call, not a one-shot "just created" flag, so a
    # session that lacks WebSearch access doesn't permanently lose the chance --
    # the next email mentioning the same org surfaces it again.
    person_repo = PersonRepository(db)
    org_repo = OrganizationRepository(db)
    referenced_org_ids = {
        person["org_id"]
        for person_id in entities_referenced.get("people", [])
        if (person := person_repo.find_one({"id": person_id})) and person.get("org_id")
    }
    new_organizations_needing_research = [
        {"org_id": org["id"], "name": org["name"], "domain": org.get("domain")}
        for org_id in referenced_org_ids
        if (org := org_repo.find_one({"id": org_id})) and org.get("researched_at") is None
    ]

    # Explicit Person -> WORKS_AT -> Organization knowledge fact (Knowledge
    # Layer): Person.org_id already IS this relationship as a silent FK; this
    # records it as an auditable KnowledgeItem too, with history/confidence/
    # source_emails like every other extracted fact. Looked up directly by
    # (person_id, predicate) -- not process_new_fact's fuzzy text-similarity
    # dedup, since a WORKS_AT fact's identity is always exactly one person and
    # one org_id, never free text requiring fuzzy matching.
    for person_id in entities_referenced.get("people", []):
        if skip_knowledge:
            continue
        person = person_repo.find_one({"id": person_id})
        if not person or not person.get("org_id"):
            continue
        org = org_repo.find_one({"id": person["org_id"]})
        if org is None:
            continue
        now = datetime.now(timezone.utc)
        existing_fact = knowledge_repo.find_one({"person_id": person_id, "predicate": "works_at"})

        if existing_fact is None:
            works_at_fact = KnowledgeItem(
                knowledge_id=f"knowledge_{thread_id}_{person_id}_works_at",
                thread_id=thread_id,
                # Human-readable subject/value, like every other KnowledgeItem --
                # the dashboard and the analyze-email LLM prompt both render these
                # as plain text, so a raw PER-xxx/ORG-xxx id would show as noise.
                subject_key=slugify(person["name"]),
                predicate="works_at",
                fact_key="works_at",
                current_value=org["name"],
                person_id=person_id,
                org_id=person["org_id"],
                history=[HistoryEntry(value=org["name"], source_email_id=message_id, recorded_at=now)],
                source_emails=[message_id],
                basis="stated",
                first_seen_at=now,
                last_confirmed_at=now,
                confidence=0.95,
            )
            knowledge_repo.upsert_by_key(
                {"knowledge_id": works_at_fact.knowledge_id}, works_at_fact.model_dump(mode="json")
            )
        elif existing_fact.get("org_id") != person["org_id"]:
            # Job change: the person's canonical org_id has moved since this fact
            # was last recorded -- update it and append a history entry, rather
            # than leaving a permanently stale record forever.
            current_item = KnowledgeItem.model_validate(existing_fact)
            updated_item = current_item.model_copy(
                update={
                    "current_value": org["name"],
                    "org_id": person["org_id"],
                    "history": [
                        *current_item.history,
                        HistoryEntry(value=org["name"], source_email_id=message_id, recorded_at=now),
                    ],
                    "source_emails": list(dict.fromkeys([*current_item.source_emails, message_id])),
                    "last_confirmed_at": now,
                }
            )
            knowledge_repo.upsert_by_key(
                {"knowledge_id": updated_item.knowledge_id}, updated_item.model_dump(mode="json")
            )

    # People profile context (Customer/Contact Intelligence): for every
    # person this email references, bundle their current profile state plus
    # their organization's already-researched profile into one payload, so
    # Claude can compose an updated profile (if this email adds anything
    # substantive) without a second lookup.
    people_profile_context = []
    for person_id in dict.fromkeys(entities_referenced.get("people", [])):
        profile_person = person_repo.find_one({"id": person_id})
        if profile_person is None:
            continue
        person_org = (
            org_repo.find_one({"id": profile_person["org_id"]}) if profile_person.get("org_id") else None
        )
        people_profile_context.append(
            {
                "person_id": person_id,
                "name": profile_person["name"],
                "role": profile_person.get("role"),
                "profile_summary": profile_person.get("profile_summary"),
                "recent_context": profile_person.get("recent_context"),
                "key_topics": profile_person.get("key_topics", []),
                "org_name": person_org["name"] if person_org else None,
                "org_description": person_org.get("description") if person_org else None,
                "org_industry": person_org.get("industry") if person_org else None,
            }
        )

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

    # Non-authoritative sanity check, never a fabrication: the same lightweight
    # regex MockLLMProvider itself uses to decide whether a body reads as a
    # commitment ("I will"/"I'll"/"we will", "could you"/"can you") -- surfaced
    # here only as a prompt to double-check, never used to invent a commitment.
    # Added after a real gap: an email stating two explicit, dated commitments
    # ("I'll send X by Oct 6", "could you share Y by Oct 9") reached COMPLETED
    # with commitments_mentioned=[] and nothing caught it until a manual DB
    # check days later. This can't fire a false negative that matters (it only
    # ever adds a warning, never blocks or alters persistence), but it CAN
    # false-positive on body text that merely resembles commitment language
    # without being one -- treat it as "double-check this," not "this is wrong."
    possible_missed_commitment = None
    if not analysis.commitments_mentioned and (
        _MINE_COMMITMENT_PATTERN.search(email.body) or _OWED_TO_ME_COMMITMENT_PATTERN.search(email.body)
    ):
        possible_missed_commitment = (
            "This email's body contains commitment-shaped language (e.g. \"I'll...\"/"
            "\"could you...\") but commitments_mentioned was empty. Re-read the email "
            "for a commitment you may have missed before calling mark_email_completed -- "
            "or ignore this if there genuinely isn't one (the check is a heuristic, not "
            "authoritative)."
        )

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "entities_referenced": entities_referenced,
        "new_organizations_needing_research": new_organizations_needing_research,
        "people_profile_context": people_profile_context,
        "possible_missed_commitment": possible_missed_commitment,
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


def _parse_timestamp_for_sort(value: str | None) -> datetime:
    """A shared, chronologically-correct sort key for email timestamps --
    NOT plain string comparison. Review finding (final review of the
    Incremental Gmail Ingestion plan): two real timestamps can carry
    different UTC offsets (e.g. "...T01:00:00+05:30" vs "...T23:00:00Z"
    for the instant just before it), and `normalize_email` never
    standardizes the timestamp's string form -- a plain `max()` over the
    raw strings can silently pick the chronologically EARLIER one. Missing/
    unparseable values sort as the minimum possible instant, never crash.
    """
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def get_last_ingested_email(db: Database) -> dict[str, Any] | None:
    """Read-only: the most recently ingested email, by its own timestamp
    (Email.timestamp -- the email's send/receive date; this project has
    never stored a separate ingestion-time field, and the email's own date
    is what a Gmail date-range search needs anyway). Timestamps are
    compared chronologically, not lexicographically (see
    _parse_timestamp_for_sort).

    Returns None if nothing has been ingested yet. `message_id`/
    `source_message_id`/`thread_id`/`timestamp` always describe the overall
    latest email regardless of processing_status.stage (including one
    stuck mid-pipeline) -- the purpose is purely "where does our data
    already reach to," not "what's fully processed."

    `earliest_unprocessed_timestamp` (None if every ingested email has
    reached COMPLETED) is the earliest timestamp among emails that have
    NOT reached COMPLETED. Final review finding: without this, an email
    stuck mid-pipeline is silently abandoned forever once ANY newer email
    completes and moves the search boundary past it -- the boundary only
    ever moves forward, so "the next incremental search re-surfaces it"
    was only true when the stuck email happened to be the single newest
    one. A caller should floor its search boundary at the EARLIER of this
    email's timestamp and earliest_unprocessed_timestamp (when not None),
    so a stuck email from an earlier run is always re-included.
    """
    emails = EmailRepository(db).find_many({})
    if not emails:
        return None
    latest = max(emails, key=lambda e: _parse_timestamp_for_sort(e.get("timestamp")))
    unprocessed = [
        e for e in emails
        if e.get("processing_status", {}).get("stage") != ProcessingStage.COMPLETED.value
    ]
    earliest_unprocessed = (
        min(unprocessed, key=lambda e: _parse_timestamp_for_sort(e.get("timestamp")))
        if unprocessed else None
    )
    return {
        "message_id": latest["message_id"],
        "source_message_id": latest.get("source_message_id"),
        "thread_id": latest.get("thread_id"),
        "timestamp": latest.get("timestamp"),
        "earliest_unprocessed_timestamp": (
            earliest_unprocessed.get("timestamp") if earliest_unprocessed else None
        ),
    }


def list_processed_emails(db: Database, limit: int = 50) -> list[dict[str, Any]]:
    thread_id_by_message_id: dict[str, str] = {}
    for thread in ThreadRepository(db).find_many({}):
        for message_id in thread["message_ids"]:
            thread_id_by_message_id[message_id] = thread["thread_id"]

    # .get(...) or "", not e["timestamp"]: a malformed/partially-written email
    # document (e.g. a retry that left only a processing_status stub behind)
    # must never crash this listing -- it just sorts to the oldest end instead.
    emails = sorted(
        EmailRepository(db).find_many({}), key=lambda e: e.get("timestamp") or "", reverse=True
    )[:limit]

    context_repo = ContextSnapshotRepository(db)
    summary_by_thread_id: dict[str, str | None] = {}

    _REQUIRED_FIELDS = ("from", "to", "cc", "subject", "timestamp", "body")

    results: list[dict[str, Any]] = []
    for email in emails:
        # A malformed/partially-written document (e.g. a retry that left only a
        # processing_status stub behind, with no subject/body/from/to at all)
        # never actually completed ingestion -- it isn't a real "processed
        # email" to report on, so it's skipped rather than shown with
        # fabricated placeholder values for the fields it's missing.
        if any(field not in email for field in _REQUIRED_FIELDS):
            continue

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
            }
        )

    return results


def search_emails(
    db: Database,
    from_email: str | None = None,
    subject_contains: str | None = None,
    label_applied: str | None = None,
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

    # .get(...) or "" -- same defensive reasoning as list_processed_emails above.
    emails = sorted(
        EmailRepository(db).find_many(query), key=lambda e: e.get("timestamp") or "", reverse=True
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
        }
        for e in emails
    ]


def get_thread(db: Database, thread_id: str) -> dict[str, Any] | None:
    """Read-only retrieval of one full conversation. Returns None if the thread doesn't
    exist -- never fabricates one. `latest_context`/`context_version` are None when no
    context_snapshot exists for this thread (e.g. a raw-only thread that was never run
    through the AI pipeline) -- never invented. `event_trail` is the deterministic,
    already-recorded Thread Events audit trail (app.entities.thread_events.
    get_thread_event_trail) -- integrated into this existing thread-retrieval surface
    rather than a new, duplicate MCP tool; [] for a thread with no recorded events.

    Each message's `source_message_id` is the original, permanent Gmail/provider
    message id (distinct from `message_id`, the canonical EML-nnn) -- needed by
    skills/sync-reply-drafts-to-gmail/SKILL.md to create a properly-threaded Gmail
    draft reply (Gmail's `replyToMessageId`). None for a message whose source never
    supplied one -- never fabricated.
    """
    thread = ThreadRepository(db).find_one({"thread_id": thread_id})
    if thread is None:
        return None

    # .get(...) or "" -- same defensive reasoning as list_processed_emails above.
    emails = sorted(
        EmailRepository(db).find_many({"message_id": {"$in": thread["message_ids"]}}),
        key=lambda e: e.get("timestamp") or "",
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
                "source_message_id": e.get("source_message_id"),
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
        "event_trail": get_thread_event_trail(db, thread_id),
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


def update_project_fields(
    db: Database,
    project_id: str,
    status: str | None = None,
    owner: str | None = None,
    health: str | None = None,
    next_milestone: str | None = None,
    due: str | None = None,
) -> dict[str, Any]:
    """The ONLY mechanism for setting a Project's manually-managed fields
    (status, owner, health, next_milestone, due). Nothing in the pipeline ever
    infers or writes these -- `app.entities.resolution._resolve_project_impl`
    only ever sets id/project/entity/goal_pillar/person_ids/org_id at creation.
    Mirrors `update_opportunity_fields` exactly (closing the "Project product
    gap": these fields existed on the schema with no way to ever populate
    them). Deterministic, LLM-free: this tool never reasons about email
    content, it only persists whatever explicit values the caller supplies.

    Only fields you actually pass (non-None) are updated -- an omitted field
    is left exactly as it was, never reset to null. There is currently no way
    to clear an already-set field back to null through this tool, same known
    V1 limitation as update_opportunity_fields.

    Never touches person_ids/org_id/goal_pillar/project (the name) -- those
    remain exclusively pipeline-derived.

    due, if given, must be an ISO 8601 date/datetime string.
    """
    repo = ProjectRepository(db)
    existing = repo.find_one({"id": project_id})
    if existing is None:
        raise ValueError(f"no project found for id={project_id!r}")

    update: dict[str, Any] = {}
    if status is not None:
        update["status"] = status
    if owner is not None:
        update["owner"] = owner
    if health is not None:
        update["health"] = health
    if next_milestone is not None:
        update["next_milestone"] = next_milestone
    if due is not None:
        update["due"] = due

    if update:
        repo.upsert_by_key({"id": project_id}, {**existing, **update})

    return repo.find_one({"id": project_id})


def list_opportunities(
    db: Database,
    org_id: str | None = None,
    status: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `opportunities` collection. Optional
    filters (all exact match): org_id, status ("open"/"won"/"lost"), project_id
    (matches any Opportunity whose project_ids includes it -- an array-membership
    query, not a text search). Never creates, resolves, or modifies anything.
    """
    query: dict[str, Any] = {}
    if org_id:
        query["org_id"] = org_id
    if status:
        query["status"] = status
    if project_id:
        query["project_ids"] = project_id

    return OpportunityRepository(db).find_many(query)[: _clamp_limit(limit)]


def update_opportunity_fields(
    db: Database,
    opportunity_id: str,
    stage: str | None = None,
    owner: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    expected_close_date: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """The ONLY mechanism for setting an Opportunity's manually-managed CRM
    fields (stage, owner, value, currency, expected_close_date, next_action).
    Nothing in the pipeline ever infers or writes these -- see
    app.entities.resolution.resolve_opportunity's own docstring. Deterministic,
    LLM-free: this tool never reasons about the email content itself, it only
    persists whatever explicit values the caller supplies.

    Only fields you actually pass (non-None) are updated -- an omitted field is
    left exactly as it was, never reset to null. There is currently no way to
    clear an already-set field back to null through this tool (a known V1
    limitation, not an oversight).

    Never touches source_email_ids/project_ids/meeting_ids/person_ids/
    buying_signals/last_activity_at -- those remain exclusively pipeline-derived.

    expected_close_date, if given, must be an ISO 8601 date/datetime string.
    """
    repo = OpportunityRepository(db)
    existing = repo.find_one({"id": opportunity_id})
    if existing is None:
        raise ValueError(f"no opportunity found for id={opportunity_id!r}")

    update: dict[str, Any] = {}
    if stage is not None:
        update["stage"] = stage
    if owner is not None:
        update["owner"] = owner
    if value is not None:
        update["value"] = value
    if currency is not None:
        update["currency"] = currency
    if expected_close_date is not None:
        update["expected_close_date"] = expected_close_date
    if next_action is not None:
        update["next_action"] = next_action

    if update:
        update["updated_at"] = datetime.now(timezone.utc).isoformat()
        repo.upsert_by_key({"id": opportunity_id}, {**existing, **update})

    return repo.find_one({"id": opportunity_id})


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
    db: Database,
    thread_id: str | None = None,
    actionable: bool | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
    agent_email: str | None = None,
) -> list[dict[str, Any]]:
    """Read-only listing over the existing `meetings` collection. Never creates a
    calendar event and never calls a calendar provider/MCP.

    category (BRD gap-analysis FR-04, closing the integration gap): an optional
    exact-match filter on this meeting's derived classification
    (app.query.meetings.classify_meeting) -- one of "sales", "finance", "internal",
    "customer", "unknown" (case-insensitive). Reuses that existing classification
    function as-is; this tool does not introduce a second, competing classification
    mechanism. Raises ValueError for any other value, including "prospect"/
    "project" -- see _validate_meeting_category for why those are rejected rather
    than silently returning zero results.

    start_date/end_date: optional ISO 8601 date or datetime strings, filtered
    against this meeting's own stored `date` field with the same half-open,
    undated-excluded contract used throughout app.query.retrieval (start
    inclusive, end exclusive). Independent of `category` -- either or both may be
    supplied, e.g. to answer "what sales meetings do I have today" by passing
    today's own [start, end) bounds alongside category="sales". Raises ValueError
    for an unparseable value rather than silently ignoring it.

    agent_email: only used for classify_meeting's INTERNAL/CUSTOMER domain
    derivation when `category` is supplied -- irrelevant to SALES/FINANCE, which
    are derived purely from the meeting's own stored project_or_pillar field.
    """
    query: dict[str, Any] = {}
    if thread_id:
        query["thread_id"] = thread_id
    if actionable is not None:
        query["actionable"] = actionable

    docs = MeetingRepository(db).find_many(query)

    if start_date is not None or end_date is not None:
        start_dt = _parse_required_iso(start_date, "start_date") if start_date is not None else None
        end_dt = _parse_required_iso(end_date, "end_date") if end_date is not None else None
        docs = [d for d in docs if _meeting_date_in_bounds(d.get("date"), start_dt, end_dt)]

    if category is not None:
        target = _validate_meeting_category(category)
        docs = [d for d in docs if classify_meeting(db, d, agent_email) == target]

    docs.sort(key=lambda d: d.get("id", ""))
    return docs[: _clamp_limit(limit)]


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


def set_reply_draft_gmail_id(db: Database, reply_id: str, gmail_draft_id: str) -> dict[str, Any]:
    """Records the real Gmail draft id a human (via Claude's own Gmail connector --
    see skills/sync-reply-drafts-to-gmail/SKILL.md) already created for this reply
    draft, so a later sync pass knows not to create a duplicate Gmail draft for the
    same reply.

    This tool never creates, edits, or sends anything in Gmail itself -- this
    codebase has no real email-sending/draft-creation integration of its own (see
    app.replies.approval.simulate_send's own docstring). It only persists the id a
    Gmail draft already has, exactly the same "record what already happened
    elsewhere" role app.mcp.tools.set_reply_draft_gmail_id's sibling tools play for
    other externally-performed actions.

    Raises ValueError if reply_id doesn't exist -- never silently creates one.
    """
    repo = ReplyDraftRepository(db)
    existing = repo.find_one({"reply_id": reply_id})
    if existing is None:
        raise ValueError(f"no reply draft found for reply_id={reply_id!r}")

    repo.upsert_by_key({"reply_id": reply_id}, {**existing, "gmail_draft_id": gmail_draft_id})
    return repo.find_one({"reply_id": reply_id})


def set_reply_withheld_reason(db: Database, message_id: str, reason: str) -> dict[str, Any]:
    """Records why a message that was classified as needing a reply did not get
    a reply draft -- e.g. the request asks for sensitive data (bank details,
    credentials), the thread shows phishing/spoofing red flags, or a human/Claude
    judgment call decided an automated draft wasn't appropriate. Persisted directly
    on the Email document as `reply_withheld_reason`, so the dashboard's email
    lookup box and anyone querying `emails` can see the reason instead of an
    unexplained gap between label_applied="1. Needs reply" and no reply_drafts record.

    This tool never creates or edits a reply draft itself, and never overwrites
    label_applied, processing_status, or entities_referenced -- purely additive.
    Raises ValueError if message_id doesn't exist, never silently fabricates one.
    """
    email_repo = EmailRepository(db)
    stored = email_repo.find_one({"message_id": message_id})
    if stored is None:
        raise ValueError(f"no email found for message_id={message_id!r}")

    email_repo.upsert_by_key({"message_id": message_id}, {"reply_withheld_reason": reason})
    return email_repo.find_one({"message_id": message_id})


def set_email_summary(db: Database, message_id: str, summary: str) -> dict[str, Any]:
    """Records a short, human-written summary of an email's body, for the dashboard's
    "Look up an email" box to show in place of (or alongside) the full raw body --
    never generated by an API call from this codebase. This project has no LLM
    client of its own in any live path (the configured LLM_API_KEY is expired, and
    the dashboard specifically has zero LLM calls by design); exactly like
    persist_email_analysis/set_reply_withheld_reason, the summary is authored by
    whichever caller invokes this tool (Claude, having actually read the email body
    in this conversation) and simply persisted here.

    Cached on the Email document as `body_summary` -- computed once, reused on
    every future dashboard view, never regenerated automatically. Purely additive;
    never overwrites label_applied, processing_status, or entities_referenced.

    Raises ValueError if message_id doesn't exist -- never silently fabricates one.
    """
    email_repo = EmailRepository(db)
    stored = email_repo.find_one({"message_id": message_id})
    if stored is None:
        raise ValueError(f"no email found for message_id={message_id!r}")

    email_repo.upsert_by_key({"message_id": message_id}, {"body_summary": summary})
    return email_repo.find_one({"message_id": message_id})


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


def persist_person_profile(
    db: Database,
    person_id: str,
    role: str | None = None,
    profile_summary: str | None = None,
    recent_context: str | None = None,
    key_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Persists a Customer/Contact Intelligence profile update a reasoning
    caller (Claude, having read the triggering email plus this person's
    existing profile from persist_email_analysis's people_profile_context)
    already composed -- this function never synthesizes text itself, purely
    persistence, exactly like persist_organization_research's own split.

    Each field is independently optional: None leaves that field exactly as
    it already was. There is no "manual" overwrite-protection like
    Organization's research has -- every field here is always Claude-
    composed, so there is nothing to protect against; the instruction to
    incorporate the existing value (already handed back via
    people_profile_context) is what prevents regression, not a tool guard.

    Raises ValueError if person_id doesn't exist.
    """
    repo = PersonRepository(db)
    person = repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    # Every field is written explicitly (even when left unchanged), not just
    # the ones that got a new value -- so the returned/stored document always
    # exposes the full field set, matching Person's own defaults, rather than
    # silently omitting a key a pre-existing document never had.
    update: dict[str, Any] = {
        "role": role if role is not None else person.get("role"),
        "profile_summary": profile_summary if profile_summary is not None else person.get("profile_summary"),
        "recent_context": recent_context if recent_context is not None else person.get("recent_context"),
        "key_topics": key_topics if key_topics is not None else person.get("key_topics", []),
        "profile_updated_at": datetime.now(timezone.utc),
    }

    repo.upsert_by_key({"id": person_id}, {**person, **update})
    return {**person, **update}


def persist_organization_research(
    db: Database,
    org_id: str,
    industry: str | None = None,
    description: str | None = None,
    products_services: list[str] | None = None,
    size_estimate: str | None = None,
    headquarters: str | None = None,
    website: str | None = None,
    research_source: str = "web_research",
) -> dict[str, Any]:
    """Persists external research a reasoning caller (Claude, via its own WebSearch
    tool) already performed about a company -- this function never searches or
    calls an LLM itself, purely persistence, exactly like persist_email_analysis's
    own split between reasoning and storage.

    Only overwrites a profile field if the existing Organization's
    research_source != "manual" -- a hand-corrected field can never be silently
    clobbered by a later automated research pass. researched_at is always set to
    now, regardless of whether any field was actually supplied, so a company that
    genuinely has no useful public information is still marked "looked, found
    nothing" rather than being re-surfaced by persist_email_analysis's
    new_organizations_needing_research signal on every future email.

    Raises ValueError if org_id doesn't exist.
    """
    repo = OrganizationRepository(db)
    org = repo.find_one({"id": org_id})
    if org is None:
        raise ValueError(f"no organization found for org_id={org_id!r}")

    protect_existing = org.get("research_source") == "manual"
    supplied = {
        "industry": industry, "description": description,
        "products_services": products_services, "size_estimate": size_estimate,
        "headquarters": headquarters, "website": website,
    }
    # Every profile field is written explicitly (even when left unchanged), not
    # just the ones that got a new value -- so the returned/stored document
    # always exposes the full field set, matching Organization's own defaults,
    # rather than silently omitting a key nothing has ever set yet.
    update: dict[str, Any] = {
        field: (org.get(field) if (protect_existing or value is None) else value)
        for field, value in supplied.items()
    }
    update["research_source"] = org.get("research_source") if protect_existing else research_source
    update["researched_at"] = datetime.now(timezone.utc)

    repo.upsert_by_key({"id": org_id}, {**org, **update})
    return {**org, **update}


def list_unresearched_organizations(db: Database) -> list[dict[str, Any]]:
    """Read-only catch-up list: every active Organization that has never been
    researched (researched_at is None), independent of
    persist_email_analysis's per-email new_organizations_needing_research
    signal -- for a company created but never mentioned again in a later email,
    or a session that lacked WebSearch access when the signal first fired.
    """
    return [
        org for org in OrganizationRepository(db).find_many({"researched_at": None})
        if org.get("status", "active") == "active"
    ]


_ORG_SUFFIX_TOKENS = {
    "inc", "llc", "ltd", "corp", "corporation", "group",
    "technologies", "analytics", "solutions", "co",
}


def _org_name_tokens(name: str) -> set[str]:
    lowered = re.sub(r"[^\w\s]", "", name.lower())
    return {t for t in lowered.split() if t not in _ORG_SUFFIX_TOKENS}


def _org_website_domain(org: dict[str, Any]) -> str | None:
    website = org.get("website")
    if not website:
        return None
    stripped = re.sub(r"^https?://", "", website).split("/")[0]
    return stripped[4:] if stripped.startswith("www.") else stripped


def preview_duplicate_organization_candidates(db: Database) -> list[dict[str, Any]]:
    """Read-only heuristic preview of organizations that may be the same real
    company under different names/domains (e.g. "DataBeat" / "DataBeat
    Analytics" / databeat.com) -- resolve_organization's own automatic path
    stays domain-only and never merges these; this is the human/Claude-
    reviewed catch for everything domain-matching can't see. Never merges
    anything itself -- see merge_organization_records for the explicit,
    caller-confirmed execution step. Already-merged organizations are never
    candidates (status != "active" is skipped entirely).

    A pair is flagged when, after normalizing both names (lowercase, strip
    punctuation, drop common suffix words: Inc/LLC/Ltd/Corp/Corporation/
    Group/Technologies/Analytics/Solutions/Co), their non-empty token sets
    are equal or one is a subset of the other ("name_match"), OR one
    organization's website domain matches the other's stored domain or an
    alias ("domain_cross_match").
    """
    orgs = [o for o in OrganizationRepository(db).find_many({}) if o.get("status", "active") == "active"]
    candidates: list[dict[str, Any]] = []

    for i, a in enumerate(orgs):
        a_tokens = _org_name_tokens(a["name"])
        a_website_domain = _org_website_domain(a)
        for b in orgs[i + 1:]:
            b_tokens = _org_name_tokens(b["name"])
            b_website_domain = _org_website_domain(b)

            name_match = bool(a_tokens) and bool(b_tokens) and (
                a_tokens == b_tokens or a_tokens <= b_tokens or b_tokens <= a_tokens
            )
            domain_cross_match = (
                (a_website_domain is not None and a_website_domain == b.get("domain"))
                or (b_website_domain is not None and b_website_domain == a.get("domain"))
                or (a.get("domain") and a["domain"] in (b.get("aliases") or []))
                or (b.get("domain") and b["domain"] in (a.get("aliases") or []))
            )

            if not (name_match or domain_cross_match):
                continue

            candidates.append(
                {
                    "org_a_id": a["id"], "org_a_name": a["name"],
                    "org_b_id": b["id"], "org_b_name": b["name"],
                    "evidence": "name_match" if name_match else "domain_cross_match",
                }
            )

    return candidates


def merge_organization_records(db: Database, source_org_id: str, target_org_id: str) -> dict[str, Any]:
    """Merges one Organization record into another, for a caller (Claude,
    having reviewed a preview_duplicate_organization_candidates pair, with
    WebSearch confirmation if genuinely ambiguous) who is confident both
    records are the same real company. Unlike resolve_organization's
    automatic domain-only path, this always requires an explicit, named
    pair -- there is no automatic name-similarity merge anywhere in this
    codebase.

    Repoints org_id across every referencing collection (people, projects,
    opportunities, commitments, follow_ups, meetings, knowledge_items,
    reply_drafts, calendar_actions), unions aliases (adding the source's own
    name as an alias on the target, so a lookup by the old name still
    resolves), and backfills any blank profile/research field on the target
    from the source without overwriting a populated target field. The source
    organization is never deleted -- only retired (status="merged",
    merged_into=target_org_id), mirroring merge_person_records exactly. A
    later resolve_organization domain lookup against the retired source
    transparently redirects to the target (app.entities.organization_lifecycle).

    Raises ValueError if either id doesn't exist, if they're the same id, or
    if source_org_id is already merged into someone else.
    """
    repo = OrganizationRepository(db)
    source = repo.find_one({"id": source_org_id})
    target = repo.find_one({"id": target_org_id})
    if source is None:
        raise ValueError(f"no organization found for source_org_id={source_org_id!r}")
    if target is None:
        raise ValueError(f"no organization found for target_org_id={target_org_id!r}")
    if source_org_id == target_org_id:
        raise ValueError("source_org_id and target_org_id must be different")
    if source.get("status") == "merged":
        raise ValueError(f"org_id={source_org_id!r} is already merged into {source.get('merged_into')!r}")

    return _execute_one_org_mapping(db, source_org_id, target_org_id)


def list_organizations(db: Database, name_contains: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Read-only listing over the existing `organizations` collection -- the
    MCP-reachable way to turn a company NAME into the canonical org_id
    get_company_summary/persist_organization_research/merge_organization_records
    all require. name_contains matches case-insensitively, anywhere in the name
    (a plain regex search, no fuzzy matching).
    """
    query: dict[str, Any] = {}
    if name_contains:
        query["name"] = re.compile(re.escape(name_contains), re.IGNORECASE)
    return OrganizationRepository(db).find_many(query)[: _clamp_limit(limit)]


def get_company_summary(db: Database, org_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary for an organization, assembled with
    plain Mongo queries in application code -- no LLM involved. Returns None
    for an org_id that doesn't exist, matching get_project_summary's own
    convention for an unknown id.

    Takes a canonical org_id (not a free-text name): every related collection
    (Person, Project, Commitment, FollowUp, Meeting, KnowledgeItem) already
    carries a real org_id reference, populated by the pipeline's own entity
    resolution -- this function joins on that FK directly. An earlier version
    of this tool matched Person.org/Project.entity by free-text string
    equality; Person.org is confirmed never populated by any resolution path,
    so that version silently returned an empty people list for every real
    organization, and hardcoded related_meetings/related_knowledge_items to []
    with a docstring claiming the schema couldn't support them -- both
    Meeting.org_id and KnowledgeItem.org_id already exist and are populated.
    """
    org = OrganizationRepository(db).find_one({"id": org_id})
    if org is None:
        return None

    return {
        "people": PersonRepository(db).find_many({"org_id": org_id}),
        "projects": ProjectRepository(db).find_many({"org_id": org_id}),
        "related_commitments": CommitmentRepository(db).find_many({"org_id": org_id}),
        "related_follow_ups": FollowUpRepository(db).find_many({"org_id": org_id}),
        "related_meetings": MeetingRepository(db).find_many({"org_id": org_id}),
        "related_knowledge_items": KnowledgeRepository(db).find_many({"org_id": org_id}),
        "relationship_notes": {
            "people": "direct: Person.org_id == org_id",
            "projects": "direct: Project.org_id == org_id",
            "related_commitments": "direct: Commitment.org_id == org_id",
            "related_follow_ups": "direct: FollowUp.org_id == org_id",
            "related_meetings": "direct: Meeting.org_id == org_id",
            "related_knowledge_items": "direct: KnowledgeItem.org_id == org_id",
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


def preview_duplicate_person_candidates(db: Database) -> dict[str, Any]:
    """Read-only preview of possible duplicate Person records -- reuses the existing
    duplicate-consolidation classifier (app.duplicate_consolidation.generate_merge_plan)
    exactly as-is, never a second detection mechanism. Only ever classifies and reports;
    never writes, merges, or approves anything -- every candidate returned still has
    approved=False and nothing is changed until a human reviews it and a separate,
    explicit consolidation run is approved.

    Returns {candidate_count, candidates: [{duplicate_person_id, duplicate_name,
    duplicate_email, canonical_person_id, canonical_name, canonical_email, confidence,
    evidence, downstream_records_affected}]} -- downstream_records_affected is a single
    total count (summed across collections) rather than the full nested impact
    breakdown, so this stays a short, reportable summary rather than a technical dump.
    """
    plan = generate_merge_plan(db)
    candidates = []
    for entry in plan:
        downstream_total = sum(
            counts.get("total_affected", 0) for counts in entry.get("downstream_impact", {}).values()
        )
        candidates.append(
            {
                "duplicate_person_id": entry["duplicate_person_id"],
                "duplicate_name": entry["duplicate_name"],
                "duplicate_email": entry["duplicate_email"],
                "canonical_person_id": entry["canonical_person_id"],
                "canonical_name": entry["canonical_name"],
                "canonical_email": entry["canonical_email"],
                "confidence": entry["confidence"],
                "evidence": entry["evidence"],
                "downstream_records_affected": downstream_total,
            }
        )
    return {"candidate_count": len(candidates), "candidates": candidates}


def merge_person_records(
    db: Database, duplicate_person_id: str, canonical_person_id: str, reason: str
) -> dict[str, Any]:
    """Merges one Person record into another, for a caller (a human, or Claude having
    read the actual email evidence) who has independently identified two Person
    records as the same real person. Reuses app.duplicate_consolidation's vetted
    single-mapping execution (_execute_one_mapping) exactly as-is -- same
    calendar-safety gate (refuses the whole merge if a calendar action already has
    external attendee data for either person), same collections repointed (threads,
    commitments, meetings, follow_ups, projects, knowledge_items, reply_drafts,
    calendar_actions), same `merged_into` lineage every other Person-merge path uses
    (app.entities.lifecycle.is_person_merged).

    Unlike that module's full Phase 18 workflow (generate_merge_plan -> human
    review -> approved plan file -> execute_merge_plan), this tool executes one,
    explicitly-named pair immediately -- no plan file, no separate approval step.
    generate_merge_plan's classifier only catches candidates with matching
    email/org signals; it does NOT catch every real duplicate (confirmed directly
    against production data: two "Ashok" records with no email at all, and the same
    real person under two unrelated email addresses, neither surfaced by
    preview_duplicate_person_candidates). This tool exists to close exactly that
    gap -- it trades generate_merge_plan's automatic review for the caller's own
    judgment, so call it only when genuinely confident; there is no second check.

    Also repoints emails.entities_referenced.people -- a real gap in
    _execute_one_mapping itself (it never touches the emails collection at all),
    left uncorrected there since it's only ever invoked through the slower,
    reviewed plan path. Closed here because the dashboard's email lookup box
    resolves people directly from this field, and a stale duplicate id there
    would keep showing the retired record after a merge.

    Raises ValueError if either id doesn't exist, if they're the same id, or if
    duplicate_person_id is already merged into someone else -- never silently
    re-merges or fabricates a person. Returns {status, updated_counts,
    duplicate_person_id, canonical_person_id}; if the calendar-safety gate blocks
    the merge, returns _execute_one_mapping's own {status: "BLOCKED", reason,
    detail} unchanged and touches nothing.
    """
    person_repo = PersonRepository(db)
    duplicate = person_repo.find_one({"id": duplicate_person_id})
    canonical = person_repo.find_one({"id": canonical_person_id})
    if duplicate is None:
        raise ValueError(f"no person found for duplicate_person_id={duplicate_person_id!r}")
    if canonical is None:
        raise ValueError(f"no person found for canonical_person_id={canonical_person_id!r}")
    if duplicate_person_id == canonical_person_id:
        raise ValueError("duplicate_person_id and canonical_person_id must be different")
    if duplicate.get("status") == "merged":
        raise ValueError(
            f"person_id={duplicate_person_id!r} is already merged into {duplicate.get('merged_into')!r}"
        )

    result = _execute_one_mapping(
        db, {"duplicate_person_id": duplicate_person_id, "canonical_person_id": canonical_person_id}
    )
    if result["status"] != "COMPLETED":
        return result

    updated_counts = dict(result["updated_counts"])
    email_repo = EmailRepository(db)
    emails_updated = 0
    for email in email_repo.find_many({"entities_referenced.people": duplicate_person_id}):
        people = email.get("entities_referenced", {}).get("people", [])
        new_people = list(dict.fromkeys(canonical_person_id if p == duplicate_person_id else p for p in people))
        email_repo.upsert_by_key(
            {"message_id": email["message_id"]},
            {"entities_referenced": {**email["entities_referenced"], "people": new_people}},
        )
        emails_updated += 1
    if emails_updated:
        updated_counts["emails"] = emails_updated

    person_repo.upsert_by_key(
        {"id": duplicate_person_id},
        {
            "merge_reason": reason,
            "merged_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    return {
        "status": "COMPLETED",
        "updated_counts": updated_counts,
        "duplicate_person_id": duplicate_person_id,
        "canonical_person_id": canonical_person_id,
    }


def ask_question(db: Database, settings: Settings, text: str, timezone_name: str | None = None) -> dict[str, Any]:
    """BRD gap-analysis FR-01: the MCP-reachable entry point into the existing,
    independently-tested query engine (app.query.service.execute_query) --
    previously fully built and tested but with zero callers outside its own test
    suite. Deterministic and strictly read-only: never constructs an LLM client
    and never passes llm_classify, so intent classification stays Stage-1-only
    (app.query.intent.classify_intent_with_fallback's deterministic fixed-phrase
    rules) -- no LLM call happens anywhere in this tool. Returns execute_query's
    own structured QueryResult verbatim; this tool deliberately never invokes
    app.query.synthesis (LLM prose generation) -- the calling client's own model
    can phrase a natural-language answer from this structured evidence itself.

    reference_datetime is always "now" (this is a live question asked right now,
    not a historical replay), so it is not a caller-supplied parameter.
    """
    request = QueryRequest(
        text=text,
        reference_datetime=datetime.now(timezone.utc),
        timezone=timezone_name or settings.timezone,
    )
    result = execute_query(db, request, agent_email=settings.agent_email)
    return result.model_dump(mode="json")


def whats_on_my_table(db: Database, settings: Settings) -> dict[str, Any]:
    """BRD gap-analysis FR-02: one read-only call combining the categories an
    executive actually needs surfaced, each still reusing an existing repository/
    query-layer primitive -- no category invents data beyond what that primitive
    already returns, and no category's failure can affect another's (each is
    computed independently). Every key is always present, even when its list is
    empty, so a caller never has to guess whether an empty category was omitted
    versus genuinely empty.
    """
    now = datetime.now(timezone.utc)
    tz = settings.timezone
    overdue = resolve_date_range(DateRangeKind.OVERDUE, now, tz)
    upcoming = resolve_date_range(DateRangeKind.UPCOMING, now, tz)

    pending_replies = list_reply_drafts(db, status="awaiting_approval")
    overdue_follow_ups = retrieval.retrieve_follow_ups(db, date_range=overdue)
    commitments_due = retrieval.retrieve_commitments(db, date_range=upcoming)
    upcoming_meetings = retrieval.retrieve_meetings(db, date_range=upcoming)
    active_projects = list_projects(db)

    return {
        "pending_replies": pending_replies,
        "overdue_follow_ups": overdue_follow_ups,
        "commitments_due": commitments_due,
        "upcoming_meetings": upcoming_meetings,
        "active_projects": active_projects,
    }
