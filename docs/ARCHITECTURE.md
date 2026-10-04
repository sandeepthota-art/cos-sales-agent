# CoS Sales Agent — Architecture & Implementation Notes

This is a standalone reference for the Chief of Staff (CoS) AI Agent: an
email-ingesting sales assistant that resolves canonical people/organizations,
extracts commitments and follow-ups, detects meetings, drafts replies, and
exposes all of it through an MCP server plus a human-in-the-loop dashboard.

### Status legend

Every capability below is one of exactly four states — never described as
production-ready when it isn't:

- **Implemented** — real code, exercised by a passing test.
- **Simulated** — the code path exists and is tested, but deliberately never
  performs the real external side effect (e.g. `simulate_send`).
- **Out of scope (V1)** — explicitly not built; a schema field may exist
  (e.g. `Email.attachments`) but nothing populates or reads it.
- **Requires external configuration** — code exists but depends on a real
  provider/credential not present in the current deployment (e.g. a real,
  non-mock `CalendarProvider`).

---

## 1. Core Architecture & Deployment

### Dual-service deployment model

The whole system is **one repository, one Docker image, two independent
Render Web Services**. Both services build from the same `Dockerfile`; only
the **start command** differs per service.

```
                 ┌─────────────────────────┐
                 │   GitHub repository      │
                 │   (single Dockerfile)     │
                 └────────────┬─────────────┘
                              │ same image, built once per push
              ┌───────────────┴───────────────┐
              ▼                                ▼
   ┌────────────────────────┐      ┌─────────────────────────────┐
   │  Render Service #1      │      │  Render Service #2           │
   │  MCP Server              │      │  Streamlit Dashboard          │
   │  CMD: python -m           │      │  CMD: streamlit run          │
   │       app.mcp.server      │      │       app/ui/dashboard.py     │
   │  Port: $PORT (streamable- │      │  Port: $PORT                  │
   │        http transport)    │      │                                │
   └────────────┬───────────┘      └───────────────┬───────────────┘
                │                                   │
                └───────────────┬───────────────────┘
                                 ▼
                    MongoDB Atlas (cos_sales_production_v1)
```

Neither service depends on the other at runtime — they only share the
database. The MCP server is the **write** path (ingests and analyzes email,
resolves entities, drafts replies); the dashboard is the **read/approval**
path (a human looks at what the agent produced and decides what happens
next).

### Environment variables and security gates

| Variable | Service | Purpose |
|---|---|---|
| `MONGODB_URI` / `MONGODB_DATABASE` (or the `SALES_AGENT_`-prefixed equivalents, which take priority — see below) | Both | Atlas connection string and database name |
| `MCP_TRANSPORT` | MCP server | `streamable-http` for a hosted deployment; defaults to `stdio` for a locally-spawned client (Claude Desktop). If left completely unset, the process infers `streamable-http` whenever `$PORT` is present (every PaaS host injects it; a stdio launcher never does) — this turns a missing config into a loud startup error instead of a silent, diagnostics-free exit |
| `MCP_AUTH_TOKEN` | MCP server | Required whenever `MCP_TRANSPORT=streamable-http`. Every request to any tool except `GET /health` must carry `Authorization: Bearer <token>`, checked with a constant-time comparison. Without it, the server refuses to start in HTTP mode at all (raises rather than silently serving unauthenticated) |
| `DASHBOARD_READ_ONLY` | Dashboard | When `true`, hides every button that can trigger a real action (Approve/Reject/Edit a reply, Create-on-calendar/Ignore a meeting). This is the **entire** safety boundary for handing the dashboard URL to someone who isn't the operator — every mutating action in the whole system is reachable only from this one file |
| `DASHBOARD_PASSWORD` | Dashboard | Optional login gate. Unset → no gate, dashboard opens directly (local dev). Set → a password prompt blocks all rendering until it matches |
| `LLM_PROVIDER` / `LLM_API_KEY` / `LLM_MODEL` / `LLM_BASE_URL` | MCP server | Selects `mock` (no API key, deterministic keyword heuristics — used by the entire test suite and safe fallback), `claude` (real Anthropic API), or `openai` (any OpenAI-compatible endpoint, e.g. Groq via `LLM_BASE_URL`) |
| `AGENT_EMAIL` / `AGENT_NAME` | MCP server | The operator's own mailbox/display name — see §5 Identity Resolution |

