# app/mcp/server.py
import os
import secrets
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.analysis.schemas import EmailAnalysis
from app.config.logging import configure_logging
from app.config.settings import get_settings
from app.context.models import ContextDelta
from app.database.mongodb import get_client, initialize_database
from app.email.models import Email
from app.mcp import tools

# Render (and any other HTTP host) assigns the port to listen on via $PORT at runtime --
# irrelevant for the default stdio transport (a local subprocess Claude Desktop spawns
# has no port), so defaulting to 8000 when unset is harmless for local/stdio use.
_HTTP_PORT = int(os.environ.get("PORT", "8000"))


@lru_cache
def _get_db():
    settings = get_settings()
    return initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)


mcp = FastMCP("cos-sales-agent", host="0.0.0.0", port=_HTTP_PORT)


# --- Deterministic MCP boundary (Phase 1 of the Claude-Desktop-reasoning architecture) ---
#
# These tools let a caller that has already done the LLM reasoning itself (e.g.
# you, reading Gmail directly and reasoning about it) persist the result through this
# project's existing deterministic application logic -- with no server-side LLM API
# call at all. None of them construct or call an LLM provider. The single-LLM-call
# path (process_email) that used to sit here was removed -- it depended on a
# server-side LLM_API_KEY that was unreliable in this project (expired-key/401
# errors), and every real caller had already moved to these granular tools instead.


@mcp.tool()
def ingest_email(email: Email) -> dict[str, Any]:
    """Deterministic, LLM-free first step for reasoning about a Gmail message
    yourself: performs the existing duplicate check (message_id + COMPLETED),
    persists the raw email, and resolves/persists its thread. Never calls an LLM.

    Returns {message_id, thread_id, already_completed, previous_context}.
    already_completed=true means this exact email (including any of the existing 662
    historical baseline emails) has already been fully processed -- stop here, do not
    reason about it or call any of the persist_* tools below for it.
    previous_context is the thread's existing accumulated context (or null for a
    brand-new thread), for you to read before reasoning about this email.

    Map Gmail fields as follows: message_id, thread_id (omit if unknown),
    from/to/cc as {"name", "email"} objects, timestamp as ISO-8601,
    in_reply_to/references from the Message-ID headers if available.
    """
    return tools.ingest_email(_get_db(), email)


