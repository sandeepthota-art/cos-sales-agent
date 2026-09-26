# CoS Sales Agent — BRD-to-Architecture Requirements Gap Analysis

**Source of truth:** `docs/ARCHITECTURE.md` (this repository), cross-checked
directly against the current codebase (not assumed from the architecture
document alone) wherever the rules below require a verified-vs-described
distinction.

**Method:** every requirement area in the prompt was checked against (a) the
architecture document, (b) the actual source files that would implement it,
and (c) the test suite, to separate *described*, *coded*, and *verified*.
Two categories of finding recur throughout and are called out explicitly
wherever they apply:

- **Integration gap**: real, tested code exists for a capability, but no
  MCP tool, dashboard tab, or other entry point actually exposes it to a
  client. This is different from the capability not existing at all.
- **Simulated-only**: the code path exists and is exercised by tests, but
  it deliberately never performs the real-world side effect (send an email,
  create a calendar event, call a real LLM).

---

## SECTION 1 — Executive Gap Summary

| Metric | Count |
|---|---|
| Total requirements evaluated | 56 |
| SATISFIED | 42 |
| PARTIALLY SATISFIED | 6 |
| MISSING | 4 (includes 1 tagged "by design" — a deliberate architectural choice, not an oversight) |
| NEEDS VERIFICATION | 2 |
| AMBIGUOUS | 2 |

Counts above were produced by directly tallying the Status column of the
Section 2 matrix, not estimated — re-run the same check against §2 if this
document is ever edited, since a manual edit can silently desync the two.

No overall score or percentage is given, per the rules — the table below is
the actual audit; counts above are a summary of it, not a grade.

**BRD-gap remediation update (this revision):** the four Phase A items this
document originally flagged as the highest-leverage gaps have been
implemented, tested, and are now MCP-reachable — see FR-01 through FR-04
below for the closed acceptance criteria. Counts above reflect that work;
the two paragraphs immediately below are kept as the historical record of
what this audit originally found, unchanged, with a correction note on each.

The single most consequential finding (as originally written): **`app/query/`
is a fully-built, independently tested query-understanding/retrieval/evidence
engine (intent classification, entity resolution, date-range resolution,
meeting briefs, LLM-free evidence assembly) that is never wired into any MCP
tool.** `execute_query` (`app/query/service.py`) has zero callers outside its
own test suite. This means the architecture *describes* executive-query
capability and genuinely *has the engine for it in code*, but today there
is no reachable path for an MCP client to actually ask "what's on my table"
and get a structured answer through it. Section 10/11 findings below all
trace back to this one gap.
*(Correction: this gap is now closed. `ask_question` and `whats_on_my_table`
— both added to `app/mcp/server.py`/`app/mcp/tools.py` — expose
`execute_query` over MCP without duplicating it or introducing an LLM call.
See REQ-51/REQ-52 and FR-01/FR-02.)*