**Routing logic**: a single Python `Settings` object (`app/config/settings.py`,
pydantic-settings) is the one source of truth both services read from. It
resolves each value from (in priority order) the real OS environment, then a
local `.env` file, then a class default — with a deliberate `AliasChoices`
guard so a project-specific `SALES_AGENT_MONGODB_URI` always wins over a
generic `MONGODB_URI` that some *other*, unrelated system on the same host
might have set. `_ENV_FILE` is resolved from `Path(__file__).resolve()`, not
the process's working directory, so it works identically regardless of how
or from where the process was launched.

---

## 2. Database & Data Model (MongoDB Atlas)

Database: `cos_sales_production_v1`. Every collection and index is created
automatically on first write (`app/database/indexes.py`) — nothing is
provisioned by hand.

### Semantic Knowledge Graph (`knowledge_items`)

Facts extracted from email are stored as **Subject–Predicate–Object (EAV)
triples**, not as free-form text summaries:

```jsonc
{
  "knowledge_id": "knowledge_<thread>_<subject>_<predicate>_<fact_key>",
  "thread_id": "...",
  "subject_key": "ashok",          // normalized subject (a person, org, or thread)
  "predicate": "requires",          // e.g. requires / has_pain_point / mentioned_competitor
  "fact_key": "seat_count",         // stable slot name for this fact
  "current_value": "100 seats",     // the latest known value
  "basis": "stated",                // "stated" (customer said it) vs "inferred"
  "confidence": 0.75,
  "person_id": "PER-001",           // set only when the subject resolves to a canonical Person
  "org_id": null,                   // set only when it resolves to a canonical Organization
  "source_emails": ["msg_..."],
  "status": "active",
  "first_seen_at": "...",
  "last_confirmed_at": "...",
  "history": [{"value": "...", "source_email_id": "...", "recorded_at": "..."}]
}
```

Deduplication runs per `(thread_id, subject_key, predicate, fact_key)`: a
new mention of an already-known fact updates `current_value` and appends to
`history` rather than creating a duplicate row; a genuinely conflicting value
triggers an LLM-based `verify_same_fact` check before deciding whether it's
the same fact restated or a real change.

**Guardrail (added after a real incident):** the extraction prompt
explicitly forbids two categories of content from ever becoming a
knowledge item — see §3, "Prompt engineering guardrails."

### Core collections

