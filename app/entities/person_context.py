"""Person Context Snapshots -- Layer 2 of the three-layer person-memory design
(Standing Person / Person Context Snapshots / Knowledge Items).

This module deliberately does NOT duplicate app.entities.context.get_person_context
(the full, unbounded 360-degree view used by the dashboard/MCP tools) and does NOT
duplicate app.knowledge.deduplication's fact-conflict handling (KnowledgeItem's own
current_value/history already IS the conflict-history mechanism -- a Person Context
Snapshot only ever REFERENCES a knowledge_id, it never stores a competing copy of a
fact's value or its own history).

What this module adds: a per-(person, email) incremental record of what THIS
specific email's processing actually established for that person -- the complete
outputs of app.pipeline._process_entities (commitments, meetings, follow-ups,
projects, opportunities, personal items) plus buying/sales-signal interpretation
from the email's own analysis, not merely a dump of KnowledgeItems.

Provenance vocabulary: "observed" | "reconstructed" | "inferred" (see
PersonContextEntry.provenance). This is a SEPARATE axis from KnowledgeItem's own
"stated"/"inferred" basis (never renamed or conflated with it -- exactly the same
"two provenance axes, never conflated" principle app.query.evidence already
documents for IdentityBasis vs KnowledgeItem.basis):
  - "observed": a structural fact directly produced by this email's own processing
    (a commitment/meeting/follow-up/project/opportunity/personal item resolved
    from THIS email, the email interaction itself, or a KnowledgeItem whose own
    basis is "stated").
  - "inferred": an LLM interpretation of this email (a buying signal, pain point,
    or objection), or a KnowledgeItem whose own basis is "inferred". Never a
    structural pipeline record.
  - "reconstructed": used ONLY by get_bounded_person_context_for_llm, to mark an
    entry pulled forward from an EARLIER snapshot into the current bounded view --
    it is never produced by build_person_context_entries itself. This is what lets
    a caller distinguish "fresh from the triggering email" from "carried-forward
    history" without re-deriving anything.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from pymongo.database import Database

from app.analysis.schemas import EmailAnalysis
from app.database.repositories import (
    CommitmentRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OpportunityRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonContextSnapshotRepository,
    PersonRepository,
    ProjectRepository,
)
from app.email.models import Email
from app.entities.ids import next_id
from app.entities.thread_events import try_record_event

PersonContextCategory = Literal[
    "email_interaction", "organization", "project", "opportunity",
    "commitment", "meeting", "follow_up", "personal_item",
    "knowledge",
    # P0 person-knowledge fix: semantic, person-attributed knowledge categories
    # (app.analysis.schemas.PersonFactMention.category, verbatim) -- these
    # entries come from a KnowledgeItem whose person_id was set EXPLICITLY at
    # extraction time (app.pipeline._process_person_facts), never from the old
    # blind "attribute every thread-level signal to the sender" mechanism this
    # replaces (see get_bounded_person_context_for_llm's module-level history).
    "role", "responsibility", "preference", "goal", "interest",
    "concern", "pain_point", "objection", "buying_signal", "other",
]
PersonContextProvenance = Literal["observed", "reconstructed", "inferred"]

# Predicates a person-attributed KnowledgeItem can carry (mirrors
# app.analysis.schemas.PersonFactMention.category exactly) -- mapped 1:1 onto
# PersonContextCategory so the LLM-facing label is semantically meaningful
# ("role: VP Engineering") rather than a generic "knowledge" bucket. Any other
# predicate (e.g. from the generic analysis.facts path, or a legacy
# requirements/pain_points/etc. KnowledgeItem that happened to get person-
# linked via the older name-matching fallback) still falls back to "knowledge".
_PERSON_FACT_CATEGORIES = frozenset(
    {"role", "responsibility", "preference", "goal", "interest", "concern", "pain_point", "objection", "buying_signal", "other"}
)


class PersonContextEntry(BaseModel):
    category: PersonContextCategory
    # The canonical id of the referenced record (CMT-/MTG-/FUP-/PRJ-/OPP-/PSN-/
    # ORG-/a knowledge_id) -- None for entries with no single backing record
    # (email_interaction, buying_signal, pain_point, objection).
    record_id: str | None = None
    summary: str
    provenance: PersonContextProvenance
    thread_id: str
    email_id: str
    confidence: float | None = None


class PersonContextSnapshot(BaseModel):
    id: str
    person_id: str
    thread_id: str
    source_email_id: str
    created_at: datetime
    entries: list[PersonContextEntry] = Field(default_factory=list)


def _build_entries_for_person(
    db: Database, person: dict[str, Any], thread_id: str, email: Email,
    entities_referenced: dict[str, list[str]],
) -> list[PersonContextEntry]:
    person_id = person["id"]
    email_id = email.message_id
    is_sender = bool(person.get("email")) and person["email"] == email.from_.email.lower()
    entries: list[PersonContextEntry] = []

    entries.append(
        PersonContextEntry(
            category="email_interaction",
            summary=f"{'Sent' if is_sender else 'Received/referenced in'} email: {email.subject!r}",
            provenance="observed",
            thread_id=thread_id, email_id=email_id,
        )
    )

    org_id = person.get("org_id")
    if org_id:
        organization = OrganizationRepository(db).find_one({"id": org_id})
        if organization is not None:
            entries.append(
                PersonContextEntry(
                    category="organization", record_id=org_id,
                    summary=f"Associated with organization {organization['name']!r}",
                    provenance="observed", thread_id=thread_id, email_id=email_id,
                )
            )

    for project_id in entities_referenced.get("projects", []):
        project = ProjectRepository(db).find_one({"id": project_id})
        if project and person_id in project.get("person_ids", []):
            entries.append(
                PersonContextEntry(
                    category="project", record_id=project_id,
                    summary=f"Involved in project {project['project']!r}",
                    provenance="observed", thread_id=thread_id, email_id=email_id,
                )
            )

    for opportunity_id in entities_referenced.get("opportunities", []):
        opportunity = OpportunityRepository(db).find_one({"id": opportunity_id})
        if opportunity and person_id in opportunity.get("person_ids", []):
            entries.append(
                PersonContextEntry(
                    category="opportunity", record_id=opportunity_id,
                    summary=f"Linked to opportunity {opportunity['name']!r}",
                    provenance="observed", thread_id=thread_id, email_id=email_id,
                )
            )

    for commitment_id in entities_referenced.get("commitments", []):
        commitment = CommitmentRepository(db).find_one({"id": commitment_id})
        if commitment and commitment.get("person_id") == person_id:
            entries.append(
                PersonContextEntry(
                    category="commitment", record_id=commitment_id,
                    summary=f"Commitment ({commitment['class']}): {commitment['what']}",
                    provenance="observed", thread_id=thread_id, email_id=email_id,
                )
            )

    for meeting_id in entities_referenced.get("meetings", []):
        meeting = MeetingRepository(db).find_one({"id": meeting_id})
        if meeting and person_id in meeting.get("person_ids", []):
            when = meeting.get("date") or "an unspecified time"
            entries.append(
                PersonContextEntry(
                    category="meeting", record_id=meeting_id,
                    summary=f"Meeting at {when}", provenance="observed",
                    thread_id=thread_id, email_id=email_id,
                )
            )

    for follow_up_id in entities_referenced.get("follow_ups", []):
        follow_up = FollowUpRepository(db).find_one({"id": follow_up_id})
        if follow_up and follow_up.get("person_id") == person_id:
            entries.append(
                PersonContextEntry(
                    category="follow_up", record_id=follow_up_id,
                    summary=f"Follow-up pending (escalation level {follow_up['escalation_level']})",
                    provenance="observed", thread_id=thread_id, email_id=email_id,
                )
            )

    # PersonalItem has no person_id field (see app.entities.models.PersonalItem) --
    # it keys on sender_email, so it can only ever be attributed to the sender.
    if is_sender:
        for item_id in entities_referenced.get("personal", []):
            item = PersonalItemRepository(db).find_one({"id": item_id})
            if item and item.get("sender_email") == person.get("email"):
                entries.append(
                    PersonContextEntry(
                        category="personal_item", record_id=item_id,
                        summary=f"Personal item: {item['description']}",
                        provenance="observed", thread_id=thread_id, email_id=email_id,
                    )
                )

    # P0 person-knowledge fix: a KnowledgeItem's own `predicate` IS the semantic
    # category when it was written by app.pipeline._process_person_facts (role/
    # responsibility/preference/goal/interest/concern/pain_point/objection/
    # buying_signal/other, matching PersonContextCategory exactly) -- surfaced
    # under that real label instead of a generic "knowledge" bucket. Any other
    # predicate (the generic analysis.facts path, or a legacy thread-scoped item
    # that happened to get person-linked via the older name-matching fallback)
    # still falls back to "knowledge". This REPLACES the older mechanism that
    # blindly copied analysis.buying_signals/pain_points/objections onto the
    # sender regardless of whether the email actually said anything about THEM
    # specifically -- that data remains available at the thread level (
    # app.pipeline._process_knowledge/ThreadContext), just never asserted here
    # as a fact about a specific person without real attribution evidence.
    for knowledge_doc in KnowledgeRepository(db).find_many(
        {"thread_id": thread_id, "person_id": person_id, "source_emails": email_id}
    ):
        predicate = knowledge_doc["predicate"]
        category = predicate if predicate in _PERSON_FACT_CATEGORIES else "knowledge"
        entries.append(
            PersonContextEntry(
                category=category, record_id=knowledge_doc["knowledge_id"],
                summary=(
                    knowledge_doc["current_value"] if category != "knowledge"
                    else f"{predicate}: {knowledge_doc['current_value']}"
                ),
                provenance="observed" if knowledge_doc["basis"] == "stated" else "inferred",
                thread_id=thread_id, email_id=email_id, confidence=knowledge_doc.get("confidence"),
            )
        )

    return entries


def enrich_person_context_from_email(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis,
    entities_referenced: dict[str, list[str]], created_at: datetime,
) -> list[PersonContextSnapshot]:
    """Phase 2/5: called once per processed email, after entity resolution AND
    knowledge-to-entity linking have both run (so KnowledgeItem.person_id is
    already set for this email's facts) -- for every person genuinely referenced
    by this email, builds and persists one incremental PersonContextSnapshot.

    Idempotent: keyed on (person_id, source_email_id) -- see
    PersonContextSnapshotRepository/app.database.indexes. A retried email (still
    resolving to the same canonical EML-nnn and the same deterministic entity ids)
    recomputes the identical entries and overwrites the same document rather than
    creating a duplicate.
    """
    person_repo = PersonRepository(db)
    snapshot_repo = PersonContextSnapshotRepository(db)
    snapshots: list[PersonContextSnapshot] = []

    for person_id in entities_referenced.get("people", []):
        person = person_repo.find_one({"id": person_id})
        if person is None:
            continue
        entries = _build_entries_for_person(db, person, thread_id, email, entities_referenced)

        key = {"person_id": person_id, "source_email_id": email.message_id}
        existing = snapshot_repo.find_one(key)
        snapshot_id = existing["id"] if existing else next_id(db, "PCS-")
        snapshot = PersonContextSnapshot(
            id=snapshot_id, person_id=person_id, thread_id=thread_id,
            source_email_id=email.message_id, created_at=created_at, entries=entries,
        )
        snapshot_repo.upsert_by_key(key, snapshot.model_dump(mode="json"))
        snapshots.append(snapshot)
        try_record_event(
            db, thread_id, email.message_id, "person_context_enriched", "person_context", snapshot.id, "enriched",
            f"Person context enriched for {person_id}",
            metadata={"person_id": person_id, "entry_categories": [e.category for e in entries]},
        )

    return snapshots


def get_bounded_person_context_for_llm(
    db: Database, person_id: str, max_current_entries: int = 15,
    max_historical_snapshots: int = 3, max_historical_entries: int = 10, max_knowledge: int = 8,
) -> dict[str, Any] | None:
    """Phase 4: a bounded, deterministic representation of a Person's accumulated
    context, suitable to pass to an LLM at analysis time (see
    app.pipeline.run_pipeline's call into app.analysis.extractor.
    analyze_email_with_validation). Never dumps the person's entire history --
    only the most recent snapshot's own entries ("current_context") plus a capped
    number of entries pulled forward from earlier snapshots ("historical_context",
    tagged provenance="reconstructed" regardless of their original provenance, so a
    caller can always tell "fresh from the triggering email" apart from "carried
    forward from history") and a capped, most-recently-confirmed slice of this
    person's KnowledgeItems.

    Deliberately NOT built from app.entities.context.get_person_context: that
    function is the full, unbounded 360-degree view (dashboard/MCP tools) and
    remains completely unchanged and untouched by this function -- this is a
    different, bounded view for a different consumer (the LLM classification
    prompt), not a competing or duplicate implementation of it.

    Returns None if person_id doesn't exist.
    """
    person = PersonRepository(db).find_one({"id": person_id})
    if person is None:
        return None

    snapshots = PersonContextSnapshotRepository(db).recent_for_person(
        person_id, limit=1 + max_historical_snapshots
    )
    current_snapshot = snapshots[0] if snapshots else None
    current_entries = (current_snapshot["entries"][:max_current_entries] if current_snapshot else [])

    historical_entries: list[dict[str, Any]] = []
    for snapshot in snapshots[1:]:
        for entry in snapshot["entries"]:
            historical_entries.append({**entry, "provenance": "reconstructed"})
    historical_entries = historical_entries[:max_historical_entries]

    knowledge_docs = sorted(
        KnowledgeRepository(db).find_many({"person_id": person_id}),
        key=lambda doc: doc.get("last_confirmed_at") or "", reverse=True,
    )[:max_knowledge]
    knowledge_bounded = [
        {
            "knowledge_id": doc["knowledge_id"], "predicate": doc["predicate"],
            "current_value": doc["current_value"], "basis": doc["basis"],
            "confidence": doc["confidence"],
        }
        for doc in knowledge_docs
    ]

    return {
        "person_id": person_id,
        "name": person["name"],
        "org": person.get("org"),
        "org_id": person.get("org_id"),
        "goal_pillar": person.get("goal_pillar"),
        "current_context": current_entries,
        "historical_context": historical_entries,
        "knowledge": knowledge_bounded,
    }