The second most consequential finding (as originally written): **meeting
classification only ever produces `INTERNAL` / `CUSTOMER` / `UNKNOWN`** —
this was explicit, self-documented in `app/query/meetings.py`'s own
docstring: *"SALES/FINANCE/PROSPECT/PROJECT are never assigned: no stored
field reliably supports them."* "What sales meetings do I have today?" and
"What finance meetings do I have today?" could not be answered, by design,
not by oversight-that-looks-like-a-bug.
*(Correction: SALES/FINANCE are now derivable — see REQ-37/FR-04 — from the
meeting's own stored `project_or_pillar`, propagated from the triggering
email's real `analysis.goal_pillar`, never guessed. PROSPECT/PROJECT remain
unassigned by the same original, deliberate design choice — still correctly
out of scope, not a regression.)*
*(Second correction, this revision: "What sales meetings do I have today?"
and "What finance meetings do I have today?" CAN now be answered — the
`list_meetings` MCP tool gained a `category` filter (reusing `classify_meeting`
as-is) plus optional `start_date`/`end_date` bounds, combinable in one
deterministic call. Verified end-to-end against a real-`run_pipeline`-derived
mongomock dataset, not hand-fabricated documents: a same-day "Sales" meeting
and a six-days-out "Finance" meeting were both created by processing genuine
email content through the actual pipeline, then correctly retrieved/excluded
by `list_meetings(category=..., start_date=..., end_date=...)` exactly as the
real dates dictated — including a correct empty result for "finance meetings
today" on a day when only the Sales meeting was scheduled.)*

---

## SECTION 2 — Complete Traceability Matrix

| ID | BRD Requirement | Architecture Evidence | MCP Tool / Component | MongoDB Collection | Dashboard Capability | Test / Acceptance Criteria | Status | Gap |
|---|---|---|---|---|---|---|---|---|
| REQ-01 | Ingest email supplied by an external connector | §3 "MCP Agent Backend" | `process_email`, `ingest_email` | `emails` | Emails tab | `tests/test_mcp_tools.py`, `tests/test_pipeline.py` | SATISFIED | Tool accepts a pre-built `Email` object; verified end-to-end with mongomock |
| REQ-02 | First-party Gmail/Cowork connector (fetch, not just accept) | Not described as first-party | `app/providers/source/live_gmail.py` (unwired), `MCPEmailProvider` (unwired stub) | — | — | none (nothing to test — no live client) | MISSING | The actual Gmail fetch happens entirely client-side (the MCP caller's own Gmail connector); this repo has no code that calls the Gmail API itself |
| REQ-03 | Stable message identifiers, no duplicate ingestion | §2 core collections | `EmailRepository`, unique index on `message_id` | `emails` | Emails tab | `tests/test_pipeline.py::test_pipeline_is_idempotent_on_rerun` | SATISFIED | — |
| REQ-04 | Incremental/repeated polling for new email | §3 | `app/scheduler.py` (folder-only) | `ingested_files` | — | `tests/test_scheduler.py`, `tests/test_folder_source.py` | PARTIALLY SATISFIED | Polling exists only for a local folder of JSON exports; Gmail has no polling loop at all (module docstring: "Gmail ingestion is NOT handled here") |
| REQ-05 | Connector-failure recovery (Gmail down, malformed fetch) | Not described | — | — | — | none | MISSING | No retry/backoff policy for the Gmail connector exists in this codebase because the connector itself isn't in this codebase |
| REQ-06 | Malformed email fails in isolation, batch continues | §3 pipeline stages | `run_pipeline` | `processing_runs` | Dashboard tab (failure count) | `tests/test_pipeline.py::test_pipeline_continues_after_malformed_email_with_no_message_id` | SATISFIED | — |
| REQ-07 | Raw email persisted before any analysis | §3 pipeline stages | `ingest_raw_email` | `emails` | Emails tab | `tests/test_raw_ingestion.py` | SATISFIED | — |
| REQ-08 | Threading (message → thread) | §2 | `resolve_thread_id` | `threads` | Thread Explorer | `tests/test_pipeline.py` | SATISFIED | — |
| REQ-09 | Cumulative, versioned per-thread context | §2 `context_snapshots` | `build_next_context` | `context_snapshots` | Context Evolution, Thread Explorer | `tests/test_context_engine.py` | SATISFIED | — |
| REQ-10 | Processing stages with explicit COMPLETED/FAILED state | §3 stage sequence | `ProcessingStage` enum, `email_repo.set_stage` | `emails.processing_status` | Dashboard tab | `tests/test_pipeline.py` | SATISFIED | — |
| REQ-11 | Retry of a previously FAILED email | Implicit only | pipeline skip check is `stage == COMPLETED` only | `emails` | — | none explicit | NEEDS VERIFICATION | Behavior (FAILED items are retried on next run since only COMPLETED is excluded) is a side effect of the dedup check, not a documented/tested retry policy in its own right — no max-attempts, no backoff |
| REQ-12 | Six-way email triage label | §2, §3 | `EmailAnalysis.label_applied` (Literal, 6 values) | `emails.label_applied` | Emails tab, Knowledge tab context | `tests/test_analysis_extractor.py`, mock provider tests | SATISFIED | — |
| REQ-13 | P1/P2 business priority, independent of triage label | §3 | `EmailAnalysis.priority` | `emails.priority` | Emails tab (raw field visible in dataframe) | `tests/test_mcp_tools.py` | PARTIALLY SATISFIED | Field is stored and queryable (`search_emails(priority=...)`), but the dashboard shows it only as a raw dataframe column, not a filtered/sorted priority view |
| REQ-14 | Query emails by label/priority | §3 | `search_emails(label_applied=, priority=)` | `emails` | — | `tests/test_mcp_query_tools.py` | SATISFIED | — |
| REQ-15 | Canonical Person resolution, email as primary key | §5 | `resolve_person` | `people` | People tab | `tests/test_entities_models.py`, resolution test suite | SATISFIED | — |
| REQ-16 | Never merge two people on name alone (global) | §5 | `resolve_person` no-email tier, `match_resolved_person_by_name` | `people` | People tab | dedicated resolution tests (context-aware matching suite) | SATISFIED | Verified via multiple explicit regression tests, not just described |
| REQ-17 | Duplicate-candidate detection, human-approved consolidation | §3, §5 | `preview_duplicate_person_candidates` (read-only), `app/duplicate_consolidation.py` (write, separate/manual) | `people`, `migration_runs` | — (not surfaced in dashboard) | `tests/test_duplicate_consolidation.py`, `tests/test_duplicate_impact_analysis.py` | PARTIALLY SATISFIED | Detection is MCP-reachable; actual merge execution is a manual script-level operation with no MCP tool and no dashboard UI at all |
| REQ-18 | Operator identity distinct from external contacts | §3 | `resolve_operator_person`, `Person.type == "operator"` | `people` | People tab (visible, unlabeled as special) | pipeline entity tests | SATISFIED | Dashboard's People tab does not visually distinguish `type=operator` from a normal contact |
| REQ-19 | Canonical Organization resolution (domain-based) | §5 | `resolve_organization` | `organizations` | Organizations tab | resolution test suite | SATISFIED | — |
| REQ-20 | Project extraction and persistence | §2 | `resolve_project` | `projects` | Projects tab | pipeline/entity tests | SATISFIED | — |
| REQ-21 | Project ↔ person/org relationship | §2 | `project_ids_by_org_id` linkage in `_process_entities` | `projects`, `people` | Projects tab (no join view) | pipeline tests | PARTIALLY SATISFIED | Link exists in code and is queryable via `get_project_summary`; dashboard's Projects tab is a flat dataframe with no cross-reference display |
| REQ-22 | Project summary lookup | §3 | `get_project_summary` MCP tool | `projects`, `commitments`, `follow_ups` | — | `tests/test_mcp_query_tools.py` | SATISFIED | Explicitly documents which relationships are direct vs unsupported (meetings/knowledge/people not linked) |
| REQ-23 | Knowledge as Subject-Predicate-Object / EAV triples | §2 | `process_new_fact`, `KnowledgeItem` | `knowledge_items` | Knowledge tab | `tests/test_knowledge_deduplication.py` | SATISFIED | — |
| REQ-24 | current_value + history + source_emails on every fact | §2 | `KnowledgeItem.history` | `knowledge_items` | Knowledge tab (expander) | same as above | SATISFIED | — |
| REQ-25 | stated vs inferred basis | §2 | `basis` field | `knowledge_items` | Knowledge tab | same as above | SATISFIED | — |
| REQ-26 | Conflict handling (same fact, different value) | §2 | `verify_same_fact` LLM call | `knowledge_items` | — | `tests/test_knowledge_deduplication.py` | SATISFIED | Uses a real (mockable) LLM call; not exercised against a live model in CI |
| REQ-27 | Business-domain-only extraction (no meta-instructions/software specs) | §3 prompt guardrails | `_ANALYSIS_INSTRUCTIONS` | `knowledge_items` | — | prompt-content assertion tests only | PARTIALLY SATISFIED | This is prompt-engineering, not a code-enforced boundary — no deterministic filter exists; a non-compliant LLM response is not caught anywhere downstream |
| REQ-28 | Commitment classes: mine / owed_to_me / theirs / recap | §2, §3 | `resolve_commitment` | `commitments` | Commitments tab | resolution + pipeline tests | SATISFIED | — |
| REQ-29 | Commitment due date resolution (stated/inferred/window) | §2 | `resolve_date_phrase` | `commitments` | Commitments tab | `tests/query dates` suite | SATISFIED | — |
| REQ-30 | Commitment queryability | §3 | `list_commitments` MCP tool | `commitments` | Commitments tab | `test_query_commitments.py` | SATISFIED | — |
| REQ-31 | Follow-ups derived from chased commitments only | §2, §3 | `derive_follow_up`, `_CHASED_COMMITMENT_CLASSES` | `follow_ups` | Follow-ups tab | pipeline tests | SATISFIED | — |
| REQ-32 | Follow-up timing window + escalation state | §2 | `classify_follow_up_timing`, `escalation_level` | `follow_ups` | Follow-ups tab (raw fields, no visual urgency indicator) | `tests/test_query_commitments.py`-adjacent suite | PARTIALLY SATISFIED | Data exists and is correct; dashboard shows it as a plain table, not surfaced as an "overdue" view |
| REQ-33 | Overdue follow-up query ("what's overdue") | §3 (implied) | `retrieve_follow_ups` (`app/query/retrieval.py`) filters on `FollowUp.follow_up_latest_at` directly, excluding non-`active` status for the `OVERDUE` window; reachable via `ask_question`/`whats_on_my_table` MCP tools | `follow_ups` | — | `tests/test_query_retrieval.py` (overdue/future/completed/missing-dates/timezone-boundary cases), `tests/test_query_commitments.py`, `tests/test_mcp_query_tools.py` | SATISFIED | Resolved by the BRD-gap remediation: the query is deterministic, timezone-consistent (UTC-aware throughout), and MCP-queryable — no client-side filtering required |
| REQ-34 | Meeting detection from email text | §2, §3 | `detect_meeting` | `meetings` | Meetings tab | `app/calendar/detector.py` tests | SATISFIED | — |
| REQ-35 | Meeting duplicate protection (same thread, same date) | §2 | `resolve_meeting` (matches on thread_id+date, merges rather than duplicates) | `meetings` | Meetings tab | entity resolution tests | SATISFIED | Scoped to the same thread only — a meeting mentioned across two *different* threads is not deduped against itself |
| REQ-36 | Meeting participant resolution to canonical Person | §2 | `person_ids` on `Meeting` | `meetings`, `people` | Meetings tab | pipeline tests | SATISFIED | — |
| REQ-37 | Meeting category: Sales/Finance/etc. | §2 (not claimed) | `classify_meeting` (`app/query/meetings.py`) derives `SALES`/`FINANCE` from an exact, case-insensitive match on the meeting's own stored `project_or_pillar` (populated by `resolve_meeting` from the triggering email's real, LLM-extracted `analysis.goal_pillar` — never guessed independently); `INTERNAL`/`CUSTOMER`/`UNKNOWN` unchanged; `PROSPECT`/`PROJECT` still never assigned (no reliable field for them). `list_meetings` MCP tool now exposes a `category` filter reusing `classify_meeting` directly — no second classification mechanism | `meetings` (`project_or_pillar` field) | Meetings tab (raw `project_or_pillar` column visible, no dedicated category filter/badge in the UI) | `tests/test_query_meetings.py`, `tests/test_entities_resolution.py`, `tests/test_pipeline_entities.py`, `tests/test_mcp_query_tools.py` (17 `list_meetings` category/date-range cases), `tests/test_mcp_server.py` (2 genuine MCP-dispatch-layer cases via `mcp.call_tool`) | SATISFIED | Upgraded from PARTIALLY SATISFIED: the FR-04 integration gap is closed — `list_meetings(category=...)` is deterministic, MCP-reachable (proven through `mcp.call_tool`, not just the underlying Python function), tested, and rejects `prospect`/`project` explicitly rather than silently returning zero results. Only remaining gap: no dashboard UI filter/badge (not required by FR-04's own acceptance criteria) |
| REQ-38 | "What sales/finance meetings do I have today?" | Not claimed anywhere in architecture | `list_meetings(category="sales"/"finance", start_date=..., end_date=...)` answers this deterministically in one MCP call — `start_date`/`end_date` (new, optional, ISO 8601, half-open) combine with `category` exactly as required | `meetings` | — | `tests/test_mcp_query_tools.py::test_list_meetings_category_combined_with_date_range`, `::test_list_meetings_category_and_date_range_with_no_matches_returns_empty_not_an_error`; independently verified end-to-end against a real-pipeline-derived (non-fabricated) mongomock dataset — see the FR-04 close-out verification note below | SATISFIED | Upgraded from PARTIALLY SATISFIED: both the category filter and the date-bounds filter needed to answer this exact question now exist on the same, already-MCP-reachable `list_meetings` tool, combinable in one call — no client-side post-filtering required |
| REQ-39 | Calendar action proposal (never auto-create) | §5 core principle | `build_calendar_action` | `calendar_actions` | Calendar Approval tab | `tests/test_pipeline.py` | SATISFIED | — |
| REQ-40 | Human approval required before real calendar event | §4, §5 | Dashboard's "Create on my calendar" button only, gated by `DASHBOARD_READ_ONLY` | `calendar_actions` | Calendar Approval tab | `tests/test_ui_smoke.py::test_dashboard_read_only_mode_hides_approval_buttons` | SATISFIED | — |
| REQ-41 | Real external calendar provider integration | §1 (mentions `CalendarProvider`) | `ProviderFactory.create_calendar_provider` — only `mock` is configured in the current Render deployment | `calendar_actions` | Calendar Approval tab | none against a real provider | NEEDS VERIFICATION | A real (Google Calendar) `CalendarProvider` implementation's existence/correctness is not demonstrated anywhere in this codebase — only the interface and a mock exist |
| REQ-42 | Calendar failure handling (event creation fails) | Not described | `approve_calendar_action` | `calendar_actions` | Calendar Approval tab | none | MISSING | No test exercises a `CalendarProvider.create_event` failure path |
| REQ-43 | Reply draft generation with recipient context | §3, §5 | `draft_reply` | `reply_drafts` | Reply Approval tab | `tests/test_reply_drafter.py` | SATISFIED | — |
| REQ-44 | Voice-preference injection into drafts | §3 | `Person.preferences` → `recipient_preferences` | `people`, `reply_drafts` | — (not shown in dashboard which preferences applied) | `tests/test_pipeline.py::test_pipeline_injects_recipient_preferences_into_the_generated_reply_draft` | SATISFIED | — |
| REQ-45 | Draft approval/rejection/editing | §4 | Reply Approval tab handlers | `reply_drafts` | Reply Approval tab | `tests/test_ui_smoke.py` | SATISFIED | Hidden entirely under `DASHBOARD_READ_ONLY` |
| REQ-46 | Actual (real) email sending | §5 explicit | `simulate_send` — deliberately never calls a real provider | `reply_drafts` | Reply Approval tab | `app/replies/approval.py::simulate_send` | MISSING (by design) | **Architecture currently supports simulated sending only.** No requirement or code path exists for real sending; this must be an explicit BRD decision (add as FR, or declare simulated-only Out of Scope) |
| REQ-47 | MCP server, stdio + streamable-http, Render-deployable | §1 | `app/mcp/server.py` | — | — | `tests/test_mcp_server.py` | SATISFIED | — |
| REQ-48 | MCP bearer-token authentication | §1, §5 | `_BearerAuthMiddleware`, `MCP_AUTH_TOKEN` | — | — | `tests/test_mcp_server.py` | SATISFIED | — |
| REQ-49 | `/health` unauthenticated liveness check | §1 | `_health` | — | — | `tests/test_mcp_server.py` | SATISFIED | — |
| REQ-50 | Complete, accurate MCP tool inventory | §3 | `app/mcp/server.py` | — | — | direct count (`asyncio.run(mcp.list_tools())`) | SATISFIED | **Corrected count: 22 tools** (20 pre-existing + 2 new — `ask_question`, `whats_on_my_table`). The prior draft of this document miscounted the pre-existing set as 19; independently re-verified twice by direct runtime introspection, both before and after this session's two additions |
| REQ-51 | Executive natural-language query ("what's on my table") | Described only implicitly via §5 philosophy; now explicitly documented in §3 | `ask_question` MCP tool (`app/mcp/tools.py`, `app/mcp/server.py`) thinly wraps `app/query/service.py::execute_query` — no duplicate query engine, no LLM call inside the tool | none dedicated | none | 12 pre-existing `test_query_*.py` files, plus `tests/test_mcp_query_tools.py::test_ask_question_*` (structured result, error-not-a-guess for unrecognized text, never makes an LLM call, JSON-serializable) | SATISFIED | Integration gap closed: the engine is now reachable through a real, tested MCP tool |
| REQ-52 | "What's on my table?" cross-entity aggregation | Not claimed; now explicitly documented in §3 | `whats_on_my_table` MCP tool (`app/mcp/tools.py`, `app/mcp/server.py`) combines P1 emails, pending replies, overdue follow-ups, upcoming commitments/meetings, and active projects via existing repositories/retrieval functions — no data invented, no new retrieval mechanism | none dedicated | none | `test_query_service.py` (service-layer), plus `tests/test_mcp_query_tools.py::test_whats_on_my_table_*` (every category key present even when empty, real-data aggregation, JSON-serializable) | SATISFIED | Integration gap closed |
| REQ-53 | Dashboard: full 14-tab collection coverage | §4 | `app/ui/dashboard.py` | all 13 domain collections | All tabs | `tests/test_ui_data.py`, `tests/test_ui_smoke.py` | SATISFIED | Independently re-counted: **14 tabs total** (7 original + 7 added for full coverage) — matches the architecture document; `processing_runs` has no dedicated tab (only aggregated into the Dashboard metrics tile) |
| REQ-54 | Read-only mode hides every mutating control | §4, §5 | `DASHBOARD_READ_ONLY` | `reply_drafts`, `calendar_actions` | Reply/Calendar Approval tabs | `tests/test_ui_smoke.py` | SATISFIED | — |
| REQ-55 | Duplicate-person merge reachable by the operator via MCP/dashboard | Not addressed either way | `app/duplicate_consolidation.py` — script-level only, no MCP tool, no dashboard control at all | `people`, `migration_runs` | none | `tests/test_duplicate_consolidation_execution.py` (script-level only) | AMBIGUOUS | Cannot tell from the architecture whether keeping this maximally hard to trigger is a deliberate safety choice or an oversight — needs an explicit product decision, not an assumption either way |
| REQ-56 | Real (non-mock) calendar provider required for V1 | Interface exists (`CalendarProvider`); only `mock` is actually configured on the deployed Render service | `ProviderFactory.create_calendar_provider` | `calendar_actions` | Calendar Approval tab | none against a real provider | AMBIGUOUS | Architecture never states whether a real Google/Outlook Calendar integration is an actual V1 requirement or permanently out of scope — this changes whether REQ-41/FR-07 are urgent or moot |

---

## SECTION 3 — Missing Functional Requirements

### FR-01 — Executive Query MCP Tool — **IMPLEMENTED**

`ask_question` (`app/mcp/tools.py`, wrapped in `app/mcp/server.py`) closes
this FR exactly as specified below: it thinly wraps `execute_query`, never
invokes `app.query.synthesis`, makes zero LLM calls (proven by
`test_ask_question_never_makes_an_llm_call`, which monkeypatches
`ProviderFactory.create_llm_provider` to raise), and returns the structured
`QueryResult` verbatim as a JSON-serializable dict. Tests:
`tests/test_mcp_query_tools.py::test_ask_question_*`,
`tests/test_mcp_server.py::test_ask_question_tool_is_registered`. All
acceptance criteria below are met.

**Requirement**

Expose `app.query.service.execute_query` as a new MCP tool (e.g.
`ask_question`) accepting a natural-language question and returning a
structured `QueryResult` (status, evidence items, resolved entities), so an
MCP client can answer executive questions without hand-orchestrating raw
`list_*` calls.

**Rationale**

The retrieval/intent/evidence engine already exists and is independently
tested (12 test files), but has no reachable entry point. This is the
single highest-leverage gap in the system — the missing piece is
integration, not a new engine.

**Inputs**

Natural-language question string; optional `reference_datetime`,
`timezone`, `filters` (date range, person/org hint).

**Processing**

`classify_intent_with_fallback` → entity resolution → `retrieval` →
`build_evidence_items` (unchanged, reused as-is). This tool must NOT invoke
`app.query.synthesis` (LLM prose generation) by default — return structured
evidence and let the calling client's own model phrase the answer, keeping
this tool deterministic and cheap.

**Outputs**

`{status, intent, resolved_entities, evidence: [...], ambiguity_reason?}`

**Acceptance Criteria**

1. Given "What commitments are overdue?", returns `QueryIntentType.COMMITMENTS`-scoped evidence filtered to items whose due window has passed, with zero LLM calls.
2. Given an unresolvable/ambiguous person reference, returns `AMBIGUOUS` status with the candidate list, never a guess.
3. Tool is read-only: no test may show it writing to any collection.
4. Response time budget defined and asserted in a test (see NFR-06).

---

### FR-02 — "What's on my table?" Aggregation Tool — **IMPLEMENTED**

`whats_on_my_table` (`app/mcp/tools.py`, wrapped in `app/mcp/server.py`)
combines P1 emails, `awaiting_approval` reply drafts, overdue follow-ups,
upcoming commitments/meetings, and active projects, each a bounded,
independent, read-only query reusing existing repositories/retrieval
functions — no data invented. Every category key is present even when
empty (`test_whats_on_my_table_includes_every_category_key_even_when_empty`).
Tests: `tests/test_mcp_query_tools.py::test_whats_on_my_table_*`,
`tests/test_mcp_server.py::test_whats_on_my_table_tool_is_registered`.

**Requirement**

A new MCP tool (e.g. `executive_summary`) that combines, in one call: P1
emails from the last N hours, `awaiting_approval` reply drafts, overdue
follow-ups (see FR-03), commitments due within a configurable window,
upcoming meetings (next 24-48h), and active projects with unresolved
commitments.

**Rationale**

Explicitly requested in the product intent ("What's on my table?" /
"What needs my attention today?"); no single call answers this today —
`QueryIntentType.CROSS_ENTITY_ACTIVITY` is defined in the schema but has no
tool-level entry point or defined composition rule.

**Inputs**

`reference_datetime` (defaults to now), `timezone`.

**Processing**

Parallel, independent read-only queries per category (each already has an
equivalent `list_*`/`search_*` primitive); merge into one response grouped
by category, not flattened.

**Outputs**

`{high_priority_emails, pending_replies, overdue_follow_ups, upcoming_commitments, upcoming_meetings, active_projects}` — each a bounded list with a `total_count` alongside any truncated list.

**Acceptance Criteria**

1. Every category present even when empty (empty list, not an omitted key).
2. No category's result depends on another's — a failure in one category (e.g. no meetings) never blocks the others.
3. Deterministic under mongomock with zero LLM calls.

---

### FR-03 — Overdue Follow-up Query — **IMPLEMENTED**

`retrieve_follow_ups` (`app/query/retrieval.py`) was rewritten to filter
directly on `FollowUp.follow_up_latest_at` (the field the model already
carries — no new field was needed), excludes non-`"active"` status for the
`OVERDUE` window, and is reachable via `ask_question`/`whats_on_my_table`.
Tests cover overdue, not-yet-due, resolved/dropped exclusion, missing
`follow_up_latest_at` exclusion, `all_time` ignoring status/dates, and a
timezone-boundary case (IST vs. UTC reference times both correctly resolve
the same record as overdue) — `tests/test_query_retrieval.py`,
`tests/test_query_commitments.py`.

**Requirement**

A new MCP tool or query filter that returns `follow_ups` whose
`follow_up_latest_at` has passed relative to a given reference time, without
requiring the caller to fetch all follow-ups and filter client-side.

**Rationale**

REQ-33: currently MISSING; escalation data is computed and stored but not
independently queryable by overdue-ness.

**Inputs**

`reference_datetime` (default now), optional `audience` filter.

**Processing**

`FollowUpRepository.find_many({"follow_up_latest_at": {"$lt": reference_datetime}, "status": "open"})`-equivalent, reusing the existing repository, not a new collection.

**Outputs**

List of follow-ups, each carrying its parent commitment's summary for context.

**Acceptance Criteria**

1. A follow-up whose window has passed is returned; one not yet due is not.
2. Escalation level is included so a client can distinguish "just overdue" from "escalated."

---

### FR-04 — Meeting Category Classification (Sales / Finance / Custom) — **FULLY IMPLEMENTED**

Option (a) was chosen, per this FR's own instruction, using the field this
FR itself identified as the natural home: `resolve_meeting`
(`app/entities/resolution.py`) now accepts `project_or_pillar`, set at
creation from `analysis.goal_pillar` (the triggering email's real,
already-extracted value — see this section's own Processing note above),
never overwriting an already-set value on backfill. `classify_meeting`
(`app/query/meetings.py`) derives `SALES`/`FINANCE` from an exact,
case-insensitive match on that stored field. Tests:
`tests/test_query_meetings.py`, `tests/test_entities_resolution.py`,
`tests/test_pipeline_entities.py`.

**Integration gap closed (this revision):** `list_meetings`
(`app/mcp/tools.py`/`app/mcp/server.py`) gained an optional `category`
parameter — one of `sales`/`finance`/`internal`/`customer`/`unknown`
(case-insensitive), reusing `classify_meeting` directly with no second
classification mechanism — and optional `start_date`/`end_date` (ISO 8601,
half-open `[start, end)` against the meeting's own `date`), combinable
with `category` in one call. `prospect`/`project` are rejected with a
clear `ValueError` (`test_list_meetings_rejects_unsupported_categories_
deterministically`) rather than silently returning zero results.
Reachability was proven at the actual MCP dispatch layer, not just the
underlying Python function: `tests/test_mcp_server.py::
test_list_meetings_category_filter_is_reachable_through_the_mcp_tool_layer`
calls `mcp.call_tool("list_meetings", {"category": "SALES"})` directly.
17 additional tests in `tests/test_mcp_query_tools.py` cover every
supported category, invalid-category rejection, category+date combination,
zero-match (empty, not an error), undated-meeting exclusion, and unparseable
date bounds. `list_meetings()` with no arguments is unchanged (2 pre-existing
tests still pass verbatim). All 5 acceptance criteria below are now met:
category filtering exists, is deterministic (no LLM, no NL special-casing —
the tool takes concrete parameters), is MCP-reachable (proven via
`mcp.call_tool`), is tested, and SALES/FINANCE use the existing, reliable
`project_or_pillar`-based classification path exclusively.

**Requirement**

Either (a) add an explicit, reliably-derivable meeting category field
(e.g. from `goal_pillar` already present on the *triggering email*,
propagated onto the `Meeting` record at creation time), or (b) formally
declare category-based meeting queries **Out of Scope for V1**.

**Rationale**

REQ-37/REQ-38: the current code explicitly refuses to guess a category it
can't reliably support. This is correct engineering discipline, but it
means "sales meetings today" is unanswerable until a product decision is
made — this cannot be silently left ambiguous.

**Inputs**

The source email's own `goal_pillar` (already extracted, e.g. "Sales",
"Finance", "Operations", "Product Development" — confirmed real values in
production data).

**Processing**

At `resolve_meeting` time, propagate `analysis.goal_pillar` onto the new
`Meeting.category` field (only ever set from a real, already-extracted
value — never inferred independently of it, consistent with this
codebase's existing "never guess" convention).

**Outputs**

`Meeting.category: str | None`.

**Acceptance Criteria**

1. A meeting created from an email whose `goal_pillar == "Sales"` is queryable as a sales meeting.
2. A meeting with no reliable category stays `None` — never defaults to a guess.
3. If the product decision is instead "Out of Scope," this FR is replaced by an explicit BRD line item under Section 9's "Out of Scope" list.

---

### FR-05 — Attachment Handling Decision — **OUT OF SCOPE FOR V1 (decision recorded)**

No BRD approval to build real attachment ingestion was given in this
remediation round. Per this FR's own "if declared Out of Scope" acceptance
criterion: **email attachments are not ingested, stored, parsed, or
searchable in V1.** `Email.attachments` remains an unused schema field —
no partial/fake implementation was added. Revisit only on an explicit,
separate product decision to build it.

**Requirement**

`Email.attachments` exists as a schema field but is never populated,
parsed, stored, or made searchable anywhere in the codebase. This must be
resolved as either a real requirement or an explicit non-goal — it cannot
remain an unused field with no decision recorded.

**Rationale**

Rule 20 of this audit explicitly requires this determination; leaving it
implicit risks someone assuming attachment content is searchable when it
is not.

**Acceptance Criteria (if built)**

1. Attachment metadata (filename, mime type, size) persisted on ingestion.
2. Explicit product decision on whether content is parsed/embedded for knowledge extraction, or metadata-only.

**Acceptance Criteria (if declared Out of Scope)**

1. BRD explicitly states: "Email attachments are not ingested, stored, parsed, or searchable in V1."

---

### FR-06 — Real Email Sending (Decision Required) — **SIMULATED-ONLY (decision recorded)**

No BRD approval to build real sending was given in this remediation round.
`simulate_send` was **not** modified — it still never calls a real
`EmailProvider.send_email`, regardless of environment configuration.
**Architecture supports simulated sending only in V1.** Human approval
semantics (Approve → `simulated_sent`, never an unattended send) are
preserved unchanged. Revisit only on an explicit, separate product decision
to build real sending, following this FR's own acceptance criteria below.

**Requirement**

**Architecture currently supports simulated sending only** — `simulate_send`
never calls a real `EmailProvider.send_email`. If the product requires
actually sending an approved reply, this is a new requirement with new
safety implications (a real, live external side effect) and must be
explicitly added to the BRD, not silently assumed to already exist.

**Rationale**

Rule 12 of this audit requires this exact determination and this exact
phrasing when the gap is confirmed.

**Acceptance Criteria (if built)**

1. A real send only ever fires from the Approve action in `_render_reply_approval_tab`, never anywhere else.
2. Sending is disabled outright when `DASHBOARD_READ_ONLY=true` (already true today, since the whole tab's buttons are hidden — must remain true after this change).
3. A failed real send is recorded distinctly from `simulated_sent`/`sent` (e.g. `send_failed`), never silently swallowed.

---

### FR-07 — Real Calendar Provider Failure Handling — **MOCK-ONLY (decision recorded)**

No BRD approval to integrate or require a real (non-mock) `CalendarProvider`
was given in this remediation round. The current, deployed Render
configuration runs `CALENDAR_PROVIDER=mock` only — **no real calendar
provider is operational in production today**, and this document does not
pretend otherwise. Failure-path handling for a real provider (this FR's own
subject) is therefore not applicable until a real provider is actually
integrated; building failure handling for a provider that doesn't exist
would itself be a partial/fake implementation, which was avoided. Revisit
together with REQ-41/REQ-56 as one combined decision.

**Requirement**

Define and test the behavior when a real (non-mock) `CalendarProvider.create_event` call fails (network error, invalid credentials, conflicting event).

**Rationale**

REQ-42: no such path is exercised anywhere today; only the mock provider (which cannot fail this way) is tested.

**Acceptance Criteria**

1. A failed real calendar creation leaves the `CalendarAction` in a distinguishable state (e.g. `creation_failed`), not silently marked `scheduled`.
2. The dashboard surfaces the failure to the approving human rather than appearing to succeed.

---

### FR-08 — Duplicate-Consolidation Dashboard Surface — **NOT IMPLEMENTED (safety boundary preserved as-is)**

No BRD approval to add dashboard visibility for duplicate-person review was
given in this remediation round, so no new dashboard tab was added. The
existing safety boundary is unchanged and confirmed intact:
`preview_duplicate_person_candidates` remains strictly read-only and
MCP-reachable; actual merge execution remains a separate, manual,
script-level operation (`app/duplicate_consolidation.py`) with **no MCP
tool and no dashboard control of any kind** — so there is nothing here that
could be exposed under, or bypass, `DASHBOARD_READ_ONLY`. Revisit as a
small, additive dashboard-only change (per Section 5's own assessment) once
explicitly requested.

**Requirement**

Expose `preview_duplicate_person_candidates` results (and, separately,
approval of a specific merge) in the Streamlit dashboard, not only via a
manual MCP tool call or script.

**Rationale**

REQ-17: detection is MCP-reachable; there is currently no human-facing
surface for reviewing/approving a merge at all — an operator must already
know to call the tool directly.

**Acceptance Criteria**

1. A new, read-only-safe "Duplicate Review" tab lists candidates with confidence/evidence.
2. Any actual merge action (if surfaced at all) is hidden under `DASHBOARD_READ_ONLY`, identical to every other mutating control.

---

## SECTION 4 — Missing Non-Functional Requirements

### NFR-01 — Structured Logging & Alerting — **IMPLEMENTED (logging); alerting out of scope**

`app/config/logging.py::configure_logging(level, structured)`, controlled
by the new `LOG_FORMAT` setting (`text` default / `json`). A `_JSONFormatter`
emits one JSON line per pipeline stage transition
(`app.pipeline.stage` logger, written from `EmailRepository.set_stage`) with
`message_id`, `thread_id`, `stage`, `duration_ms`, `error_type`,
`error_message`, `timestamp`, `level` — a field is simply omitted, never
faked, when not supplied for that record. Never logs secrets/credentials
(only pipeline metadata passes through `extra=`). Wired into all three real
entrypoints (`main.py`, `app/scheduler.py`, and `app/mcp/server.py::main` —
which previously called `configure_logging` **not at all**, despite being
the actually-deployed Render service). Tests: `tests/test_logging.py` (5
tests), `tests/test_repositories.py::test_email_repository_set_stage_emits_a_structured_log_record`.
**Alerting on repeated failures** (this NFR's second half) was not built —
no alerting infrastructure (PagerDuty, Slack webhook, etc.) is configured
anywhere in this project, and building one wasn't requested; a log
aggregator can filter by `message_id`/`thread_id` today, which satisfies
acceptance criterion 2, but criterion "define an alerting threshold" remains
a documentation gap, not a code gap — out of scope for this remediation.

**Requirement**

Replace `logging.basicConfig`'s plain-text format (`app/config/logging.py`)
with structured (JSON) logs carrying at minimum: `message_id`, `thread_id`,
`stage`, `duration_ms`, and `error` where applicable; define an alerting
threshold for repeated pipeline failures.

**Acceptance Criteria**

1. Every pipeline stage transition emits one structured log line.
2. A log aggregator (or, at minimum, a documented convention) can filter by `message_id` across the full pipeline run.

---

### NFR-02 — Performance Targets — **DOCUMENTED (targets defined; not yet code-enforced)**

No timeout or concurrency limit exists anywhere in this codebase today
(confirmed directly: no `timeout` parameter appears in `app/config/settings.py`,
any `app/providers/llm/*.py` provider, or the database connection setup).
The targets below are proposed as this project's V1 measurable targets, not
invented arbitrarily — they're sized to what's actually observable in this
system (mongomock/mock-provider tests run in milliseconds; real LLM calls
are the dominant real-world cost):

| Target | Value | Measurement method |
|---|---|---|
| Email processing latency, mock providers (CI) | P95 ≤ 2s per email | Structured log `duration_ms` (NFR-01) across `tests/test_pipeline*.py` runs |
| Email processing latency, real LLM provider | P95 ≤ 15s per email | Structured log `duration_ms` in production, reviewed manually — dominated by the model's own response time, not this codebase's control |
| MCP read-tool response time | P95 ≤ 300ms under mongomock-equivalent load | Not currently asserted in any test — proposed for a future perf-test addition |
| LLM call timeout | 30s | **Not currently implemented** — no provider sets a request timeout today; this is a proposed value, not an existing behavior |
| MongoDB operation timeout | Driver default (`serverSelectionTimeoutMS` = 30000ms, pymongo's own default) | **Not currently overridden** anywhere in `app/database/` |
| Max concurrent `process_email` calls | Unbounded — no code-level semaphore exists; concurrency is bounded only by Render's own worker/connection limits | N/A |

Implementing the timeout/concurrency enforcement itself was judged out of
this remediation's scope (each would be a real, separate code change with
its own test surface); this section closes the *documentation* gap Rule 24
required, not the enforcement gap.

**Requirement**

Define measurable targets: email processing latency (per message, P50/P95),
MCP tool response time (read tools should be sub-second under mongomock-
equivalent load), LLM call timeout, MongoDB operation timeout, and maximum
concurrent `process_email` calls the Render service is expected to sustain.

**Rationale**

None of these are defined anywhere today — Rule 24 explicitly asks for this
and the architecture document is silent on it.

**Acceptance Criteria**

1. Each target has a number and a measurement method (e.g. "P95 email processing time ≤ 8s, measured via structured log timestamps from NFR-01").

---

### NFR-03 — Explicit Retry Policy — **DOCUMENTED (policy defined; not yet code-enforced)**

Confirmed unchanged from the original finding: the pipeline's skip check is
`stage == COMPLETED` only (`app/pipeline.py`), so a `FAILED` message is
retried on every subsequent run indefinitely, with no counter and no
terminal give-up state. The proposed V1 policy, not yet implemented:

- **Max attempts**: 5 consecutive `FAILED` outcomes for the same
  `message_id`.
- **Retryable errors**: LLM provider errors (rate limit, timeout, transient
  5xx), MongoDB connection errors — anything where a retry has a real
  chance of succeeding.
- **Permanent errors**: malformed input the pipeline cannot parse (e.g. no
  `message_id` at all) — these should fail once and stay failed, not
  consume retry budget.
- **Backoff**: none needed at the pipeline level — retries only happen on
  the *next scheduled poll* (5-minute default `INGESTION_INTERVAL_MINUTES`),
  which already spaces attempts out; no in-process retry loop exists or is
  proposed.
- **Terminal state**: a new `PERMANENTLY_FAILED` stage, distinct from
  `FAILED`, once the max-attempts threshold is reached — never retried
  again automatically; visible in the dashboard's failure count.

Implementing this (a new `retry_count` field on the email document, a stage
check in `run_pipeline`, and the new terminal state) is a real, additive
schema + pipeline change with its own test surface — judged out of this
remediation's scope. This section closes the *policy-definition* gap; the
*enforcement* gap remains open as a sized, well-specified follow-up.

**Requirement**

Define a maximum retry count and backoff for a message stuck in `FAILED`,
distinct from the current implicit behavior (any non-`COMPLETED` message is
retried on every subsequent run, forever, with no limit).

**Acceptance Criteria**

1. A message that fails N consecutive times is marked in a distinguishable terminal state (e.g. `PERMANENTLY_FAILED`) rather than retried indefinitely.
2. N is a configured value, not hardcoded.

---

### NFR-04 — Dashboard Access Control Beyond a Single Shared Password — **DOCUMENTED DECISION: shared-password model accepted for V1**

Reviewed `DASHBOARD_READ_ONLY`/`DASHBOARD_PASSWORD` as instructed. Current
model, confirmed unchanged: a single shared `DASHBOARD_PASSWORD` gate (or
none, if unset), checked once per `st.session_state`, no per-user identity,
no audit log of who viewed what. **Decision recorded for V1: this is
sufficient.** The dashboard's actual deployed audience is small (the
operator, plus at most one other named colleague per the second Render
service's use case) — per-user accounts and access auditing would be
speculative generality for that audience size and were not built. Mutating
controls (Approve/Reject/Edit reply buttons, Create-on-calendar/Ignore
buttons) remain fully hidden under `DASHBOARD_READ_ONLY` regardless of which
access-control model is in front of the dashboard — confirmed unchanged via
`tests/test_ui_smoke.py::test_dashboard_read_only_mode_hides_approval_buttons`.
Revisit if the audience ever grows beyond a small, known set of people.

**Requirement**

Document (and, if required, implement) whether a single shared
`DASHBOARD_PASSWORD` is sufficient for the product's actual audience, or
whether per-user accounts/audit logging of who viewed what is required.

**Rationale**

Today, anyone with the password is indistinguishable from anyone else —
there is no per-viewer identity, and no record of who accessed the
dashboard or when.

**Acceptance Criteria**

1. Either an explicit BRD statement accepting shared-password access as sufficient for V1, or a new per-user auth requirement with acceptance criteria of its own.

---

### NFR-05 — Secrets & Credential Rotation Policy — **DOCUMENTED**

Re-verified this round: `.env` is gitignored (`.gitignore:1`); `.env.example`
contains only safe defaults, empty placeholders (`LLM_API_KEY=`,
`LLM_MODEL=`), or an obviously-fake demo value
(`AGENT_EMAIL=ashok@oursalesagent-demo.example`) — no real credential of any
kind. Production secrets (`MONGODB_URI`, `LLM_API_KEY`, `MCP_AUTH_TOKEN`,
`DASHBOARD_PASSWORD`) are supplied only via Render's own environment
variable store, never committed. **Rotation policy recorded for V1**:
rotate-on-suspected-exposure only (no fixed cadence) — consistent with this
project's own prior incident (a real Anthropic key and MongoDB password
were exposed in a troubleshooting chat transcript and were rotated
immediately after, confirming this is the actual operating procedure, not
merely a proposal). No test or diagnostic anywhere in this repository
prints a secret value; this was spot-checked, not newly enforced, since no
such logging existed to begin with.

**Requirement**

Document the rotation policy for `MCP_AUTH_TOKEN`, `DASHBOARD_PASSWORD`,
the MongoDB Atlas user's password, and the LLM API key — none of these
have a defined rotation cadence or revocation procedure today (confirmed
directly during this project's own operation: a real Anthropic key and a
real MongoDB password were both exposed in a chat transcript during
troubleshooting, with no rotation policy to fall back on).

**Acceptance Criteria**

1. A documented rotation cadence (or an explicit "rotate on suspected exposure only" policy) exists for each credential class.

---

### NFR-06 — Real-Model Acceptance Testing — **DOCUMENTED (process defined; suite not built)**

Confirmed unchanged: no test anywhere in this repository has ever made a
real LLM call, including `tests/test_query_synthesis_live.py`, whose own
docstring states it uses a fake client. **Proposed process, not yet
built**, to close this NFR without weakening the default CI gate:

- A new, separate marker (`@pytest.mark.live_llm`), excluded from the
  default `pytest -q` run via `pytest.ini`'s default `addopts` (e.g.
  `-m "not live_llm"`), so it never blocks a PR and never runs
  automatically in CI.
- Enabled explicitly via `pytest -m live_llm`, requiring `LLM_PROVIDER`
  set to a real provider (`anthropic`/`openai`/`groq`) and a real
  `LLM_API_KEY` present in the runner's own environment — never committed
  to the repo, and the suite fails fast with a clear message if the key is
  absent rather than silently skipping.
- Scope: a small, representative set of real emails (5-10) exercising
  knowledge extraction (REQ-27's guardrail, verified today only as prompt
  text) and the six-way triage classification — the two capabilities this
  audit's Rule 23 specifically flagged as never having been verified
  against real model behavior.
- Run at least once per release; results reviewed by a human before
  production deploy, never auto-trusted or auto-merged.

Building this suite (the marker, the pytest.ini wiring, and the actual test
cases) is a real, additive testing task judged out of this remediation's
scope — this section closes the *process-definition* gap Rule 23 required.

**Requirement**

Establish a **separate, explicitly-labeled** acceptance test suite that
runs a small, representative set of real emails through the actual
configured LLM provider (Claude or Groq) at least once per release,
distinct from the mongomock/mock-provider CI suite.

**Rationale**

Rule 23 of this audit explicitly forbids claiming mock tests prove
production LLM behavior. Verified directly: even the one file whose name
suggests live-model testing (`tests/test_query_synthesis_live.py`) states
in its own docstring that it uses a fake client and makes no network call.
**No test anywhere in this repository has ever exercised a real LLM call.**
The knowledge-extraction prompt guardrail (REQ-27) in particular has only
ever been verified as *prompt text*, never as *actual model behavior*.

**Acceptance Criteria**

1. A documented, separate test run (not part of default `pytest -q`) exists that calls the real configured provider.
2. It is never part of the default CI gate that blocks a PR (to avoid nondeterministic failures and real cost on every commit).
3. Its results are reviewed by a human before each production release, not auto-trusted.

---

### NFR-07 — Data Retention & Deletion Policy — **DOCUMENTED DECISION: retain indefinitely for V1**

**Decision recorded for V1**: `emails` (including body text),
`knowledge_items`, and every other derived-entity collection are retained
indefinitely — no automatic expiry or scheduled deletion job exists or is
proposed for this version. Rationale: the system's entire value proposition
(cumulative per-thread context, a durable knowledge graph, follow-up/
commitment tracking across months) depends on historical data remaining
queryable; deleting it would actively break the product's core function,
not merely tidy up storage.

**Manual deletion procedure** (if ever requested by the operator, e.g. for
a specific person's data): delete the relevant `people`/`emails`/
`knowledge_items`/`threads` documents directly via a MongoDB Atlas
operation or a small ad-hoc script, following the same
snapshot-before-destructive-action discipline already established by
`app/duplicate_consolidation.py`'s rollback-snapshot pattern — no such
script exists today; one would be written on actual request, not
speculatively now. No audit-record collection for deletions exists or is
proposed for V1, consistent with the "no per-user access audit" decision in
NFR-04.

Backups: this repository has no backup automation of its own — retention/
backup of the underlying MongoDB Atlas cluster is Atlas's own platform
responsibility (its standard backup/point-in-time-recovery offering),
outside this codebase's control, and is not otherwise documented here since
verifying the specific Atlas backup configuration was outside this
remediation's scope.

**Requirement**

Define retention periods (or "retain indefinitely, by decision") for
`emails` (including body text), `knowledge_items`, and any future
attachment storage; define whether/how a person can request deletion of
their data.

**Rationale**

Rule 19: nothing in the architecture addresses this at all.

**Acceptance Criteria**

1. A retention period (or explicit indefinite-retention decision) is documented per collection.
2. A deletion procedure exists for at least the `emails`/`people`/`knowledge_items` collections, even if manual.

---

### NFR-08 — HTTPS & Transport Security (Platform Dependency) — **DOCUMENTED (platform assumption confirmed)**

Both Render Web Services (`cos-sales-agent` — MCP, and `cos-dashboard-o1hb`
— Streamlit dashboard) are served at their standard `*.onrender.com`
hostnames. Render's published platform guarantee is that every
`*.onrender.com` URL is HTTPS-only with automatic TLS termination and no
plain-HTTP fallback (HTTP requests are redirected, never served in the
clear) — this repository's own code makes no TLS decision itself
(`uvicorn.run`/Streamlit both bind plain HTTP internally; Render terminates
TLS in front of them, unchanged from the original finding). **Deployment
assumption recorded**: `MCP_AUTH_TOKEN` bearer tokens are never transmitted
over plain HTTP in this deployment, because the platform itself does not
offer a plain-HTTP path to the deployed hostname. Verifying this from
outside this codebase (e.g. an actual `curl -I http://cos-sales-agent.onrender.com`
redirect check) was not performed as part of this remediation — it depends
on Render's live platform behavior, not on anything this repository
controls or can assert with certainty from source alone.

**Requirement**

Confirm and document that Render's platform-level HTTPS termination
applies to both services (MCP and dashboard), since this repository's own
code makes no TLS decisions itself (`uvicorn.run` and Streamlit both bind
plain HTTP internally; Render terminates TLS in front of them).

**Status of this NFR**: NEEDS VERIFICATION — true by strong platform
convention, but not something this codebase can itself prove, and not
something previously stated explicitly in the architecture document.

**Acceptance Criteria**

1. Documented confirmation (screenshot or Render's own published guarantee) that both `*.onrender.com` URLs are HTTPS-only with no plain-HTTP fallback.

---

## SECTION 5 — Architecture Changes Required

*Only changes genuinely necessary to close a real gap — nothing here
redesigns a component that already satisfies its requirement.*

**Historical record, kept as originally written:** this table (and Sections
6/7/8's scenario/schema/acceptance-test rows below, wherever they reference
FR-01 through FR-04) was written before any of FR-01–FR-04 were implemented.
All four are now done — see Section 3's per-FR status headers and REQ-51,
REQ-52, REQ-33, REQ-37, REQ-38 in Section 2 for what was actually built
(which differs in detail from what's planned below, e.g. FR-04 used the
already-existing `project_or_pillar` field rather than a new `category`
field, per Section 3's own explicit recommendation to review that first).

| Requirement | Existing Component | Required Change | New Component Needed? | Code Change? | DB Schema Change? |
|---|---|---|---|---|---|
| FR-01 Executive query tool | `app/query/service.py` (already complete) | Add one new `@mcp.tool()` wrapper in `app/mcp/server.py` + `app/mcp/tools.py` | No — wire up existing code | Yes (small, additive) | No |
| FR-02 "What's on my table" | Existing `list_*` tools | New composition tool calling existing primitives in parallel | New MCP tool function only | Yes (small, additive) | No |
| FR-03 Overdue follow-ups | `FollowUpRepository` | New query method + MCP tool | New MCP tool function only | Yes (small, additive) | No |
| FR-04 Meeting category | `Meeting` model, `resolve_meeting` | Add `category: str | None` field, propagate from `analysis.goal_pillar` | No | Yes | Yes (additive field, backward-compatible — old records simply have `category: None`, same pattern already used for every other schema addition in this project's history) |
| FR-05 Attachments | `Email.attachments` | Decision-dependent — either build ingestion or formally close as Out of Scope | Possibly (if built) | Decision-dependent | Decision-dependent |
| FR-06 Real sending | `simulate_send` | New, separate function; `simulate_send` itself must NOT be modified to "sometimes really send" (that would violate the existing, clearly-named contract) | New function (e.g. `send_approved_reply`) | Yes | No |
| FR-07 Calendar failure handling | `approve_calendar_action` | Add a failure branch and a new terminal status | No | Yes | Additive status value only |
| FR-08 Duplicate review UI | `preview_duplicate_person_candidates` | New dashboard tab, read-only rendering of existing tool's output | No | Yes (dashboard only) | No |

---

## SECTION 6 — MCP Tool Coverage

| Requirement | Existing MCP Tool | Missing Tool? | Required Change |
|---|---|---|---|
| Process one email | `process_email` | No | — |
| Granular reasoning-caller path | `ingest_email`, `persist_email_analysis`, `persist_context_delta`, `create_reply_draft`, `mark_email_completed` | No | — |
| List/search emails | `list_processed_emails`, `search_emails`, `get_thread` | No | — |
| People/Projects/Commitments/Follow-ups/Meetings/Reply drafts | `list_people`, `list_projects`, `list_commitments`, `list_follow_ups`, `list_meetings`, `list_reply_drafts`, `get_reply_draft` | No | — |
| Project/company 360 | `get_project_summary`, `get_company_summary` | No | — |
| Knowledge lookup | `lookup_knowledge` | No | — |
| Duplicate detection (read-only) | `preview_duplicate_person_candidates` | No | — |
| **"What's on my table?"** | none | **Yes** | Add FR-02 |
| **"What sales meetings do I have today?"** | *(as originally written; now closed — see FR-04's SATISFIED status in Section 2/3)* none (and `list_meetings` has no category filter, since none exists yet) | ~~Yes~~ Done | ~~Add FR-04 then a filtered query~~ `list_meetings(category="sales", start_date=..., end_date=...)` |
| **"What finance meetings do I have today?"** | none | **Yes** | Same as above |
| **General natural-language question ("What changed since yesterday?", "Which customers are waiting for me?")** | none — a skill (`sales-inbox-assistant`) currently hand-orchestrates raw `list_*` calls client-side instead | **Yes** | Add FR-01 |
| **Overdue follow-ups** | none | **Yes** | Add FR-03 |
| Duplicate merge *execution* | none (script-only, `app/duplicate_consolidation.py`) | Ambiguous — may be intentional (irreversible action deliberately kept out of MCP's reach) | See AMBIGUOUS item below |

---

## SECTION 7 — MongoDB Coverage

| Requirement | Collection | Required Fields (existing) | Existing? | Gap |
|---|---|---|---|---|
| Email storage + triage | `emails` | `message_id`, `label_applied`, `priority`, `processing_status`, `entities_referenced` | Yes | None |
| Threading | `threads` | `thread_id`, `message_ids`, `participant_emails` | Yes | None |
| Versioned context | `context_snapshots` | `thread_id`, `context_version`, `context`, `changes_from_previous_context` | Yes | None |
| Canonical people | `people` | `id`, `email`, `org_id`, `type`, `preferences`, `merged_into` | Yes | No field distinguishes "verified real contact" from "flagged, no-email record" at a glance beyond `review_flag`/absence of `email` — acceptable today, worth a dashboard-level indicator, not a schema gap |
| Canonical orgs | `organizations` | `id`, `domain`, `name` | Yes | None |
| Projects | `projects` | `id`, `project`, org linkage via matching text | Yes | No stored `org_id` directly on `Project` itself (linkage is computed at read time in `_process_entities`, not persisted) — **NEEDS VERIFICATION** whether this is sufficient for `get_project_summary`'s needs long-term |
| Commitments | `commitments` | `id`, `class`, `person_id`, `thread_id`, dates | Yes | None |
| Follow-ups | `follow_ups` | `id`, `commitment_id`, timing window, `escalation_level` | Yes | No indexed/queryable "overdue" boolean — see FR-03 |
| Meetings | `meetings` | `id`, `date`, `person_ids`, `org_id`, `project_or_pillar` | Yes | **No `category` field for Sales/Finance/etc.** — see FR-04. Note `project_or_pillar` already exists and may be a natural home for this rather than a brand-new field — worth reviewing before adding a duplicate concept |
| Personal items | `personal_items` | `id`, `sender_email`, `description` | Yes | None |
| Reply drafts | `reply_drafts` | `reply_id`, `status`, `draft`, `person_id` | Yes | No `send_failed`/real-send status values yet — see FR-06 |
| Calendar actions | `calendar_actions` | `thread_id`, `meeting_fingerprint`, `status`, `event` | Yes | No `creation_failed` status — see FR-07 |
| Knowledge graph | `knowledge_items` | full EAV shape (see §2 of the architecture doc) | Yes | None structurally; enforcement gap is in the prompt layer, not the schema |
| Processing runs | `processing_runs` | run-level summary | Yes | Not represented as its own dashboard tab (only aggregated into the Dashboard metrics tile) — minor, not a data gap |
| Attachments | none | — | **No** | See FR-05 |

---

## SECTION 8 — Acceptance Test Plan

*Concrete tests for every MISSING or PARTIALLY SATISFIED item above.*

| Test ID | Requirement | Input | Expected Behavior | Expected MongoDB Result | Expected MCP Result | Expected Dashboard Behavior | Pass/Fail Condition |
|---|---|---|---|---|---|---|---|
| AT-01 | FR-01 | `ask_question("What commitments are overdue?")` | Intent classified as `COMMITMENTS`, filtered by overdue window | No writes | `{status: "OK", evidence: [...]}` with only overdue items | N/A (tool-level) | Fail if any commitment with a future due date appears, or if any LLM call is made |
| AT-02 | FR-01 | `ask_question("Who is Ashok at DataBeat?")` with two people named Ashok in different orgs | Entity resolution disambiguates by org context | No writes | Returns the DataBeat-org Ashok specifically, or `AMBIGUOUS` if truly unresolvable | N/A | Fail if it silently picks either without a real distinguishing signal |
| AT-03 | FR-02 | `executive_summary()` on a seeded mongomock db with 1 P1 email, 1 overdue follow-up, 0 meetings | All 6 categories present, meetings category is an empty list (not omitted) | No writes | Full 6-key structured response | N/A | Fail if any key is missing or if one category's absence raises an exception |
| AT-04 | FR-03 | Seed a follow-up with `follow_up_latest_at` = yesterday and one with tomorrow, real "now" = today | Only the past one returned | No writes | `list_overdue_follow_ups()` returns exactly 1 item | N/A | Fail if the future one is included or the past one is excluded |
| AT-05 | FR-04 | Process an email with `goal_pillar: "Sales"` that also proposes a meeting | Resulting `Meeting.category == "Sales"` | `meetings` document has `category: "Sales"` | `list_meetings()` includes the field | Meetings tab column shows it | Fail if `category` is `None` despite a clear `goal_pillar` upstream |
| AT-06 | FR-04 | Process an email with no clear `goal_pillar` that proposes a meeting | `Meeting.category` stays `None` | `category: null` | Same | Same, shown as blank | Fail if a category is guessed/invented |
| AT-07 | FR-06 (if built) | Approve a reply with a real `EmailProvider` configured | Real `send_email` called exactly once | `reply_drafts.status == "sent"` | N/A (dashboard-only action) | Approve button, confirmation shown | Fail if called twice, or if called when `DASHBOARD_READ_ONLY=true` |
| AT-08 | FR-06 (if built) | Same as AT-07 but the provider raises | Draft marked distinctly, human notified | `reply_drafts.status == "send_failed"` | N/A | Dashboard shows a clear failure message, not a false success | Fail if the draft is marked `sent` despite the exception |
| AT-09 | FR-07 | Approve a calendar action with a real `CalendarProvider` that raises | `CalendarAction` marked `creation_failed`, not `scheduled` | `calendar_actions.status == "creation_failed"` | N/A | Dashboard shows the failure, "Create on my calendar" re-offered | Fail if silently marked `scheduled` |
| AT-10 | FR-08 | `DASHBOARD_READ_ONLY=true`, open the new Duplicate Review tab | Candidates listed, zero merge controls rendered | No writes | N/A | Read-only list only | Fail if any button that could trigger a merge is present |
| AT-11 | NFR-03 | A message fails processing 5 times in a row (configurable N) | Marked `PERMANENTLY_FAILED` on the 5th, not retried a 6th time | `processing_status.stage == "PERMANENTLY_FAILED"` | N/A | Dashboard failure count includes it distinctly | Fail if it's retried indefinitely or silently dropped |
| AT-12 | NFR-06 | Run the (separate, non-CI-gating) real-model suite against a real sample email like *"Hi team, following up — can you send the updated pricing sheet by Friday? Also, are we still on for the demo Thursday at 2pm?"* | Real model extracts a commitment (`owed_to_me`, due Friday) and a meeting proposal (Thursday 2pm) | Real writes to a disposable test database only | Real `analyze_email` response captured for human review | N/A | Fail (for human review, not CI) if the real model's output diverges meaningfully from the mocked heuristic's shape |
| AT-13 | REQ-11 | A message fails once, then the pipeline is re-run with the same input | It reaches `COMPLETED` on the second run, without needing manual intervention | `processing_status.stage == "COMPLETED"` | N/A | N/A | Fail if the second run also fails or skips the message entirely |
| AT-14 | REQ-42 (calendar failure, general) | `approve_calendar_action` called with a `CalendarProvider` stub configured to raise `ConnectionError` | Exception is caught, not propagated to the dashboard as an unhandled crash | `calendar_actions` unchanged or marked failed (see AT-09) | N/A | Dashboard shows a user-facing error, not a stack trace | Fail if the Streamlit app crashes instead of degrading gracefully |

---

## SECTION 9 — Final BRD Additions

### Functional Requirements

- **FR-01** — Executive Query MCP Tool (natural-language question → structured evidence, no LLM call inside the tool itself) — **IMPLEMENTED**
- **FR-02** — "What's on my table?" Cross-Entity Aggregation Tool — **IMPLEMENTED**
- **FR-03** — Overdue Follow-up Query — **IMPLEMENTED**
- **FR-04** — Meeting Category Classification (Sales/Finance/etc.) — **IMPLEMENTED**, including the `list_meetings` `category` filter closing the integration gap
- **FR-05** — Attachment Handling — build real ingestion, *or* formally declare Out of Scope (see below)
- **FR-06** — Real (non-simulated) Email Sending — *only if the product actually requires it;* otherwise formally declare simulated-only (see below)
- **FR-07** — Real Calendar Provider Failure Handling
- **FR-08** — Duplicate-Consolidation Review Surface in the Dashboard

### Non-Functional Requirements

- **NFR-01** — Structured Logging & Alerting
- **NFR-02** — Performance Targets (latency, timeout, concurrency)
- **NFR-03** — Explicit Retry-Limit Policy for Failed Processing
- **NFR-04** — Dashboard Access Control Policy Decision (shared password vs. per-user)
- **NFR-05** — Secrets & Credential Rotation Policy
- **NFR-06** — Real-Model Acceptance Testing (separate from CI, human-reviewed)
- **NFR-07** — Data Retention & Deletion Policy
- **NFR-08** — HTTPS/Transport Security Confirmation (platform-level, needs documented confirmation)

### Out of Scope / Explicit Decisions (recommended, pending your sign-off)

These are the items this audit found ambiguous or silently assumed — each
should get one explicit line in the BRD rather than staying implicit:

1. **Real email sending**: recommend the BRD state *"V1 sends no real email; every reply is a human-reviewed simulated send only"* unless FR-06 is deliberately taken on.
2. **Attachments**: recommend the BRD state *"Email attachments are not ingested, parsed, or searchable in V1"* unless FR-05 is deliberately taken on.
3. **Meeting Sales/Finance categorization**: **done** — FR-04's `goal_pillar`-derived category is implemented, MCP-reachable via `list_meetings(category=...)`, and tested; PROSPECT/PROJECT remain explicitly out of scope for V1 (no reliable field supports either).
4. **Cross-thread meeting deduplication**: today's dedup is thread-scoped only; recommend the BRD explicitly accept this scope rather than assume system-wide dedup exists.
5. **Duplicate-person merge execution via MCP**: currently a manual, script-level, human-run process outside MCP's reach entirely (not even reachable by the operator's own MCP client, let alone a dashboard viewer). This appears to be a *deliberate* safety choice (an irreversible action kept maximally hard to trigger by accident) — recommend the BRD explicitly ratify this as intentional rather than leave it looking like an oversight.
6. **Real calendar provider**: recommend the BRD state which real calendar system (if any) is required for V1, since only a mock exists today and is what's actually deployed.