| Collection | Holds | Key relationships |
|---|---|---|
| `emails` | One document per processed message — raw content, `processing_status`, `label_applied` (6-way triage), `entities_referenced` | Links out to every entity type below via `entities_referenced` |
| `threads` | Resolved conversation threads | `message_ids`, `participant_emails`, canonical `person_ids`/`org_ids` |
| `context_snapshots` | Cumulative, versioned per-thread summary (16-field `ThreadContext`) plus a diff (`changes_from_previous_context`) for every new email | One per `(thread_id, context_version)` |
| `people` | Canonical Person records — real contacts **and** the operator's own dedicated profile (`type: "operator"`) | `org_id`, `open_threads`, `merged_into` (soft-merge trail) |
| `organizations` | Canonical Organization records, resolved by email domain only | Referenced by `people.org_id` |
| `projects` | Named projects/engagements mentioned in an email — pillar-agnostic (Sales, Finance, Operations, ...), LLM-extracted | Linked to people via matching `org_id` |
| `opportunities` | Confirmed **sales** deals — a CRM overlay on top of `projects`, created only for `goal_pillar == "Sales"` (see §3.1) | `project_ids`, `org_id`, `person_ids`, `meeting_ids`, `source_email_ids` |
| `commitments` | Promises made in email (`mine` / `owed_to_me` / `theirs` / `recap`) | `person_id`, `thread_id` |
| `follow_ups` | Derived from chased commitments, with computed timing windows and escalation state | `commitment_id`, `thread_id` |
| `meetings` | Detected meeting proposals, each classified (`app/query/meetings.py::classify_meeting`) as `INTERNAL`/`CUSTOMER` (attendee domain vs. `AGENT_EMAIL` domain), `SALES`/`FINANCE` (only on an exact, case-insensitive match of the meeting's own stored `project_or_pillar` — never keyword-guessed independently of it), or `UNKNOWN` | `person_ids`, linked `calendar_actions` |
| `personal_items` | Non-business items mentioned (reminders, personal tasks) | keyed by sender email |
| `reply_drafts` | One per email that needed a reply, `status` machine (`awaiting_approval → approved/rejected/edited → simulated_sent`) | `source_email_id`, `person_id`, `org_id` |
| `calendar_actions` | Proposed (never auto-created) calendar events | `thread_id`, `meeting_fingerprint`, `person_id`, `meeting_id` |
| `processing_runs` | Per-batch pipeline run summaries | — |

### Opportunity vs Project vs Email

**Email** carries the raw classification signal: `goal_pillar` (a free-text
field, e.g. `"Sales"`, `"Finance"`, `""`) is set per-email by whoever is doing
the reasoning (Claude Desktop, via the granular tools — see "Prompt
engineering guardrails" below for the criteria used).

**Project** is a lightweight, pillar-agnostic "named engagement" extracted
directly from `EmailAnalysis.projects_mentioned` — created for *any*
`goal_pillar` value, not just Sales, whenever the reasoning caller identifies
enough evidence to name one (`app.entities.resolution.resolve_project`).

**Opportunity** is a CRM overlay, *not* a replacement for Project — it exists
only for confirmed **Sales** deals, and references one or more Project records
via `project_ids` rather than duplicating their fields. It is created only
when **both** conditions hold on the same email: `goal_pillar == "Sales"` AND
a `projects_mentioned` entry that actually resolved to a real `project_id`. A
Sales-classified email with no nameable project creates no Opportunity — the
system never invents one to fill the gap.

**Field ownership** — every Opportunity field is either pipeline-derived or
human-managed, never both:

| Pipeline-derived (append-only, automatic) | Human-managed only (`update_opportunity_fields`) |
|---|---|
| `source_email_ids`, `project_ids`, `meeting_ids`, `person_ids`, `buying_signals`, `last_activity_at` | `stage`, `owner`, `value`, `currency`, `expected_close_date`, `next_action` |

`status` (`"open"`/`"won"`/`"lost"`) defaults to `"open"` at creation — a
structural fact about a brand-new record, not a guess — and is otherwise only
ever changed via `update_opportunity_fields`.

**Resolution/deduplication** (`app.entities.resolution.resolve_opportunity`):
the dedup key is exclusively *"does an existing Opportunity already reference
this exact `project_id`?"* — no independent name/entity text comparison, no
fuzzy matching, no embeddings. This deliberately inherits Project's own
existing deterministic dedup (normalized entity + name + `goal_pillar`) rather
than re-deriving one: two Sales emails whose `projects_mentioned` entries both
resolve to the same `project_id` always resolve to the same Opportunity.
**Known V1 limitation**: two textually-distinct Projects a human would
recognize as the same deal do not auto-merge into one Opportunity.

**Data flow:**

```
Gmail
  ↓
Claude Desktop reads the email
  ↓
Claude determines Sales / Not Sales (goal_pillar)
  ↓
ingest_email                          (raw persistence, no LLM)
  ↓
persist_email_analysis                (deterministic, LLM-free)
  ├─ if NOT Sales → persisted as a normal email; no Opportunity, ever
  └─ if Sales:
        ↓
     resolve/create Project (existing logic, pillar-agnostic)
        ↓
     resolve/create Opportunity (only if a project_id was resolved)
        ↓
     link email ↔ Opportunity (entities_referenced["opportunities"])
  ↓
persist_context_delta / create_reply_draft (unchanged, as warranted)
  ↓
mark_email_completed
```

The MCP server never decides Sales/Not-Sales and never decides whether an
Opportunity should exist — both are downstream, deterministic consequences of
what the reasoning caller already put in `EmailAnalysis`. The only place a
human (or Claude Desktop, on explicit instruction) can set `stage`/`owner`/
`value`/`currency`/`expected_close_date`/`next_action` is the
`update_opportunity_fields` MCP tool — nothing infers these from email
content, ever.

---

## 3. The MCP Agent Backend

### Model Context Protocol server

`app/mcp/server.py` exposes the system to any MCP-capable client (Claude
Desktop, Claude Code, or a custom client) over two transports:

- **stdio** (default) — for a locally-spawned client with no network hop.
- **streamable-http** — for the hosted Render deployment; `POST /mcp` for
  tool calls, `GET /health` unauthenticated for platform liveness checks.

Tool *definitions* here are thin wrappers; the actual logic lives in
`app/mcp/tools.py`, kept separate so the server file stays a pure protocol
boundary.

### Implemented tools (28)

*(Corrected from an earlier draft of this document, which miscounted this
list as 19 — independently re-verified twice by direct runtime introspection
of `mcp.list_tools()`. The single-LLM-call `process_email` tool that used to
sit here was later removed entirely — it depended on a server-side
`LLM_API_KEY` that was unreliable in this project (expired-key/401 errors),
and every real caller had already moved to the granular path below instead.)*

- **Write path** (granular, LLM-free — the caller does the reasoning,
  these tools only persist it): `ingest_email`, `persist_email_analysis`,
  `persist_context_delta`, `create_reply_draft`, `mark_email_completed`.
- **Additive record-keeping tools**: `set_reply_draft_gmail_id` (links an
  already-created Gmail draft id to its MongoDB reply_drafts record),
  `set_reply_withheld_reason` (records why a message needing a reply didn't
  get one drafted), `set_email_summary` (caches a Claude-authored summary of
  an email's body for the dashboard).
- **Entity-merge tool**: `merge_person_records` — a thin wrapper around
  `app.duplicate_consolidation`'s vetted single-mapping execution, for a
  caller confident two Person records are the same real person (see
  `preview_duplicate_person_candidates` below for the read-only classifier
  this complements).
- **Read/query path**: `list_processed_emails`, `search_emails`, `get_thread`,
  `list_people`, `list_projects`, `list_commitments`, `list_follow_ups`,
  `list_meetings` (optional `category`/`start_date`/`end_date` filters,
  closing FR-04's integration gap — see §2's `meetings` collection row),
  `list_reply_drafts`, `get_reply_draft`,
  `get_project_summary`, `get_company_summary`, `lookup_knowledge`.
- **Opportunity tools**: `list_opportunities` — read-only, filters by
  `org_id`/`status`/`project_id` (array-membership) — and
  `update_opportunity_fields` — the *only* mechanism for setting an
  Opportunity's human-managed CRM fields (`stage`/`owner`/`value`/`currency`/
  `expected_close_date`/`next_action`); deterministic, LLM-free, never touches
  the pipeline-derived fields. There is no `create_opportunity`/
  `resolve_opportunity` tool — creation/resolution happens automatically
  inside `persist_email_analysis` (see "Opportunity vs
  Project vs Email" above).
- **Safety-net tool**: `preview_duplicate_person_candidates` — strictly
  read-only; classifies possible duplicate Person records and reports them
  for human review. Never merges anything itself.
- **Executive query tools** (added post-BRD-gap-analysis, closing FR-01/FR-02):
  `ask_question` — exposes the previously-unreachable `app.query.service.execute_query`
  engine (intent classification, entity resolution, evidence assembly) over
  MCP, deterministically (no LLM call inside the tool) — and
  `whats_on_my_table` — one call combining pending replies, overdue
  follow-ups, upcoming commitments/meetings, and active projects.

### Ingestion pipeline (`app/pipeline.py::run_pipeline`)

Each email moves through a fixed stage sequence, with the stage recorded on
the document so a crash mid-pipeline never silently reprocesses or loses
work:

```
THREADED → ANALYZED → CONTEXT_BUILT → KNOWLEDGE_PROCESSED →
ENTITIES_PROCESSED → REPLY_PROCESSED → MEETING_PROCESSED → COMPLETED
```

- **Email triage**: a single LLM call classifies every message into exactly
  one of six labels (`Needs reply: ASAP`, `Needs reply`,
  `Needs reply: mention`, `Read only`, `Delete`, `Undecided`). (A separate
  `P1`/`P2` business-priority field and a `confidence` score were extracted
  and stored alongside this label in an earlier version; both were
  deliberately removed, along with `search_emails`'s `priority` filter and
  `whats_on_my_table`'s `p1_emails` category — see `docs/DATA_DICTIONARY.md`.)
- **Identity resolution / merging** (`app/entities/resolution.py`): a
  strict, tiered rule set that **never merges two people on name alone**.
  An email address is the only fully-trusted signal; a no-email mention
  either matches an existing person already resolved from this same
  email's envelope (fixing a real duplicate-creation bug from calendar
  invites) or falls through to a lower-confidence, no-email record.
  Actual duplicate consolidation is a separate, human-approved,
  snapshot-and-rollback-capable batch process (`app/duplicate_consolidation.py`)
  — never automatic.
- **Operator Profile**: the operator (`AGENT_EMAIL`/`AGENT_NAME`) is tracked
  via one dedicated Person record (`type: "operator"`), distinct from an
  external lead, instead of being silently skipped or duplicated.
- **Meeting category propagation**: `resolve_meeting` (`app/entities/resolution.py`)
  now accepts `project_or_pillar`, set from the email analysis's own
  `goal_pillar` field at the `app/pipeline.py` call site, stored on
  creation and additively backfilled onto an existing meeting that lacks
  it — never overwritten once set. `SALES`/`FINANCE` classification (see
  §2's `meetings` collection row) is derived only from this stored field.
  `PROSPECT`/`PROJECT` remain declared in `MeetingClassification` but are
  intentionally never assigned — no existing field reliably distinguishes
  them, and guessing from arbitrary keywords was rejected as a correctness
  risk; out of scope for V1 until a reliable signal exists.
- **Meeting category is server-side filterable through MCP** (FR-04's
  remaining integration gap, now closed): the `list_meetings` MCP tool
  (`app/mcp/tools.py`) gained an optional `category` parameter reusing
  `classify_meeting` as-is — one of `sales`/`finance`/`internal`/
  `customer`/`unknown` (case-insensitive); `prospect`/`project` are
  rejected with a `ValueError` rather than silently returning zero
  results, since `classify_meeting` never assigns either. `list_meetings`
  also gained optional `start_date`/`end_date` (ISO 8601, half-open
  `[start, end)` against the meeting's own `date`), combinable with
  `category` — e.g. "what sales meetings do I have today" is answered
  deterministically by `list_meetings(category="sales", start_date=...,
  end_date=...)`, with no natural-language special-casing anywhere in the
  tool itself. Both new parameters are optional and default to the exact
  prior behavior; `list_meetings()` with no arguments is unchanged.
- **Overdue follow-ups use the follow-up's own dates**: `retrieve_follow_ups`
  (`app/query/retrieval.py`) filters on `FollowUp.follow_up_latest_at`
  directly (UTC-aware throughout `app/query/dates.py`), and an `OVERDUE`
  window additionally excludes any follow-up whose `status` isn't
  `"active"` — so a resolved/dropped follow-up, or one with no
  `follow_up_latest_at` at all, never appears as overdue. This replaced an
  earlier version that joined through the parent Commitment's
  `committed_date`, which the `FollowUp` model no longer needs.

### Prompt engineering guardrails

The knowledge-extraction prompt (`_ANALYSIS_INSTRUCTIONS`, shared by both
the Claude and OpenAI/Groq providers) carries an explicit negative
boundary, added after a real incident where an internal engineering-minutes
email was extracted into 23 knowledge-graph facts as if they were business
data:

- Never extract meta-instructions about the AI agent's **own** behavior,
  architecture, or configuration.
- Never extract software engineering specs — schemas, database structures,
  code-level logic — as knowledge items.
- Every extracted field must describe the actual **business domain only**:
  people/org relationships, project timelines, deal terms, concrete
  business action items.
- If an entire email is an internal engineering discussion, summarize it
  in `summary`/`intent` only and leave every fact list empty.

This is prompt-engineering, not a hard code-level guarantee — it steers a
probabilistic model, verified today only by asserting the guardrail text
itself is present (this test suite never makes a real LLM call). A
deterministic second-pass classifier was considered and deliberately not
built, to avoid doubling LLM cost/latency, unless this recurs in production.

---

## 4. The Streamlit Dashboard (Frontend)

`app/ui/dashboard.py` is the **human-in-the-loop control center** — and,
notably, the *only* place in the entire system where a reply is actually
(simulated-)sent or a calendar event is actually created. Every other code
path only ever proposes; this file is where a human converts a proposal
into an action.

### Read-only mode for external viewers

`DASHBOARD_READ_ONLY=true` hides the Approve/Reject/Edit buttons (Reply
Approval tab) and the Create-on-calendar/Ignore buttons (Calendar Approval
tab) — the underlying records stay fully visible, only the action controls
disappear. Since this file is the sole trigger point for those actions
system-wide, this one flag is the complete safety boundary for handing the
dashboard to someone other than the operator (e.g. a colleague, deployed as
a second, independent Render service).

An optional `DASHBOARD_PASSWORD` gate sits in front of all of it: unset,
zero configuration is needed; set, nothing renders until the exact password
is entered (checked via `st.session_state`, so it isn't re-prompted on every
interaction within a session).

### Complete 7-tab collection coverage

Beyond the original Dashboard/Emails/Thread Explorer/Context
Evolution/Knowledge/Reply Approval/Calendar Approval tabs, every remaining
collection has its own tab: **People, Organizations, Projects, Commitments,
Follow-ups, Meetings, Personal Items** — each a thin `st.dataframe(...)`
over a corresponding one-line `Repository.find_many({})` helper in
`app/ui/data.py`. No collection in the system is invisible to the dashboard.

---

## 5. Safety & Core Design Principles

### Human-in-the-loop philosophy

- **Nothing auto-sends.** A reply is always generated as a *draft*
  (`reply_drafts`, `status: "awaiting_approval"`); `simulate_send` — the only
  function that ever "sends" anything — logs and marks status, and never
  calls a real email API regardless of environment configuration.
- **Nothing auto-schedules.** Meeting detection only ever produces a
  proposed `calendar_actions` document; a real event is created only when a
  human clicks "Create on my calendar" in the dashboard, which is disabled
  entirely under `DASHBOARD_READ_ONLY`.
- **Identity merges require explicit human sign-off.** The resolution
  pipeline never merges on name alone; `preview_duplicate_person_candidates`
  only ever *reports* candidates; an actual merge runs through a separate,
  human-approved, rollback-snapshotted batch process.
- **Every consequential production action in this project's own operating
  history was preceded by a live, read-only verification step** before any
  write — e.g., confirming real Atlas data before a merge, or reproducing a
  reported failure directly before proposing a fix, rather than acting on
  assumption.

### Structured logging

`app/config/logging.py::configure_logging(level, structured)` — controlled
by `LOG_FORMAT` (`text` default, or `json`). When `structured=True`, a
`_JSONFormatter` emits one JSON object per line for every pipeline
stage-transition log record (`app.pipeline.stage` logger, written from
`EmailRepository.set_stage`): `message_id`, `thread_id`, `stage`,
`duration_ms`, `error_type`, `error_message`, `timestamp`, `level`,
`message` — omitting any field that wasn't supplied for that record, and
never logging secrets/credentials (only pipeline metadata is passed
through `extra=`). All three real entrypoints (`main.py`,
`app/scheduler.py`, `app/mcp/server.py::main`) call `configure_logging`
before doing anything else; the MCP server previously never configured
logging at all despite being the actually-deployed Render service.

### Testing strategy

- **`mongomock` everywhere.** The entire test suite (1,148+ tests) runs
  against an in-memory MongoDB double — no real Atlas instance is ever
  touched by CI or local test runs.
- **Mock providers for every external dependency.** `MockLLMProvider`,
  `MockEmailProvider`, `MockCalendarProvider` give deterministic,
  zero-cost, zero-API-call behavior; `LLM_PROVIDER=mock` is the safe
  default requiring no credentials at all.
- **No real API calls in CI, ever** — including no live Anthropic/Groq
  calls; prompt-engineering changes are regression-tested by asserting on
  the constructed prompt/request content, not a live model response.
- **Isolated process caches.** Streamlit's `@st.cache_resource` is a
  process-global cache that can silently leak a stale database handle
  across otherwise-independent tests in the same pytest process — the
  dashboard's test suite explicitly clears it between tests to guarantee
  isolation.