@mcp.tool()
def persist_email_analysis(message_id: str, analysis: EmailAnalysis) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of an email analysis YOU already produced
    by reading the email yourself. Accepts the EmailAnalysis schema's fields
    (people_mentioned, projects_mentioned, commitments_mentioned, meetings_mentioned,
    personal_items_mentioned, goal_pillar, label_applied, etc.).

    Before constructing `analysis`, explicitly decide whether this email represents
    a genuine sales opportunity/deal yourself -- never call an LLM API to do this.
    Classify as Sales (goal_pillar="Sales") only when the email shows concrete
    activity tied to a specific prospect/customer organization: an active
    negotiation, pricing/quote discussion, a proposal or contract in progress, a
    pilot/POC/trial, a renewal, an expansion/upsell, a purchase/order discussion, or
    an equivalent concrete deal engagement. Do NOT classify as Sales merely because
    the email mentions a company, product, revenue, or generic sales terminology, or
    because it's marketing/newsletter/internal content unrelated to a specific deal.
    When genuinely Sales, populate projects_mentioned only with enough evidence to
    name the actual engagement -- never invent one. Otherwise leave goal_pillar=""
    and projects_mentioned=[]. Do not include your reasoning in `analysis` -- only
    the resulting structured fields.

    Runs the same deterministic entity/commitment/follow-up/meeting/personal-item/
    knowledge persistence and the same regex-based calendar meeting detection every
    other path through this pipeline uses -- assigning canonical IDs (PER-/PRJ-/COM-/
    FU-/MTG-/PSN-) via the same counters. Requires ingest_email to have been called
    first for this message_id.

    This tool never calls analyze_email, update_context, draft_reply, or any real LLM
    provider -- it only persists structured data you supply.

    Returns {message_id, thread_id, entities_referenced, possible_missed_commitment,
    calendar_proposal}. possible_missed_commitment is a non-authoritative heuristic
    (null when nothing is flagged) -- it fires when commitments_mentioned was left
    empty but the body contains commitment-shaped language ("I'll...", "could
    you..."). It's a prompt to double-check, not proof you missed one -- re-read the
    email if it fires, but don't treat it as an error.
    """
    return tools.persist_email_analysis(_get_db(), message_id, analysis, get_settings())


@mcp.tool()
def persist_context_delta(thread_id: str, message_id: str, delta: ContextDelta) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of a thread-context update YOU already
    produced. Accepts the bounded ContextDelta shape (only what this email changes --
    never the full context; see that schema for the exact added/removed/*_updates
    shape) and merges it with the same deterministic logic this pipeline always
    uses. Idempotent: a second call for the same (thread_id, message_id) returns the
    already-persisted snapshot unchanged (already_persisted=true) instead of creating
    a duplicate version.

    This tool never calls update_context or any real LLM provider.
    """
    return tools.persist_context_delta(_get_db(), thread_id, message_id, delta)


@mcp.tool()
def create_reply_draft(message_id: str, thread_id: str, subject: str, body: str) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of a reply YOU already drafted. This tool
    never generates reply text itself -- you write the subject/body, it only stores
    what you give it. Never overwrites an existing draft for the same message_id (a
    human may already have approved/edited/sent it) -- a repeat call returns the
    existing draft with already_existed=true.

    NEVER SENDS EMAIL. This only creates a draft awaiting human approval.
    """
    return tools.create_reply_draft(_get_db(), message_id, thread_id, subject, body)


@mcp.tool()
def mark_email_completed(message_id: str) -> dict[str, Any]:
    """Final step of the deterministic, LLM-free ingestion path: marks an email
    COMPLETED so future ingest_email calls correctly report it as already_completed.
    Verifies that persist_email_analysis has actually run for this message_id first
    (rejects the call otherwise) -- it will not mark an email complete based only on
    your say-so.
    """
    return tools.mark_email_completed(_get_db(), message_id)


@mcp.tool()
def list_processed_emails(limit: int = 50) -> list[dict[str, Any]]:
    """List emails already ingested through this pipeline, most recent first.

    Only shows emails that have already been processed by this tool -- it never reads
    Gmail directly. Each entry includes message_id, thread_id, from/to/cc, subject,
    timestamp, processing_status, the thread's current summary, and a body_preview
    truncated to about 150 characters. The full email body is never returned.

    Each entry also includes goal_pillar, label_applied, and entities_referenced
    (a dict of people/projects/commitments/follow_ups/meetings/personal id
    lists) -- these are populated once the email reaches the
    ENTITIES_PROCESSED stage, and are None (or empty lists, for
    entities_referenced) for an email that hasn't gotten there yet.
    """
    return tools.list_processed_emails(_get_db(), limit)


@mcp.tool()
def search_emails(
    from_email: str | None = None,
    subject_contains: str | None = None,
    label_applied: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search raw emails in the `emails` collection -- read-only, no LLM processing.

    Filters (all optional, AND-combined): from_email (exact sender address, case
    insensitive), subject_contains (case-insensitive substring), label_applied (exact
    match). Returns full email bodies, most recent first, capped at `limit` (max 500).
    """
    return tools.search_emails(_get_db(), from_email, subject_contains, label_applied, limit)


@mcp.tool()
def get_thread(thread_id: str) -> dict[str, Any] | None:
    """Retrieve one full email conversation by thread_id -- all messages, ordered
    oldest to newest, with complete bodies. Also includes the thread's latest context
    snapshot if the AI pipeline has produced one for it; latest_context/context_version
    are None for a thread that was only raw-ingested. Returns None if the thread_id
    doesn't exist. Read-only.
    """
    return tools.get_thread(_get_db(), thread_id)


@mcp.tool()
def list_people(
    org: str | None = None,
    email: str | None = None,
    thread_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List canonical people (PER-xxx) from the `people` collection. Optional filters:
    org (exact), email (exact, case insensitive), thread_id (person has this thread in
    their open_threads). Read-only -- never creates or resolves a person.
    """
    return tools.list_people(_get_db(), org, email, thread_id, limit)


@mcp.tool()
def list_projects(
    entity: str | None = None, goal_pillar: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List canonical projects (PRJ-xxx) from the `projects` collection. Optional
    filters: entity (exact), goal_pillar (exact). Read-only.
    """
    return tools.list_projects(_get_db(), entity, goal_pillar, limit)


@mcp.tool()
def update_project_fields(
    project_id: str,
    status: str | None = None,
    owner: str | None = None,
    health: str | None = None,
    next_milestone: str | None = None,
    due: str | None = None,
) -> dict[str, Any]:
    """The ONLY way to set a Project's manually-managed fields: status, owner,
    health, next_milestone, due. Use this only when the user (or you, on their
    explicit instruction) is providing this information directly -- never infer
    any of these from email content yourself. Mirrors update_opportunity_fields
    exactly. This tool is deterministic and calls no LLM/API; it only persists
    exactly what you pass it.

    Only fields you actually supply (non-None) are changed -- an omitted field
    is left untouched, never reset to null. due, if given, must be an ISO 8601
    date/datetime string. Never touches person_ids/org_id/goal_pillar/project --
    those remain exclusively pipeline-derived and are not settable through this
    tool.

    Raises ValueError if project_id doesn't exist.
    """
    return tools.update_project_fields(_get_db(), project_id, status, owner, health, next_milestone, due)


@mcp.tool()
def list_opportunities(
    org_id: str | None = None, status: str | None = None, project_id: str | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List canonical Opportunities (OPP-xxx) from the `opportunities` collection.
    Optional filters: org_id (exact), status (exact -- "open"/"won"/"lost"),
    project_id (matches any Opportunity whose project_ids includes it). Read-only
    -- never creates, resolves, or modifies anything. Opportunities are created
    only by the pipeline itself (a Sales-classified email whose projects_mentioned
    entry resolved to a real project) -- there is no create_opportunity tool.
    """
    return tools.list_opportunities(_get_db(), org_id, status, project_id, limit)


@mcp.tool()
def update_opportunity_fields(
    opportunity_id: str,
    stage: str | None = None,
    owner: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    expected_close_date: str | None = None,
    next_action: str | None = None,
) -> dict[str, Any]:
    """The ONLY way to set an Opportunity's manually-managed CRM fields: stage,
    owner, value, currency, expected_close_date, next_action. Use this only when
    the user (or you, on their explicit instruction) is providing this
    information directly -- never infer any of these from email content
    yourself. This tool is deterministic and calls no LLM/API; it only persists
    exactly what you pass it.

    Only fields you actually supply (non-None) are changed -- an omitted field
    is left untouched, never reset to null. expected_close_date, if given, must
    be an ISO 8601 date/datetime string. Never touches source_email_ids/
    project_ids/meeting_ids/person_ids/buying_signals/last_activity_at -- those
    remain exclusively pipeline-derived and are not settable through this tool.

    Raises ValueError if opportunity_id doesn't exist.
    """
    return tools.update_opportunity_fields(
        _get_db(), opportunity_id, stage, owner, value, currency, expected_close_date, next_action
    )


@mcp.tool()
def list_commitments(
    thread_id: str | None = None,
    class_: str | None = None,
    project_id: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List canonical commitments (CMT-xxx) from the `commitments` collection. Optional
    filters: thread_id (exact), class_ (one of mine/owed_to_me/theirs/recap),
    project_id (exact -- populated by the live pipeline only when the commitment's
    counterparty's org matches a project resolved in the same email; unset otherwise).
    Read-only -- status is returned exactly as stored, never inferred.
    """
    return tools.list_commitments(_get_db(), thread_id, class_, project_id, limit)


@mcp.tool()
def list_follow_ups(
    commitment_id: str | None = None,
    thread_id: str | None = None,
    escalation_level: int | None = None,
    audience: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List canonical follow-ups (FUP-xxx) from the `follow_ups` collection. Optional
    filters (all exact match): commitment_id, thread_id, escalation_level (1-4),
    audience (internal/client_fixed_date/client_open_window/his_own_question -- only
    ever set for a chased "owed_to_me" commitment; "his_own_question" is never actually
    produced today, see app.entities.dates.classify_follow_up_timing), status
    (active/resolved/dropped). Read-only -- `escalation_level`/`status` always default
    to their initial state (level 1, active) today, since no scheduler exists yet to
    transition them.
    """
    return tools.list_follow_ups(_get_db(), commitment_id, thread_id, escalation_level, audience, status, limit)


@mcp.tool()
def list_meetings(
    thread_id: str | None = None,
    actionable: bool | None = None,
    category: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List canonical meetings (MTG-xxx) from the `meetings` collection. Optional
    filters: thread_id (exact), actionable (exact), category (BRD gap-analysis
    FR-04 -- one of "sales"/"finance"/"internal"/"customer"/"unknown",
    case-insensitive; "prospect"/"project" are rejected since no stored field
    reliably supports them), start_date/end_date (ISO 8601, half-open [start,
    end) against this meeting's own `date`). Combine category with start_date/
    end_date to answer e.g. "what sales meetings do I have today" -- pass
    today's own bounds. Read-only -- never creates a calendar event and never
    calls a calendar provider.
    """
    return tools.list_meetings(
        _get_db(), thread_id, actionable, category, start_date, end_date, limit,
        agent_email=get_settings().agent_email,
    )


@mcp.tool()
def list_reply_drafts(
    thread_id: str | None = None,
    source_email_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List reply drafts from the `reply_drafts` collection -- read-only, so you can
    verify what create_reply_draft actually persisted. Optional filters (all exact match):
    thread_id, source_email_id, status (one of no_reply_required/awaiting_approval/
    approved/edited/rejected/cancelled/simulated_sent/sent).

    Each entry is returned exactly as stored: reply_id, thread_id, source_email_id,
    status, draft ({subject, body}), created_by, created_at, approved_by, sent_at.
    created_at is absent on a draft written before that field existed. Never sends,
    approves, edits, or creates a draft -- read-only.
    """
    return tools.list_reply_drafts(_get_db(), thread_id, source_email_id, status, limit)


@mcp.tool()
def get_reply_draft(reply_id: str) -> dict[str, Any] | None:
    """Retrieve one reply draft by its reply_id (e.g. "reply_<message_id>", the id
    create_reply_draft assigns). Returns None if it doesn't exist. Never sends, approves,
    edits, or creates a draft -- read-only.
    """
    return tools.get_reply_draft(_get_db(), reply_id)


@mcp.tool()
def set_reply_draft_gmail_id(reply_id: str, gmail_draft_id: str) -> dict[str, Any]:
    """Records the real Gmail draft id a human (via Claude's own Gmail connector)
    already created for this reply draft -- see
    skills/sync-reply-drafts-to-gmail/SKILL.md. Never creates, edits, or sends
    anything in Gmail itself; only persists an id that already exists, so a later
    sync pass can tell this reply draft already has a matching Gmail draft and skip
    it. Raises if reply_id doesn't exist.
    """
    return tools.set_reply_draft_gmail_id(_get_db(), reply_id, gmail_draft_id)


@mcp.tool()
def set_reply_withheld_reason(message_id: str, reason: str) -> dict[str, Any]:
    """Records why a message classified as needing a reply did not get a reply
    draft -- e.g. a sensitive-data request, phishing/spoofing red flags, or a
    deliberate judgment call. Persisted on the Email document as
    `reply_withheld_reason`, surfaced in the dashboard's email lookup box. Never
    creates or edits a reply draft. Raises if message_id doesn't exist.
    """
    return tools.set_reply_withheld_reason(_get_db(), message_id, reason)


@mcp.tool()
def set_email_summary(message_id: str, summary: str) -> dict[str, Any]:
    """Records a short summary of an email's body for the dashboard's "Look up an
    email" box -- you (Claude) write the summary yourself after reading the email;
    this tool never calls an LLM API itself (there is none in this codebase's live
    path). Persisted on the Email document as `body_summary`, cached and reused on
    every future dashboard view. Raises if message_id doesn't exist.
    """
    return tools.set_email_summary(_get_db(), message_id, summary)


@mcp.tool()
def get_project_summary(project_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary for one project (PRJ-xxx): the project
    itself, plus related commitments/follow-ups found via real stored references.
    Meetings, knowledge items, and people are always empty for this tool because the
    current schema has no field linking any of them to a project -- see
    `relationship_notes` in the response for exactly which relationships are direct,
    indirect, or unsupported. Returns None if the project doesn't exist. No LLM
    involved -- deterministic MongoDB queries only.
    """
    return tools.get_project_summary(_get_db(), project_id)


@mcp.tool()
def list_organizations(name_contains: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Read-only listing of organizations -- the way to turn a company NAME
    into the canonical org_id (ORG-xxx) that get_company_summary,
    persist_organization_research, and merge_organization_records all
    require. name_contains matches case-insensitively, anywhere in the name.
    """
    return tools.list_organizations(_get_db(), name_contains=name_contains, limit=limit)


@mcp.tool()
def get_company_summary(org_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary for one organization (ORG-xxx): people,
    projects, commitments, follow-ups, meetings, and knowledge items with a direct
    org_id reference. See `relationship_notes` in the response for exactly which
    relationship each list represents. Returns None if the organization doesn't
    exist. No LLM involved -- deterministic MongoDB queries only.
    """
    return tools.get_company_summary(_get_db(), org_id)


@mcp.tool()
def lookup_knowledge(
    entity_id: str | None = None,
    query: str | None = None,
    depth: int = 1,
) -> dict[str, Any]:
    """Read-only traversal over the Markdown knowledge projection (data/knowledge/) --
    a preparation layer for a future graph database, distinct from every other tool
    here (which read MongoDB directly). Never touches MongoDB, never writes to
    Markdown, never sends email or creates calendar events.

    Pass exactly one of:
    - entity_id: a canonical id (PER-xxx/PRJ-xxx/CMT-xxx/FUP-xxx/MTG-xxx/PSN-xxx/
      ORG-xxx), a message_id, or a thread_id.
    - query: a name/email/project-name to search for (deterministic exact-then-
      substring matching only -- no fuzzy matching, no LLM, no vector search).

    depth (1-5, bounded and cycle-safe): how many relationship hops to traverse
    outward from the found entity.

    Every relationship in the result reports its basis -- "direct" (a real stored
    reference) or "derived" (co-occurred in a shared source email) -- and its
    provenance (source email/thread ids, or explicitly marked unavailable). A
    derived relationship is never presented as if it were direct.
    """
    return tools.lookup_knowledge(get_settings().knowledge_dir, entity_id, query, depth)


@mcp.tool()
def preview_duplicate_person_candidates() -> dict[str, Any]:
    """Read-only report of possible duplicate Person records currently in MongoDB --
    reuses the existing consolidation classifier (app.duplicate_consolidation.
    generate_merge_plan) exactly as-is. Never writes, merges, or approves anything --
    this is strictly a safety-net report for a human to review; nothing is changed in
    the database by calling this tool, ever.

    Returns {"candidate_count": int, "candidates": [...]} -- each candidate names the
    duplicate person, the canonical person it likely duplicates, a confidence label,
    the evidence behind that classification, and a count of downstream records
    (commitments/follow-ups/meetings/etc.) that a future approved merge would update.
    An actual merge always remains a separate, explicit, human-approved step outside
    this tool.
    """
    return tools.preview_duplicate_person_candidates(_get_db())


@mcp.tool()
def merge_person_records(duplicate_person_id: str, canonical_person_id: str, reason: str) -> dict[str, Any]:
    """Merges one Person record into another -- use only when you (Claude, having
    read the actual emails) are genuinely confident two Person records are the same
    real person. preview_duplicate_person_candidates only catches matches with
    similar email/org signals; it will NOT surface every real duplicate (e.g. two
    records with no email at all, or the same person under two unrelated
    addresses) -- this tool is for exactly that gap, based on your own judgment
    rather than that classifier.

    Repoints every downstream reference (threads, commitments, meetings,
    follow_ups, projects, knowledge_items, reply_drafts, calendar_actions, and
    emails' entities_referenced.people) from duplicate_person_id to
    canonical_person_id, then marks duplicate_person_id merged (status="merged",
    merged_into=canonical_person_id) -- it is never deleted, only retired.

    Refuses (returns status="BLOCKED") if a calendar action tied to either person
    already has external attendee data, rather than risk silently corrupting a
    real calendar event. Raises ValueError if either id doesn't exist, they're the
    same id, or duplicate_person_id is already merged into someone else.
    """
    return tools.merge_person_records(_get_db(), duplicate_person_id, canonical_person_id, reason)


@mcp.tool()
def persist_organization_research(
    org_id: str,
    industry: str | None = None,
    description: str | None = None,
    products_services: list[str] | None = None,
    size_estimate: str | None = None,
    headquarters: str | None = None,
    website: str | None = None,
) -> dict[str, Any]:
    """Persists external research you (Claude, via your own WebSearch tool)
    already performed about a company -- call this after seeing
    new_organizations_needing_research in a persist_email_analysis result, or
    after reviewing list_unresearched_organizations. Only overwrites a field
    if it wasn't already hand-corrected (research_source != "manual" on the
    existing record). Sets researched_at regardless of whether you found
    anything, so the same org isn't re-surfaced on every future email. Raises
    ValueError if org_id doesn't exist.
    """
    return tools.persist_organization_research(
        _get_db(), org_id, industry=industry, description=description,
        products_services=products_services, size_estimate=size_estimate,
        headquarters=headquarters, website=website,
    )


@mcp.tool()
def list_unresearched_organizations() -> list[dict[str, Any]]:
    """Read-only catch-up list of every active Organization that has never
    been researched (researched_at is None) -- for a company created but
    never mentioned again in a later email, independent of
    persist_email_analysis's per-email new_organizations_needing_research
    signal. Never writes anything.
    """
    return tools.list_unresearched_organizations(_get_db())


@mcp.tool()
def preview_duplicate_organization_candidates() -> list[dict[str, Any]]:
    """Read-only heuristic preview of organizations that may be the same real
    company under a different name or domain (e.g. "DataBeat" / "DataBeat
    Analytics" / databeat.com) -- resolve_organization's own automatic path
    only merges on exact email domain and never catches these. Never merges
    anything -- review each candidate yourself (WebSearch if genuinely
    ambiguous) and call merge_organization_records only when confident.
    """
    return tools.preview_duplicate_organization_candidates(_get_db())


@mcp.tool()
def merge_organization_records(source_org_id: str, target_org_id: str) -> dict[str, Any]:
    """Merges one Organization record into another -- use only when you
    (Claude, having reviewed a preview_duplicate_organization_candidates pair,
    with WebSearch if genuinely ambiguous) are confident both records are the
    same real company. Repoints org_id across every downstream collection
    (people, projects, opportunities, commitments, follow_ups, meetings,
    knowledge_items, reply_drafts, calendar_actions), unions aliases, and
    backfills blank profile fields on the target from the source. The source
    is never deleted -- only retired (status="merged", merged_into=
    target_org_id). Raises ValueError if either id doesn't exist, they're the
    same id, or source_org_id is already merged into someone else.
    """
    return tools.merge_organization_records(_get_db(), source_org_id, target_org_id)


@mcp.tool()
def persist_person_profile(
    person_id: str,
    role: str | None = None,
    profile_summary: str | None = None,
    recent_context: str | None = None,
    key_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Persists a Customer/Contact Intelligence profile update you (Claude)
    already composed -- call this after seeing people_profile_context in a
    persist_email_analysis result, when the email adds something substantive
    about who this person is, their role, or what you've been discussing.
    Each field is optional -- omit one to leave it unchanged. Raises
    ValueError if person_id doesn't exist.
    """
    return tools.persist_person_profile(
        _get_db(), person_id, role=role, profile_summary=profile_summary,
        recent_context=recent_context, key_topics=key_topics,
    )


@mcp.tool()
def ask_question(text: str, timezone: str | None = None) -> dict[str, Any]:
    """BRD gap-analysis FR-01: answer an executive-style natural-language question
    (e.g. "What meetings do I have today?", "What follow-ups are overdue?", "What
    commitments are due?", "What are my P1 emails?", "What projects are currently
    active?") by reusing the existing, independently-tested query engine
    (app.query.service.execute_query) -- never a second, duplicate query mechanism.

    Strictly read-only and deterministic: this tool never constructs or calls an
    LLM itself, so intent classification only ever uses the deterministic,
    fixed-phrase Stage-1 rules -- an unrecognized question returns a clear
    "not recognized" result rather than a guess. Returns the query engine's own
    structured result (status, resolved entities, evidence records) -- it does NOT
    synthesize prose; phrase the final natural-language answer yourself from this
    structured evidence.

    timezone (optional, e.g. "Asia/Kolkata"): only affects resolution of relative
    date phrases like "today"/"this week"; defaults to the server's configured
    timezone when omitted.
    """
    return tools.ask_question(_get_db(), get_settings(), text, timezone_name=timezone)


@mcp.tool()
def whats_on_my_table() -> dict[str, Any]:
    """BRD gap-analysis FR-02: one read-only call answering "What's on my table?" /
    "What needs my attention today?" -- combines pending reply drafts, overdue
    follow-ups (still outstanding, not resolved/dropped), upcoming commitments,
    upcoming meetings, and active projects into one structured response. Every key
    is always present (an empty list, never an omitted key),
    and each category is computed independently -- a problem in one category can
    never affect another. Reuses existing repositories/query-layer functions
    exclusively; invents no data.
    """
    return tools.whats_on_my_table(_get_db(), get_settings())


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request without a valid bearer token.

    Only used for the streamable-http transport. Unlike stdio (a local subprocess only
    Claude Desktop itself can spawn and talk to), an HTTP deployment is reachable by
    anyone who has the URL -- without this, they could call any tool here using your
    MongoDB credentials with no credentials of their own.

    Accepts the SAME `MCP_AUTH_TOKEN` via either of exactly two headers -- no others:
    `Authorization: Bearer <token>` (the original scheme) or `X-Auth-Token: <token>`
    (for a client that can't set a custom Authorization header). When `Authorization`
    is present at all, it is the only header checked -- an invalid Authorization value
    is rejected outright and never falls back to `X-Auth-Token`, even if the latter is
    valid. `X-Auth-Token` is only consulted when `Authorization` is absent entirely.
    """

    def __init__(self, app, expected_token: str) -> None:
        super().__init__(app)
        self._expected_token = expected_token

    async def dispatch(self, request: Request, call_next):
        # /health is deliberately unauthenticated -- a platform liveness probe (e.g.
        # Render's) has no way to supply the bearer token, and the endpoint reveals
        # nothing beyond "the process is running".
        if request.url.path == "/health":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        if auth_header:
            provided = auth_header[len("Bearer "):].strip() if auth_header.startswith("Bearer ") else ""
            if provided and secrets.compare_digest(provided, self._expected_token):
                return await call_next(request)
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        x_auth_token = request.headers.get("x-auth-token", "").strip()
        if x_auth_token and secrets.compare_digest(x_auth_token, self._expected_token):
            return await call_next(request)
        return JSONResponse({"error": "unauthorized"}, status_code=401)


async def _health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _run_http() -> None:
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "")
    if not auth_token:
        raise RuntimeError(
            "MCP_AUTH_TOKEN must be set when MCP_TRANSPORT=streamable-http -- running this "
            "server on the public internet without it would let anyone call its tools using "
            "your MongoDB credentials and LLM API key, with no authentication at all."
        )

    import uvicorn

    starlette_app = mcp.streamable_http_app()
    starlette_app.add_route("/health", _health, methods=["GET"])
    starlette_app.add_middleware(_BearerAuthMiddleware, expected_token=auth_token)
    uvicorn.run(
        starlette_app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )


def _wants_http_transport() -> bool:
    transport = os.environ.get("MCP_TRANSPORT")
    if transport is not None:
        return transport == "streamable-http"
    # MCP_TRANSPORT left completely unset: infer from $PORT, which every
    # PaaS-style host (Render, Railway, Heroku) injects for a web service, and which
    # a stdio launcher (Claude Desktop, a local `python -m app.mcp.server`) never
    # sets. Without this, an operator who forgets to set MCP_TRANSPORT=streamable-http
    # on such a host gets a silent "Application exited early" with zero diagnostic
    # info (mcp.run()'s stdio loop hits EOF on a stdin nothing is attached to and
    # exits cleanly) -- this at least fails loudly instead, via _run_http()'s own
    # "MCP_AUTH_TOKEN must be set" RuntimeError if that's also missing.
    return bool(os.environ.get("PORT"))


def main() -> None:
    # BRD gap-analysis C1: this entry point never called configure_logging at all
    # before this change -- every log line (including every pipeline stage
    # transition logged by app.database.repositories.EmailRepository.set_stage)
    # went through Python's unconfigured root logger default. Same settings-driven
    # LOG_LEVEL/LOG_FORMAT every other entry point (main.py, app.scheduler) already uses.
    settings = get_settings()
    configure_logging(settings.log_level, structured=(settings.log_format == "json"))

    if _wants_http_transport():
        _run_http()
    else:
        mcp.run()


if __name__ == "__main__":
    main()
