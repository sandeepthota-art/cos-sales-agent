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
from app.config.settings import get_settings
from app.context.models import ContextDelta
from app.database.mongodb import get_client, initialize_database
from app.email.models import Email
from app.interfaces.calendar_provider import CalendarProvider
from app.interfaces.llm_provider import LLMProvider
from app.mcp import tools
from app.providers.factory import ProviderFactory

# Render (and any other HTTP host) assigns the port to listen on via $PORT at runtime --
# irrelevant for the default stdio transport (a local subprocess Claude Desktop spawns
# has no port), so defaulting to 8000 when unset is harmless for local/stdio use.
_HTTP_PORT = int(os.environ.get("PORT", "8000"))


@lru_cache
def _get_db():
    settings = get_settings()
    return initialize_database(get_client(settings.mongodb_uri), settings.mongodb_database)


@lru_cache
def _get_llm_provider() -> LLMProvider:
    return ProviderFactory.create_llm_provider(get_settings())


@lru_cache
def _get_calendar_provider() -> CalendarProvider:
    return ProviderFactory.create_calendar_provider(get_settings())


mcp = FastMCP("cos-sales-agent", host="0.0.0.0", port=_HTTP_PORT)


@mcp.tool()
def process_email(email: Email) -> dict[str, Any]:
    """Run one Gmail message through the sales-agent pipeline: normalization, thread
    resolution, LLM analysis, cumulative context, knowledge extraction/deduplication,
    meeting detection, and reply drafting. Persists everything to MongoDB. Returns
    structured results -- including a proposed reply draft and/or meeting proposal for
    you to review and act on via your Gmail connector. Never sends email or creates
    calendar events itself.

    Map Gmail fields into the input shape as follows:
    - message_id: Gmail message id (or Message-ID header)
    - thread_id: Gmail thread id, if available (omit if unknown -- the pipeline will
      infer one)
    - from/to/cc: {"name": ..., "email": ...} objects
    - timestamp: ISO-8601 datetime string
    - in_reply_to / references: Message-ID header values, if available
    """
    return tools.process_email(
        _get_db(), email, _get_llm_provider(), _get_calendar_provider(), get_settings()
    )


# --- Deterministic MCP boundary (Phase 1 of the Claude-Desktop-reasoning architecture) ---
#
# These five tools let a caller that has already done the LLM reasoning itself (e.g.
# you, reading Gmail directly and reasoning about it) persist the result through this
# project's existing deterministic application logic -- WITHOUT process_email's
# built-in Claude/OpenAI/Groq API call. None of them construct or call an LLM
# provider. process_email above is unchanged and remains fully available as the
# Python-LLM-driven path.


@mcp.tool()
def ingest_email(email: Email) -> dict[str, Any]:
    """Deterministic, LLM-free first step for reasoning about a Gmail message
    yourself instead of using process_email: performs the existing duplicate check
    (message_id + COMPLETED), persists the raw email, and resolves/persists its
    thread. Never calls an LLM.

    Returns {message_id, thread_id, already_completed, previous_context}.
    already_completed=true means this exact email (including any of the existing 662
    historical baseline emails) has already been fully processed -- stop here, do not
    reason about it or call any of the persist_* tools below for it.
    previous_context is the thread's existing accumulated context (or null for a
    brand-new thread), for you to read before reasoning about this email.

    Map Gmail fields the same way process_email documents: message_id, thread_id
    (omit if unknown), from/to/cc as {"name", "email"} objects, timestamp as ISO-8601,
    in_reply_to/references from the Message-ID headers if available.
    """
    return tools.ingest_email(_get_db(), email)


@mcp.tool()
def persist_email_analysis(message_id: str, analysis: EmailAnalysis) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of an email analysis YOU already produced
    by reading the email yourself. Accepts the exact same EmailAnalysis shape
    process_email's own internal LLM call produces -- see that schema's fields
    (people_mentioned, projects_mentioned, commitments_mentioned, meetings_mentioned,
    personal_items_mentioned, goal_pillar, label_applied, confidence, etc.). Runs the
    same deterministic entity/commitment/follow-up/meeting/personal-item/knowledge
    persistence process_email uses internally, and the same regex-based calendar
    meeting detection -- assigning the same canonical IDs (PER-/PRJ-/COM-/FU-/MTG-/
    PSN-) via the same counters. Requires ingest_email to have been called first for
    this message_id.

    This tool never calls analyze_email, update_context, draft_reply, or any real LLM
    provider -- it only persists structured data you supply.

    Returns {message_id, thread_id, entities_referenced, calendar_proposal}.
    """
    return tools.persist_email_analysis(_get_db(), message_id, analysis, get_settings())


@mcp.tool()
def persist_context_delta(thread_id: str, message_id: str, delta: ContextDelta) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of a thread-context update YOU already
    produced. Accepts the exact same bounded ContextDelta shape process_email's own
    internal LLM call produces (only what this email changes -- never the full
    context; see that schema for the exact added/removed/*_updates shape). Merges it
    with the same deterministic logic process_email uses internally. Idempotent: a
    second call for the same (thread_id, message_id) returns the already-persisted
    snapshot unchanged (already_persisted=true) instead of creating a duplicate
    version.

    This tool never calls update_context or any real LLM provider.
    """
    return tools.persist_context_delta(_get_db(), thread_id, message_id, delta)


