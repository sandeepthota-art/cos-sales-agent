import re
import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from pymongo.database import Database

from app.analysis.extractor import analyze_email_with_validation
from app.analysis.schemas import EmailAnalysis
from app.calendar.actions import build_calendar_action
from app.calendar.detector import detect_meeting
from app.config.settings import Settings
from app.context.engine import build_next_context
from app.context.models import ThreadContext
from app.database.repositories import (
    CalendarActionRepository,
    ContextSnapshotRepository,
    EmailRepository,
    KnowledgeRepository,
    MeetingRepository,
    OrganizationRepository,
    PersonRepository,
    ProcessingRunRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
from app.email.models import Email, parse_email
from app.email.normalizer import normalize_email, normalize_subject
from app.email.threading import ThreadCandidate, resolve_thread_id
from app.entities.dates import classify_follow_up_timing, resolve_date_phrase
from app.entities.extraction import envelope_people
from app.entities.resolution import (
    derive_follow_up,
    match_resolved_person_by_name,
    resolve_canonical_person_for_email,
    resolve_commitment,
    resolve_meeting,
    resolve_operator_person,
    resolve_person,
    resolve_personal_item,
    resolve_project,
)
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.email_provider import EmailProvider
from app.interfaces.llm_provider import LLMProvider
from app.knowledge.deduplication import process_new_fact
from app.knowledge.models import KnowledgeItem
from app.knowledge.normalize import normalize_text
from app.processing.models import EmailResult, PipelineRunSummary, ProcessingStage
from app.replies.drafter import draft_reply, needs_reply
from app.replies.models import ReplyDraft

_GMAIL_INTERNAL_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")

_FACT_FIELD_PREDICATES = {
    "requirements": "requires",
    "pain_points": "has_pain_point",
    "competitors": "mentioned_competitor",
    "objections": "raised_objection",
    "buying_signals": "showed_buying_signal",
}

# BRD 6.3: of the four commitment classes, only "mine" (he committed to something) and
# "owed_to_me" (someone committed to him) are chased. "theirs" (two other parties agreed;
# he was copied) is tracked but never chased, and "recap" (a restatement of something
# already known) is logged with no action -- neither should ever produce a FollowUp.
_CHASED_COMMITMENT_CLASSES = {"mine", "owed_to_me"}


def _load_thread_candidates(thread_repo: ThreadRepository) -> list[ThreadCandidate]:
    candidates = []
    for doc in thread_repo.find_many({}):
        candidates.append(
            ThreadCandidate(
                thread_id=doc["thread_id"],
                normalized_subject=doc["normalized_subject"],
                participant_emails=set(doc["participant_emails"]),
                message_ids=set(doc["message_ids"]),
                last_message_at=datetime.fromisoformat(doc["last_message_at"]),
            )
        )
    return candidates


def _upsert_thread(thread_repo: ThreadRepository, thread_id: str, email: Email) -> None:
    existing = thread_repo.find_one({"thread_id": thread_id})
    participant_emails = set(existing["participant_emails"]) if existing else set()
    message_ids = set(existing["message_ids"]) if existing else set()

    participant_emails |= {email.from_.email, *(a.email for a in email.to), *(a.email for a in email.cc)}
    message_ids.add(email.message_id)

    last_message_at = email.timestamp
    if existing:
        existing_last = datetime.fromisoformat(existing["last_message_at"])
        last_message_at = max(last_message_at, existing_last)

    thread_repo.upsert_by_key(
        {"thread_id": thread_id},
        {
            "thread_id": thread_id,
            "normalized_subject": existing["normalized_subject"] if existing else normalize_subject(email.subject),
            "participant_emails": sorted(participant_emails),
            "message_ids": sorted(message_ids),
            "last_message_at": last_message_at.isoformat(),
        },
    )


def ingest_raw_email(email_repo: EmailRepository, email: Email) -> tuple[Email, bool]:
    """Normalizes the email and applies the existing message_id + COMPLETED duplicate
    guard; if not a duplicate, persists the raw email and advances it through
    RECEIVED/VALIDATED. Returns (normalized_email, already_completed).

    Shared by run_pipeline's per-email loop and app.mcp.tools.ingest_email, so the
    exact same duplicate-detection/raw-persistence behavior is never duplicated
    between the two entry points.
    """
    email = normalize_email(email)

    existing = email_repo.find_one({"message_id": email.message_id})
    if existing and existing.get("processing_status", {}).get("stage") == ProcessingStage.COMPLETED.value:
        return email, True

    email_repo.upsert_by_key(
        {"message_id": email.message_id}, email.model_dump(mode="json", by_alias=True)
    )
    email_repo.set_stage(email.message_id, ProcessingStage.RECEIVED.value)
    email_repo.set_stage(email.message_id, ProcessingStage.VALIDATED.value)
    return email, False


def resolve_and_persist_thread(thread_repo: ThreadRepository, email_repo: EmailRepository, email: Email) -> str:
    """Resolves/upserts the thread this email belongs to and advances it to THREADED.

    Shared by run_pipeline and app.mcp.tools.ingest_email.
    """
    candidates = _load_thread_candidates(thread_repo)
    thread_id = resolve_thread_id(email, candidates)
    _upsert_thread(thread_repo, thread_id, email)
    email_repo.set_stage(email.message_id, ProcessingStage.THREADED.value)
    return thread_id


def _process_knowledge(
    knowledge_repo: KnowledgeRepository,
    thread_id: str,
    analysis: EmailAnalysis,
    llm: LLMProvider,
    subject_name: str,
    source_email_id: str,
    now: datetime,
) -> None:
    stored_docs = knowledge_repo.all_for_thread(thread_id)
    items = [KnowledgeItem.model_validate(doc) for doc in stored_docs]

    for fact in analysis.facts:
        items, item = process_new_fact(
            items, thread_id, fact.subject, fact.predicate, fact.object, source_email_id, "stated", llm, now
        )
        knowledge_repo.upsert_by_key(
            {
                "thread_id": item.thread_id,
                "subject_key": item.subject_key,
                "predicate": item.predicate,
                "fact_key": item.fact_key,
            },
            item.model_dump(mode="json"),
        )

    for field, predicate in _FACT_FIELD_PREDICATES.items():
        for value in getattr(analysis, field):
            items, item = process_new_fact(
                items, thread_id, subject_name, predicate, value, source_email_id, "stated", llm, now
            )
            knowledge_repo.upsert_by_key(
                {
                    "thread_id": item.thread_id,
                    "subject_key": item.subject_key,
                    "predicate": item.predicate,
                    "fact_key": item.fact_key,
                },
                item.model_dump(mode="json"),
            )


def _process_entities(
    db: Database,
    thread_id: str,
    email: Email,
    analysis: EmailAnalysis,
    reference_now: datetime,
    agent_email: str,
    agent_name: str | None = None,
) -> dict[str, list[str]]:
    # reference_now is the email's OWN timestamp, not wall-clock "now" -- this matches the
    # existing detect_meeting's established pattern (app/calendar/detector.py, called with
    # email.timestamp) so a re-run days later resolves the same relative phrase the same way.
    entities_referenced: dict[str, list[str]] = {
        "people": [], "projects": [], "commitments": [],
        "follow_ups": [], "meetings": [], "personal": [],
    }

    envelope = {addr.email.lower(): addr for addr in envelope_people(email)}
    sender_email = email.from_.email.lower()
    agent_email_normalized = agent_email.lower()
    # Used only to classify a chased commitment's counterparty as "internal" (same
    # domain as the agent's own mailbox) vs "client" (BRD 6.4) -- never used to create
    # or resolve any Person/Organization.
    agent_email_domain = agent_email_normalized.split("@")[-1] if "@" in agent_email_normalized else None
    # None (not just falsy) when unconfigured -- keeps the operator-recognition check
    # below inert with zero configuration, matching agent_name's own None default.
    agent_name_tokens = _name_tokens_pipeline(agent_name) if agent_name else None
    # Set only at the operator Person's creation (see resolve_operator_person) --
    # never overwrites an existing operator profile's own name on later calls, same
    # contract as resolve_person's own email branch.
    operator_display_name = f"{agent_name} (Me)" if agent_name else "Me"

    person_repo = PersonRepository(db)
    # Every Person actually resolved for THIS email (envelope + LLM mentions), as full
    # documents -- not just IDs -- so commitments/meetings/projects below can match a
    # free-text name (owed_by, an attendee, ...) against a real, already-canonical
    # Person from this same email via match_resolved_person_by_name, rather than a
    # fresh, wider (and riskier) scan of the whole people collection.
    resolved_people: list[dict[str, Any]] = []

    # Deterministic envelope-based resolution: every real address in From/To/CC gets a
    # Person record and its last_inbound/last_outbound touched -- independent of
    # whether the LLM's own people_mentioned happened to include them. This is
    # additive to (not a replacement for) the LLM-mention loop below: resolve_person
    # is idempotent by email address, so an address covered by BOTH this loop and the
    # LLM's own mention resolves to the exact same Person id both times, never a
    # duplicate. The operator's own mailbox resolves to their dedicated operator
    # profile (resolve_operator_person) instead of an ordinary Person -- tracked, but
    # never indistinguishable from a real external contact.
    for envelope_email, addr in envelope.items():
        if envelope_email == agent_email_normalized:
            person_id = resolve_operator_person(
                db, agent_email, operator_display_name,
                is_sender=envelope_email == sender_email,
                now=reference_now,
                thread_id=thread_id,
            )
        else:
            person_id = resolve_person(
                db,
                {"name": addr.name, "email": addr.email, "org": None},
                is_sender=envelope_email == sender_email,
                now=reference_now,
                thread_id=thread_id,
            )
        entities_referenced["people"].append(person_id)
        resolved_people.append(person_repo.find_one({"id": person_id}))

    for mention in analysis.people_mentioned:
        mention_email = (mention.email or "").lower() or None

        # Recognizes the operator either by email (a mention that happens to carry
        # the operator's own address) or, when the mention has no email of its own
        # (e.g. a calendar invite's body text naming the operator as an attendee), by
        # agent_name -- ties the mention to the SAME dedicated operator profile the
        # envelope loop above resolves, rather than falling into the ordinary
        # same-email-match/no-email tiers below (which are for external contacts).
        is_operator_mention = mention_email == agent_email_normalized
        if not is_operator_mention and not mention_email and agent_name_tokens:
            mention_name_tokens = _name_tokens_pipeline(mention.name)
            is_operator_mention = bool(mention_name_tokens) and (
                mention_name_tokens <= agent_name_tokens or agent_name_tokens <= mention_name_tokens
            )

        is_sender = None
        if mention_email and mention_email == sender_email:
            is_sender = True
        elif mention_email and mention_email in envelope:
            is_sender = False

        if is_operator_mention:
            person_id = resolve_operator_person(
                db, agent_email, operator_display_name, is_sender=is_sender,
                now=reference_now, thread_id=thread_id,
            )
            entities_referenced["people"].append(person_id)
            resolved_people.append(person_repo.find_one({"id": person_id}))
            continue

        # Context-aware reuse -- NOT a relaxation of resolve_person's own global
        # "never merge on name alone" rule. A mention with no email of its own
        # (e.g. a calendar invite's body text repeating a name already present in
        # the real To/CC headers) is checked first against people ALREADY resolved
        # for THIS SAME email (envelope address matches, or an earlier mention in
        # this same loop) -- never a fresh, wider scan of the whole people
        # collection. Anyone already in resolved_people was resolved for this
        # exact thread_id moments ago in this same call, so no further write is
        # needed here; a no-email mention with no such match still falls through
        # to resolve_person's existing (unchanged) no-email tier below.
        same_email_match = (
            match_resolved_person_by_name(resolved_people, mention.name) if not mention_email else None
        )
        if same_email_match is not None:
            person_id = same_email_match["id"]
        else:
            person_id = resolve_person(
                db,
                {"name": mention.name, "email": mention.email, "org": mention.org},
                is_sender=is_sender,
                now=reference_now,
                thread_id=thread_id,
            )
        entities_referenced["people"].append(person_id)
        resolved_people.append(same_email_match or person_repo.find_one({"id": person_id}))

    # org_id -> [project_id, ...], built only from projects actually resolved for THIS
    # email (never a fresh collection-wide scan) -- reused below to link a commitment to
    # a project via its counterparty's org_id. A list, not a single value: an org_id
    # with more than one project mentioned in the same email is genuine ambiguity (which
    # one does the commitment belong to?) and must never be resolved by "whichever was
    # processed last" -- see the single-candidate check where this is consumed below. A
    # project with no org_id is never added here, so a commitment can never be linked
    # through a missing key.
    project_ids_by_org_id: dict[str, list[str]] = {}

    for mention in analysis.projects_mentioned:
        # person_ids/org_id: every Person already resolved for this email whose org
        # matches this project's entity -- never inferred from the project/entity name
        # text alone (see resolve_organization's own domain-only rule).
        entity_normalized = normalize_text(mention.org or "")
        matching_people = [
            p for p in resolved_people if entity_normalized and normalize_text(p.get("org") or "") == entity_normalized
        ]
        project_org_id = next((p["org_id"] for p in matching_people if p.get("org_id")), None)
        project_id = resolve_project(
            db,
            {"name": mention.name, "org": mention.org},
            goal_pillar=analysis.goal_pillar,
            person_ids=list(dict.fromkeys(p["id"] for p in matching_people)),
            org_id=project_org_id,
        )
        entities_referenced["projects"].append(project_id)
        if project_org_id:
            project_ids_by_org_id.setdefault(project_org_id, [])
            if project_id not in project_ids_by_org_id[project_org_id]:
                project_ids_by_org_id[project_org_id].append(project_id)

    # FollowUps are derived ONLY from a resolved Commitment (spec S5.1.1 correction) --
    # a Meeting or PersonalItem NEVER triggers a FollowUp by itself, no matter how
    # "actionable" the meeting is. Do not add a fallback branch here.
    for raw_commitment in analysis.commitments_mentioned:
        resolved_date, date_type = resolve_date_phrase(raw_commitment.date_phrase, reference_now)
        # Best-effort canonical match against this email's own already-resolved people
        # (never a fresh, wider scan) -- try the counterparty field first (owed_to for
        # a commitment WE owe, owed_by for one owed TO us), falling back to the other;
        # a generic placeholder ("Sender", "Me", ...) simply won't match anyone real,
        # which is the correct outcome, not a special case to detect.
        commitment_person = match_resolved_person_by_name(
            resolved_people, raw_commitment.owed_to
        ) or match_resolved_person_by_name(resolved_people, raw_commitment.owed_by)
        # project_id: linked only when the commitment's own counterparty's org_id
        # matches EXACTLY ONE project already resolved for THIS SAME email
        # (project_ids_by_org_id above) -- never a fresh scan, never a guess. No
        # counterparty/org_id, no project mentioned for that org, OR more than one
        # project mentioned for that same org (genuine ambiguity -- which one does this
        # commitment belong to?) all leave it unset rather than inventing/guessing a
        # project. The current extraction schema (RawCommitment) carries no explicit
        # project reference of its own, so org-match is the only evidence available;
        # when that evidence is ambiguous, None is the only honest answer.
        commitment_org_id = commitment_person.get("org_id") if commitment_person else None
        org_candidate_project_ids = project_ids_by_org_id.get(commitment_org_id, []) if commitment_org_id else []
        commitment_project_id = org_candidate_project_ids[0] if len(org_candidate_project_ids) == 1 else None
        commitment_id = resolve_commitment(
            db,
            thread_id=thread_id,
            raw=raw_commitment.model_dump(mode="json", by_alias=True),
            message_id=email.message_id,
            made_on=reference_now,
            resolved_date=resolved_date,
            date_type=date_type,
            goal_pillar=analysis.goal_pillar,
            project_id=commitment_project_id,
            person_id=commitment_person["id"] if commitment_person else None,
            org_id=commitment_org_id,
        )
        entities_referenced["commitments"].append(commitment_id)
        # BRD 6.3: only "mine"/"owed_to_me" commitments are chased -- "theirs" and
        # "recap" are tracked via the Commitment record above but must never generate a
        # FollowUp. The Commitment itself is always persisted regardless of class.
        if raw_commitment.commitment_class in _CHASED_COMMITMENT_CLASSES:
            # BRD 6.4's audience/timing classification only ever applies to "owed_to_me"
            # (chasing someone else) -- classify_follow_up_timing itself no-ops for
            # "mine" (see its docstring), so this is safe to compute unconditionally for
            # every chased class without a separate branch here.
            is_internal_counterparty = False
            if commitment_org_id and agent_email_domain:
                counterparty_org = OrganizationRepository(db).find_one({"id": commitment_org_id})
                is_internal_counterparty = bool(
                    counterparty_org and counterparty_org.get("domain") == agent_email_domain
                )
            audience, follow_up_earliest_at, follow_up_latest_at = classify_follow_up_timing(
                commitment_class=raw_commitment.commitment_class,
                date_type=date_type,
                committed_date=resolved_date,
                is_internal_counterparty=is_internal_counterparty,
            )
            # thread_id (this email's resolved thread, in scope from _process_entities'
            # own parameter) is now carried onto the derived FollowUp -- see
            # app.entities.resolution.derive_follow_up. person_id/org_id are inherited
            # directly from the commitment just resolved above, never re-inferred.
            follow_up_id = derive_follow_up(
                db,
                commitment_id=commitment_id,
                thread_id=thread_id,
                person_id=commitment_person["id"] if commitment_person else None,
                org_id=commitment_org_id,
                audience=audience,
                follow_up_earliest_at=follow_up_earliest_at,
                follow_up_latest_at=follow_up_latest_at,
            )
            entities_referenced["follow_ups"].append(follow_up_id)

    for raw_meeting in analysis.meetings_mentioned:
        resolved_date, _ = resolve_date_phrase(raw_meeting.date_phrase, reference_now)
        # The actionable signal (spec S4.6): true for any future-oriented meeting mention,
        # even a vague one with no precise resolved_date -- false only when the LLM (or,
        # for MockLLMProvider, the simple absence of a "meet" match on past-tense "met")
        # flagged it as historical.
        actionable = not raw_meeting.is_past
        # Match every attendee name against this email's own already-resolved people --
        # unmatched attendees (a generic "Sender", a name not otherwise resolved this
        # email) are simply not included, never guessed.
        attendee_people = [
            match_resolved_person_by_name(resolved_people, attendee) for attendee in raw_meeting.attendees
        ]
        attendee_people = [p for p in attendee_people if p is not None]
        meeting_org_id = next((p["org_id"] for p in attendee_people if p.get("org_id")), None)
        meeting_id = resolve_meeting(
            db,
            thread_id=thread_id,
            date=resolved_date,
            raw=raw_meeting.model_dump(mode="json"),
            actionable=actionable,
            person_ids=list(dict.fromkeys(p["id"] for p in attendee_people)),
            org_id=meeting_org_id,
        )
        entities_referenced["meetings"].append(meeting_id)

    for raw_item in analysis.personal_items_mentioned:
        resolved_date, _ = resolve_date_phrase(raw_item.date_phrase, reference_now)
        item_id = resolve_personal_item(
            db, sender_email=sender_email, raw=raw_item.model_dump(mode="json"), resolved_date=resolved_date
        )
        entities_referenced["personal"].append(item_id)

    return {key: list(dict.fromkeys(ids)) for key, ids in entities_referenced.items()}


def _thread_resolved_people(db: Database, thread_id: str) -> list[dict[str, Any]]:
    """Every Person whose open_threads includes this thread -- the authoritative,
    already-canonical set (Person.open_threads is itself only ever grown, never
    invented), reusable by anything that needs to link OTHER entities in this thread
    to canonical people/orgs after _process_entities has already run.
    """
    return PersonRepository(db).find_many({"open_threads": thread_id})


def _link_thread_to_entities(db: Database, thread_id: str) -> None:
    """Part 9 of the global-identity design: a thread should expose person_ids/org_ids
    without re-parsing names. Always recomputed fresh from the current, authoritative
    Person records for this thread (never accumulated/drifting) -- correct even if a
    Person's own org_id was itself only backfilled after this thread was first seen.
    """
    people = _thread_resolved_people(db, thread_id)
    person_ids = sorted({p["id"] for p in people})
    org_ids = sorted({p["org_id"] for p in people if p.get("org_id")})
    ThreadRepository(db).upsert_by_key({"thread_id": thread_id}, {"person_ids": person_ids, "org_ids": org_ids})


def _link_knowledge_to_entities(db: Database, thread_id: str, message_id: str) -> None:
    """Part 6 of the global-identity design: a knowledge item's subject is matched
    against this thread's already-resolved people (person_id) and, failing that, their
    organizations (org_id) -- e.g. "Ashok prefers morning meetings" links to PER-391,
    while "DataBeat has 500 employees" links to DataBeat's ORG-xxx instead. A
    thread-level fact (subject_key derived from the thread_id itself, not a real name --
    see _process_knowledge's requirements/pain_points/etc. loop) matches neither, and
    correctly stays without either reference: not every knowledge item is about a
    specific person or company.
    """
    people = _thread_resolved_people(db, thread_id)
    if not people:
        return
    org_ids = {p["org_id"] for p in people if p.get("org_id")}
    organizations = OrganizationRepository(db).find_many({"id": {"$in": list(org_ids)}}) if org_ids else []

    for item in db.knowledge_items.find(
        {"thread_id": thread_id, "source_emails": message_id}, {"_id": 0, "knowledge_id": 1, "subject_key": 1}
    ):
        if item.get("person_id") or item.get("org_id"):
            continue  # already linked by an earlier email in this thread -- never overwrite
        subject_tokens = set(item["subject_key"].split("_"))
        person_match = next(
            (p for p in people if subject_tokens <= _name_tokens_pipeline(p["name"])
             or _name_tokens_pipeline(p["name"]) <= subject_tokens),
            None,
        )
        update: dict[str, Any] = {}
        if person_match is not None:
            update["person_id"] = person_match["id"]
        else:
            org_match = next(
                (o for o in organizations if subject_tokens <= _name_tokens_pipeline(o["name"])
                 or _name_tokens_pipeline(o["name"]) <= subject_tokens),
                None,
            )
            if org_match is not None:
                update["org_id"] = org_match["id"]
        if update:
            KnowledgeRepository(db).upsert_by_key({"knowledge_id": item["knowledge_id"]}, update)


def _name_tokens_pipeline(name: str) -> set[str]:
    return set(normalize_text(name).split())


def run_pipeline(
    db: Database,
    email_provider: EmailProvider,
    llm_provider: LLMProvider,
    calendar_provider: CalendarProvider,
    settings: Settings,
) -> PipelineRunSummary:
    email_repo = EmailRepository(db)
    thread_repo = ThreadRepository(db)
    context_repo = ContextSnapshotRepository(db)
    knowledge_repo = KnowledgeRepository(db)
    reply_repo = ReplyDraftRepository(db)
    calendar_repo = CalendarActionRepository(db)
    run_repo = ProcessingRunRepository(db)

    run_id = f"run_{uuid.uuid4().hex}"
    started_at = datetime.now(timezone.utc)
    results: list[EmailResult] = []

    raw_emails = email_provider.fetch_emails(limit=settings.email_limit)

    for raw in raw_emails:
        message_id = raw.get("message_id") if isinstance(raw, dict) else None
        try:
            email = parse_email(raw)
        except ValidationError as exc:
            results.append(EmailResult(message_id=message_id, final_stage="FAILED", error=str(exc)))
            if message_id:
                email_repo.set_stage(
                    message_id,
                    ProcessingStage.FAILED.value,
                    error=str(exc),
                    failed_stage=ProcessingStage.VALIDATED.value,
                )
            continue

        email, already_completed = ingest_raw_email(email_repo, email)
        if already_completed:
            results.append(EmailResult(message_id=email.message_id, final_stage="SKIPPED"))
            continue

        # Everything below calls out to LLM/provider code and mutates several
        # collections across multiple stages. Any unexpected exception here
        # (timeouts, transport errors, ...) must not crash the whole batch --
        # it is recorded as a FAILED result at whatever stage was in flight,
        # and the loop moves on to the next email.
        current_stage = ProcessingStage.THREADED
        try:
            thread_id = resolve_and_persist_thread(thread_repo, email_repo, email)

            current_stage = ProcessingStage.ANALYZED
            outcome = analyze_email_with_validation(llm_provider, email)
            if not outcome.success:
                email_repo.set_stage(
                    email.message_id,
                    ProcessingStage.FAILED.value,
                    error=outcome.error,
                    failed_stage=ProcessingStage.ANALYZED.value,
                )
                results.append(EmailResult(message_id=email.message_id, final_stage="FAILED", error=outcome.error))
                continue
            analysis = outcome.analysis
            email_repo.set_stage(email.message_id, ProcessingStage.ANALYZED.value)

            current_stage = ProcessingStage.CONTEXT_BUILT
            existing_snapshot = context_repo.find_one(
                {"thread_id": thread_id, "triggering_email_id": email.message_id}
            )
            if existing_snapshot:
                next_context = ThreadContext.model_validate(existing_snapshot["context"])
            else:
                previous_snapshot = context_repo.latest_for_thread(thread_id)
                previous_context = (
                    ThreadContext.model_validate(previous_snapshot["context"]) if previous_snapshot else None
                )
                next_context, changes = build_next_context(
                    previous_context, analysis, email.message_id, llm_provider
                )
                next_version = (previous_snapshot["context_version"] + 1) if previous_snapshot else 1
                context_repo.upsert_by_key(
                    {"thread_id": thread_id, "triggering_email_id": email.message_id},
                    {
                        "thread_id": thread_id,
                        "context_version": next_version,
                        "triggering_email_id": email.message_id,
                        "context": next_context.model_dump(mode="json"),
                        "changes_from_previous_context": [c.model_dump(mode="json") for c in changes],
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            email_repo.set_stage(email.message_id, ProcessingStage.CONTEXT_BUILT.value)

            current_stage = ProcessingStage.KNOWLEDGE_PROCESSED
            _process_knowledge(
                knowledge_repo,
                thread_id,
                analysis,
                llm_provider,
                thread_id,
                email.message_id,
                datetime.now(timezone.utc),
            )
            email_repo.set_stage(email.message_id, ProcessingStage.KNOWLEDGE_PROCESSED.value)

            current_stage = ProcessingStage.ENTITIES_PROCESSED
            # email.timestamp, not datetime.now() -- matches detect_meeting's existing
            # reference-date pattern, so relative phrases resolve consistently regardless
            # of when the pipeline actually runs.
            entities_referenced = _process_entities(
                db, thread_id, email, analysis, email.timestamp, settings.agent_email, settings.agent_name
            )
            # Additive linking passes, run AFTER entity resolution so both have the
            # complete, up-to-date set of canonical people/orgs for this thread to work
            # from -- deliberately separate steps rather than reordering the existing
            # KNOWLEDGE_PROCESSED/ENTITIES_PROCESSED stage sequence above.
            _link_knowledge_to_entities(db, thread_id, email.message_id)
            _link_thread_to_entities(db, thread_id)
            source_link = (
                f"https://mail.google.com/mail/u/0/#all/{email.message_id}"
                if _GMAIL_INTERNAL_ID_PATTERN.match(email.message_id)
                else None
            )
            email_repo.set_entity_metadata(
                message_id=email.message_id,
                record_id=email.message_id,
                source_type="gmail",
                source_link=source_link,
                date=email.timestamp.date().isoformat(),
                entities_referenced=entities_referenced,
                goal_pillar=analysis.goal_pillar,
                label_applied=analysis.label_applied,
                confidence=analysis.confidence,
                priority=analysis.priority,
            )
            email_repo.set_stage(email.message_id, ProcessingStage.ENTITIES_PROCESSED.value)

            current_stage = ProcessingStage.REPLY_PROCESSED
            # email.from_.email is already lowercased by normalize_email; lowercase
            # settings.agent_email too so the comparison is case-insensitive regardless of
            # how the operator wrote it in .env. An email FROM our own mailbox (e.g. the
            # sales rep's own outbound message in a two-sided demo thread) must never get a
            # reply draft generated for it.
            is_from_agent = email.from_.email.lower() == settings.agent_email.lower()
            # The canonical Person/Organization this email's sender resolves to -- via
            # resolve_canonical_person_for_email (Phase 20.1), NOT a raw email lookup,
            # so a reply draft or calendar action about this email stays connected to
            # that same PER-xxx/ORG-xxx even if the sender's address historically
            # belonged to a Person since consolidated into another one.
            sender_person = resolve_canonical_person_for_email(db, email.from_.email)
            sender_person_id = sender_person["id"] if sender_person else None
            sender_org_id = sender_person.get("org_id") if sender_person else None
            if not is_from_agent and needs_reply(analysis, email):
                reply_key = {"source_email_id": email.message_id}
                # A reply draft may already exist for this email (e.g. a human
                # already approved/edited/sent it after an earlier partial run).
                # Never blind-overwrite it -- only create it the first time.
                if reply_repo.find_one(reply_key) is None:
                    draft_content = draft_reply(llm_provider, next_context, email)
                    draft = ReplyDraft(
                        reply_id=f"reply_{email.message_id}",
                        thread_id=thread_id,
                        source_email_id=email.message_id,
                        status="awaiting_approval",
                        draft=draft_content,
                        person_id=sender_person_id,
                        org_id=sender_org_id,
                        created_at=datetime.now(timezone.utc),
                    )
                    reply_repo.upsert_by_key(reply_key, draft.model_dump(mode="json"))
            email_repo.set_stage(email.message_id, ProcessingStage.REPLY_PROCESSED.value)

            current_stage = ProcessingStage.MEETING_PROCESSED
            detection = detect_meeting(email, thread_id, settings.timezone, email.timestamp)
            action = build_calendar_action(detection, thread_id, reference_now=email.timestamp)
            if action is not None:
                # meeting_id: linked only when it's unambiguous. A single LLM-extracted
                # meeting for this email is linked outright. With more than one, the
                # only real evidence connecting this calendar action to a SPECIFIC one
                # of them is a matching date -- action.event.start is this detection's
                # own independently-parsed date (never the needs_clarification
                # placeholder, which is just "now" and proves nothing). Exactly one
                # same-date candidate is required; zero or several sharing that date is
                # genuine ambiguity, left unset rather than guessed. Never "the first
                # meeting in the list" -- that was the bug being fixed here.
                meeting_ids = entities_referenced.get("meetings", [])
                if len(meeting_ids) == 1:
                    matched_meeting_id = meeting_ids[0]
                elif len(meeting_ids) > 1 and detection.meeting_detected:
                    candidates = [
                        m for m in MeetingRepository(db).find_many({"id": {"$in": meeting_ids}})
                        if m.get("date") and datetime.fromisoformat(m["date"]).date() == action.event.start.date()
                    ]
                    matched_meeting_id = candidates[0]["id"] if len(candidates) == 1 else None
                else:
                    matched_meeting_id = None
                # Metadata about who/what this action concerns -- entirely separate
                # from action.event.attendees, which stays [] exactly as before; this
                # never touches the calendar-safety validator.
                action = action.model_copy(
                    update={
                        "person_id": sender_person_id,
                        "org_id": sender_org_id,
                        "meeting_id": matched_meeting_id,
                    }
                )
                calendar_key = {
                    "thread_id": action.thread_id,
                    "meeting_fingerprint": action.meeting_fingerprint,
                }
                # Same guard as reply_drafts above: a calendar action for this
                # key may already have been approved/scheduled by a human, and
                # must never be blind-overwritten by a later run.
                if calendar_repo.find_one(calendar_key) is None:
                    calendar_repo.upsert_by_key(calendar_key, action.model_dump(mode="json"))
            email_repo.set_stage(email.message_id, ProcessingStage.MEETING_PROCESSED.value)

            email_repo.set_stage(email.message_id, ProcessingStage.COMPLETED.value)
            results.append(EmailResult(message_id=email.message_id, final_stage="COMPLETED"))
        except Exception as exc:  # noqa: BLE001 - deliberately broad: any provider/stage failure must not crash the batch
            error_detail = f"{type(exc).__name__}: {exc}"
            email_repo.set_stage(
                email.message_id,
                ProcessingStage.FAILED.value,
                error=error_detail,
                failed_stage=current_stage.value,
            )
            results.append(EmailResult(message_id=email.message_id, final_stage="FAILED", error=error_detail))
            continue

    completed_at = datetime.now(timezone.utc)
    summary = PipelineRunSummary(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        processed=len(raw_emails),
        completed=sum(1 for r in results if r.final_stage == "COMPLETED"),
        failed=sum(1 for r in results if r.final_stage == "FAILED"),
        skipped=sum(1 for r in results if r.final_stage == "SKIPPED"),
        results=results,
    )
    run_repo.upsert_by_key({"run_id": run_id}, summary.model_dump(mode="json"))
    return summary