@mcp.tool()
def create_reply_draft(message_id: str, thread_id: str, subject: str, body: str) -> dict[str, Any]:
    """Deterministic, LLM-free persistence of a reply YOU already drafted. This tool
    never generates reply text itself -- you write the subject/body, it only stores
    what you give it, exactly the way process_email's own internal draft is stored.
    Never overwrites an existing draft for the same message_id (a human may already
    have approved/edited/sent it) -- a repeat call returns the existing draft with
    already_existed=true.

    NEVER SENDS EMAIL. This only creates a draft awaiting human approval, identical
    to process_email's existing behavior.
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
    """List emails already ingested via process_email, most recent first.

    Only shows emails that have already been processed by this tool -- it never reads
    Gmail directly. Each entry includes message_id, thread_id, from/to/cc, subject,
    timestamp, processing_status, the thread's current summary, and a body_preview
    truncated to about 150 characters. The full email body is never returned.

    Each entry also includes record_id, source_type, source_link, date, goal_pillar,
    label_applied, priority ("P1"/"P2"), confidence, and entities_referenced (a dict of
    people/projects/commitments/follow_ups/meetings/personal id lists) -- these are
    populated once the email reaches the ENTITIES_PROCESSED stage, and are None (or
    empty lists, for entities_referenced) for an email that hasn't gotten there yet.
    """
    return tools.list_processed_emails(_get_db(), limit)


@mcp.tool()
def search_emails(
    from_email: str | None = None,
    subject_contains: str | None = None,
    label_applied: str | None = None,
    priority: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Search raw emails in the `emails` collection -- read-only, no LLM processing.

    Filters (all optional, AND-combined): from_email (exact sender address, case
    insensitive), subject_contains (case-insensitive substring), label_applied (exact
    match), priority (exact match, "P1" or "P2"). Returns full email bodies, most
    recent first, capped at `limit` (max 500).
    """
    return tools.search_emails(_get_db(), from_email, subject_contains, label_applied, priority, limit)


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
    thread_id: str | None = None, actionable: bool | None = None, limit: int = 50
) -> list[dict[str, Any]]:
    """List canonical meetings (MTG-xxx) from the `meetings` collection. Optional
    filters: thread_id (exact), actionable (exact). Read-only -- never creates a
    calendar event and never calls a calendar provider.
    """
    return tools.list_meetings(_get_db(), thread_id, actionable, limit)


@mcp.tool()
def list_reply_drafts(
    thread_id: str | None = None,
    source_email_id: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List reply drafts from the `reply_drafts` collection -- read-only, so you can
    verify what process_email actually persisted. Optional filters (all exact match):
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
    process_email assigns). Returns None if it doesn't exist. Never sends, approves,
    edits, or creates a draft -- read-only.
    """
    return tools.get_reply_draft(_get_db(), reply_id)


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
def get_company_summary(org: str) -> dict[str, Any]:
    """Read-only cross-collection summary for an organization name: people and
    projects with a direct field match on `org`, plus commitments/follow-ups reachable
    indirectly through those projects. Meetings and knowledge items are always empty --
    the current schema has no org/company reference for either. See
    `relationship_notes` in the response for exactly which relationships are direct,
    indirect, or unsupported. No LLM involved -- deterministic MongoDB queries only.
    """
    return tools.get_company_summary(_get_db(), org)


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


class _BearerAuthMiddleware(BaseHTTPMiddleware):
    """Rejects any request without a valid bearer token.

    Only used for the streamable-http transport. Unlike stdio (a local subprocess only
    Claude Desktop itself can spawn and talk to), an HTTP deployment is reachable by
    anyone who has the URL -- without this, they could call process_email using your
    MongoDB credentials and burn your LLM API key with no credentials of their own.
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
        header = request.headers.get("authorization", "")
        provided = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not provided or not secrets.compare_digest(provided, self._expected_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


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


def main() -> None:
    if os.environ.get("MCP_TRANSPORT", "stdio") == "streamable-http":
        _run_http()
    else:
        mcp.run()


if __name__ == "__main__":
    main()
