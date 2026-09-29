# CoS Sales Agent — Canonical Identity, Person Context & Thread Events Architecture

This document covers everything built in this development phase, on top of the
baseline system described in `docs/ARCHITECTURE.md`. It does not repeat that
document's content (deployment model, base collections, dashboard tabs, etc.) —
it covers only what's new: the canonical ID model, the three-layer person-memory
system, and the Thread Events audit trail.

Status at the end of this phase: **1369 tests passing**, all work committed to
branch `worktree-phase-2-implementation` at commit `a8ada35`.

---

## 1. Why this phase exists

Two gaps were identified against the original system:

1. **Person records were purely transactional.** A `Person` document had a name,
   email, org, and little else — nothing accumulated across emails beyond a
   handful of scalar fields. Every subsequent email should enrich what the
   system knows about a person, and that accumulated knowledge should reach the
   LLM before it reasons about that person's next email.
2. **A thread had no audit trail.** There was no way to answer "what did the
   system actually do while processing this thread" — which entities were
   created vs. reused, what knowledge changed, what commitments/meetings
   resulted — beyond scattered fields on individual documents.

Both gaps required a stable way to refer to emails and threads first, which is
why the canonical ID refactor came first.

---

## 2. Canonical Identity Model

### The problem

Before this phase, `emails.message_id` and `threads.thread_id` held whatever
the source system (Gmail) supplied — long opaque strings, not stable across
providers, not human-readable, and not safe to use as a foreign key once
multiple ingestion paths (live Gmail, file replay, raw JSON import) could all
feed the same collections.

### The design

- **Canonical IDs**: `emails.message_id` / `emails.id` = `EML-nnn`;
  `threads.thread_id` / `threads.id` = `THR-nnn`. Both fields are mirrors of
  each other (kept for backward compatibility with code that reads either
  name). Generated via the existing atomic counter (`app.entities.ids.next_id`,
  backed by `CounterRepository.increment_and_get`, itself backed by MongoDB's
  atomic `find_one_and_update`) — the same mechanism already used for
  `PER-`/`PRJ-`/`CMT-`/etc.
- **Source identifiers**: `emails.source_message_id` / `threads.source_thread_id`
  hold the true, permanent, never-reassigned Gmail/provider identifiers. These
  are the *only* place a raw Gmail id is allowed to live as a domain identifier.
- **One translation boundary**: `app.pipeline.ingest_raw_email` is the only
  place `message_id` is reassigned from raw to canonical — every downstream
  function keeps reading `email.message_id` by the same name; only the value
  changes at that one point. `resolve_and_persist_thread` does the equivalent
  for `thread_id`.
- **Dedup key**: `source_message_id` (permanent) — not `message_id` (now
  canonical, and therefore not a stable identity for a retried raw message).
- **Threading signals** (`in_reply_to`, `references`, provider thread id) are
  matched against `source_message_id`/`source_thread_id` — never against
  canonical ids, which a raw Gmail header could never contain anyway.
- **Transitional shim**: `Email.model_validator` auto-fills `source_message_id`
  from `message_id` (and `source_thread_id` from `thread_id`) when a caller
  only supplies the old-style keys — this is what let ~70 pre-existing test
  fixtures keep working unmodified.

### Where this lives

| Concern | File |
|---|---|
| `Email` model + shim | `app/email/models.py` |
| Thread candidate matching | `app/email/threading.py` |
| Translation boundary, dedup, thread upsert | `app/pipeline.py` (`ingest_raw_email`, `resolve_and_persist_thread`, `_upsert_thread`) |
| Raw-file ingestion path | `app/raw_ingestion.py` |
| Indexes (`source_message_id`/`source_thread_id`, sparse) | `app/database/indexes.py` |

### Known limitation

**No historical backfill has been run.** Documents already in production from
before this phase were written under the pre-refactor semantics. This phase
deliberately never touched production MongoDB — see §7 for exact current
runtime state.

---

## 3. Three-Layer Person Memory

The goal: build a rich, durable profile of a person across emails, not just a
name/email/org record, and get that profile in front of the LLM before it
reasons about that person's next email.

### Layer 1 — Standing Person (pre-existing, unchanged)

`app/entities/models.py:Person` — the durable identity record: `id`, `name`,
`email`, `aliases`, `org`, `org_id`, `type`, `goal_pillar`,
`last_inbound`/`last_outbound`, `open_threads`, `status`/`merged_into`.
Resolved via `app/entities/resolution.py:resolve_person` (email match →
email-anchored name match → thread-scoped name match → new record; "never
merge on name alone" globally).

### Layer 2 — Person Context Snapshots (new)

`app/entities/person_context.py`. One `PersonContextSnapshot` is created per
`(person, email)` pair, every time that person is referenced in a processed
email — an **incremental** record of what *that specific email's processing*
established, not a copy of everything ever known.

```python
class PersonContextEntry(BaseModel):
    category: PersonContextCategory  # see §4 for the full list
    record_id: str | None            # canonical id of the referenced record, if any
    summary: str
    provenance: Literal["observed", "reconstructed", "inferred"]
    thread_id: str                   # always THR-nnn
    email_id: str                    # always EML-nnn
    confidence: float | None

class PersonContextSnapshot(BaseModel):
    id: str                          # PCS-nnn
    person_id: str
    thread_id: str
    source_email_id: str
    created_at: datetime
    entries: list[PersonContextEntry]
```

A snapshot's entries are built from what `app.pipeline._process_entities`
actually resolved for that email: the email interaction itself, the person's
organization (if resolved), any project/opportunity/commitment/meeting/
follow-up/personal-item this person was genuinely linked to *from this
email*, and — new in this phase — explicit semantic knowledge (§4).

**Idempotency**: keyed on `(person_id, source_email_id)`, unique index, upsert
— a retried email overwrites the same snapshot rather than duplicating it.

**Provenance vocabulary** (`observed` / `reconstructed` / `inferred`) is a
dedicated, separate axis from `KnowledgeItem.basis` (`stated`/`inferred`) —
the two are never conflated. `observed` = a structural fact this email's
processing directly produced; `inferred` = an LLM interpretation; `reconstructed`
= only ever applied by the bounded-retrieval function (§5), to mark an entry
pulled forward from an earlier snapshot.

### Layer 3 — Person-Attributed Semantic Knowledge (new)

This is the fix for "Person Context was purely transactional." Before this
addition, the only things that ever accumulated on a person were structural
(commitments, meetings, projects) — qualitative facts (role, preference,
concern, pain point...) had nowhere to attach, because the existing
`requirements`/`pain_points`/`objections`/`buying_signals`/`competitors`
extraction fields were *always* stored as thread-scoped `KnowledgeItem`s
(subject = the thread id itself), never person-scoped.

**New schema**: `app/analysis/schemas.py:PersonFactMention`

```python
class PersonFactMention(BaseModel):
    person_name: str
    person_email: str | None
    category: Literal[
        "role", "responsibility", "preference", "goal", "interest",
        "concern", "pain_point", "objection", "buying_signal", "other",
    ]
    value: str
    basis: Literal["stated", "inferred"] = "stated"
```

`EmailAnalysis.person_facts_mentioned: list[PersonFactMention]` is the new
field the LLM populates. The production prompt
(`app/providers/llm/claude.py:_ANALYSIS_INSTRUCTIONS`) instructs the model to
populate this **only when the email gives real evidence the statement is about
that specific person** — never merely because they're mentioned, and never for
a statement that's actually about "the team"/"procurement"/the organization as
a whole. An ambiguous statement is left out of `person_facts_mentioned`
entirely and simply stays in the pre-existing thread-scoped fields.

**Attribution, at the pipeline level** — `app.pipeline._resolve_person_fact_target`:
resolves `person_email` (exact match, restricted to people *already resolved for
this same email*) then falls back to name-token matching
(`match_resolved_person_by_name`, an existing, reused helper) — never a fresh
scan of the whole `people` collection, and never a guess: an ambiguous or
unresolved mention is silently dropped rather than attached to the wrong
person.

**Storage — reuses `KnowledgeItem`, never a parallel system.**
`app.pipeline._process_person_facts` calls the existing
`app.knowledge.deduplication.process_new_fact` (now accepting optional
`person_id`/`org_id`) with `subject = target["id"]` (the canonical `PER-nnn`
id) instead of a free-text name — meaning `subject_key` is deterministic and
explicit, never derived from post-hoc name matching. This gives every
person-attributed fact the exact same conflict/history mechanism every other
`KnowledgeItem` already has (`current_value`, `history`, `source_emails`,
`confidence`, fuzzy-match-based "is this the same fact restated" detection) —
nothing new was built for conflict handling.

**Surfacing in Person Context**: `PersonContextEntry.category` now includes
the same 10 semantic values as `PersonFactMention.category`. When a
`KnowledgeItem`'s `predicate` is one of them, it's surfaced under that real
label (`category="role"`) instead of the generic `"knowledge"` bucket. The
*old* mechanism that unconditionally copied `analysis.buying_signals`/
`pain_points`/`objections` onto the sender — regardless of whether the email
actually said anything about them specifically — was **removed** in this phase;
that was exactly the "automatic sender attribution" this design explicitly
avoids.

---

## 4. Bounded Person Context for the LLM

`app.entities.person_context.get_bounded_person_context_for_llm(db, person_id)`
— called from `app.pipeline.run_pipeline`, right before
`analyze_email_with_validation`, using a **read-only** lookup
(`resolve_canonical_person_for_email`) of any Person already known for the
sender (never creates one — that stays `_process_entities`'s job, later in the
same run).

Returned shape:

```python
{
    "person_id": ..., "name": ..., "org": ..., "org_id": ..., "goal_pillar": ...,
    "current_context": [...],      # entries from the single most recent snapshot
    "historical_context": [...],   # entries from up to 3 earlier snapshots, capped at 10,
                                    # each re-tagged provenance="reconstructed"
    "knowledge": [...],             # up to 8 most-recently-confirmed KnowledgeItems
                                     # for this person_id — NOT bounded by snapshot
                                     # recency, so durable facts (role, preference)
                                     # don't disappear just because it's been a while
}
```

This is passed to `LLMProvider.analyze_email(..., person_context=...)` — a new
parameter on the interface, alongside the existing `thread_history`. Real
providers (`ClaudeProvider`/`OpenAIProvider`) render it into the prompt via
`app.providers.llm.thread_history.format_person_context`; `MockLLMProvider`
accepts and ignores it (same convention as `thread_history`).

**Selection is deterministic and recency-based** — not relevance-ranked. This
is a known, explicit limitation (see §8).

---

## 5. Thread Events

`app/entities/thread_events.py` — an append-only, descriptive audit trail of
what the pipeline actually did while processing each email. **It never
performs entity mutation itself** — the flow is always:

```
existing operation → existing repository/service performs it → record_event describes what happened
```

never the reverse (no event processor, no replay-to-mutate, not a second
pipeline).

### Schema

```python
class ThreadEvent(BaseModel):
    id: str                 # TEV-nnn
    thread_id: str           # THR-nnn
    email_id: str | None     # EML-nnn
    sequence: int            # monotonic, per-thread
    event_type: EventType
    entity_type: EntityType
    entity_id: str | None    # canonical id of the referenced record
    operation: Operation
    summary: str
    provenance: Literal["observed", "reconstructed", "inferred"]  # always "observed" here
    created_at: datetime
    metadata: dict[str, Any]
```

`EventType`: `email_received`, `entity_created`/`entity_reused`/`entity_updated`/
`entity_linked`, `context_enriched`, `knowledge_created`/`knowledge_updated`,
`commitment_created`/`commitment_reused`, `meeting_created`/`meeting_reused`,
`follow_up_created`, `project_updated`, `opportunity_created`/`opportunity_reused`,
`person_context_enriched`, `processing_failed`.

### Idempotency & sequencing

- **Idempotency key**: `(thread_id, email_id, event_type, entity_type, entity_id)`,
  unique index. A retry that re-observes the same logical operation upserts
  over the same document (keeping its original `id`/`sequence`/`created_at`);
  a genuinely later email always produces a new event.
- **Sequence**: a per-thread atomic counter, reusing the exact same
  `CounterRepository.increment_and_get` primitive as `EML-`/`THR-`/etc.
  (`f"TEV_SEQ:{thread_id}"` as the counter key) — no new counter mechanism.
- **Failure isolation**: every call site uses `try_record_event`, which
  swallows any exception — Thread Event persistence can never break core email
  processing. Verified directly (not just by code inspection): a monkeypatched
  `record_event` that always raises still lets a real `run_pipeline` call
  reach `COMPLETED`.

### Instrumentation points

Every resolver in `app/entities/resolution.py` (`resolve_person`,
`resolve_operator_person`, `resolve_project`, `resolve_commitment`,
`resolve_meeting`, `resolve_opportunity`, `resolve_personal_item`,
`derive_follow_up`) gained a `_with_operation` sibling —
`resolve_X_with_operation(...) -> (id, operation, delta)` — alongside its
original, byte-for-byte-unchanged plain function. `operation` is the resolver's
own real created/reused/updated decision (never inferred from diffing DB
state); `delta` is `{field: {"old", "new"}}` for exactly what changed in that
call, or `{}` if nothing did (see §6). `app.pipeline._process_entities` calls
the `_with_operation` variants and records the corresponding event immediately
after each resolution.

### `context_enriched`

At the existing `CONTEXT_BUILT` pipeline stage, the real, already-computed
`list[ContextChange]` that `app.context.engine.build_next_context` returns is
used directly — never a fabricated or reconstructed diff. One `context_enriched`
event is recorded per email, only when that list is non-empty (skipped
entirely for a thread's first email, or an email that added nothing new),
with `metadata={"changes": [...]}` holding the real `ContextChange` objects
(field/type/detail/source_email_id) — never the full `ThreadContext`.

### Retrieval & exposure

`get_thread_event_trail(db, thread_id)` — a flat list, ordered by `sequence`
ASC. Exposed through the existing, already-live `get_thread` MCP tool as a new
`event_trail` key (no new MCP tool was added). **This is a flat,
chronologically-ordered list, not a literal tree** — a caller can present it
hierarchically by grouping on `email_id` (which works today because insertion
order is linear per email), but there is no parent/child field in the schema.

---

## 6. Entity Update Deltas

Every resolver already built its own `update: dict` internally (the set of
fields it determined needed backfilling on an existing record) before this
phase — this phase just captures that same, already-known information rather
than re-deriving it:

```python
def _field_delta(old_doc, update):
    return {field: {"old": old_doc.get(field), "new": new_value} for field, new_value in update.items()}
```

- **Person**: reports whichever of `last_inbound`/`last_outbound`/
  `open_threads`/`org_id`/`org`/`aliases` actually changed.
- **Project**: `person_ids`/`org_id`.
- **Commitment**: `person_id`/`org_id`/`project_id` (backfill only — the
  named vocabulary has no `commitment_updated`, so this still reports as
  `commitment_reused` with a delta attached).
- **Meeting**: `person_ids`/`org_id`/`project_or_pillar`.
- **Opportunity**: `person_ids`/`org_id`/`source_email_ids`/`meeting_ids`/
  `buying_signals` — deliberately **excluding** `last_activity_at`/`updated_at`,
  which tick on every reuse and would otherwise make every event look like it
  changed something.
- **Follow-up, Personal item**: never mutate an existing record on reuse, so
  the delta is always `{}`.

**No fabrication**: when a resolver's own `update` dict is empty, the event's
`metadata` has no `delta` key at all — never an empty placeholder.

---

## 7. Current Runtime State (as of the end of this phase)

This section reflects what was actually verified live, not just what's in the
code:

- **Code**: committed at `a8ada35` on branch `worktree-phase-2-implementation`,
  pushed to `origin`. Working tree clean.
- **Tests**: 1369 passing, 0 failing, entirely against `mongomock` — no
  production database was ever touched by this phase's development or testing.
- **MCP server**: `claude_desktop_config.json` registers `"cos-sales-agent"` →
  `python -m app.mcp.server` from this worktree. The 8 stale server processes
  that had been running since before this phase's commit were terminated; a
  fresh process will start on the next tool call from Claude Desktop and will
  load the current, committed code.
- **Dashboard**: `app/ui/dashboard.py` was **not modified** by this phase — it
  has no view for `thread_events` or `person_context_snapshots`. It shares the
  same MongoDB connection as the server, so once real emails flow through the
  reloaded pipeline, its existing tabs (emails, threads, commitments, etc.)
  will show canonical `EML-`/`THR-` ids — but nothing new is visualized for
  Person Context or Thread Events without further UI work.
- **Production MongoDB**: **no historical backfill/migration has been run.**
  Documents already in the database predate this phase's schema. A canonical-
  ID + Person-Context + Thread-Events backfill for historical data was
  requested but explicitly paused pending a design decision (see §8) —
  nothing was written to production as part of that discussion.

---

## 8. Known Limitations / Explicitly Deferred Work

**P1 (deferred, not implemented):**
- Relevance-based ranking for `get_bounded_person_context_for_llm` — selection
  is recency-only today; an important old fact can drop out of the bounded
  view purely because it's aged out, even if it's still open/active.
- A read-only MCP tool exposing `PersonContextSnapshot` directly (today it's
  pipeline-internal only).
- Surfacing `KnowledgeItem.history` (only `current_value` is exposed anywhere
  today — a changed fact's prior value is invisible outside a direct DB read).

**P2 (deferred, not implemented):**
- A literal parent/child or `caused_by` model for Thread Events (today: flat,
  sequence-ordered, groupable by `email_id`).
- `ThreadContext` fed directly into `analyze_email` (today it only reaches
  reply drafting and the dashboard/`get_thread`, never the classification
  prompt itself).
- Broader event-taxonomy expansion beyond what's listed in §5.
- **Historical backfill/reprocessing** — explicitly requested mid-phase, then
  paused: Thread Events cannot honestly represent a historical
  created-vs-reused decision that was never recorded at the time, so this
  needs an explicit decision (e.g., a distinct `operation="backfilled"`
  marker) before any migration code is written, and any production run must
  be dry-run-first, never a single blind pass.

---

## 9. File Map

| Area | Files |
|---|---|
| Canonical ID model | `app/email/models.py`, `app/email/threading.py`, `app/pipeline.py`, `app/raw_ingestion.py`, `app/database/indexes.py` |
| Person Context | `app/entities/person_context.py` |
| Person-attributed knowledge | `app/analysis/schemas.py` (`PersonFactMention`), `app/pipeline.py` (`_process_person_facts`, `_resolve_person_fact_target`), `app/knowledge/deduplication.py` (`process_new_fact` person_id/org_id params), `app/providers/llm/claude.py` (prompt) |
| Thread Events | `app/entities/thread_events.py`, instrumentation in `app/pipeline.py` and `app/entities/resolution.py` (`_with_operation` variants) |
| Entity deltas | `app/entities/resolution.py` (`_field_delta`), `app/pipeline.py` (`_delta_metadata`) |
| Query/Evidence corrections | `app/query/schemas.py`, `app/query/evidence.py`, `app/query/retrieval.py` |
| MCP exposure | `app/mcp/tools.py` (`get_thread` → `event_trail`) |
| Tests | `tests/test_person_context.py`, `tests/test_thread_events.py`, `tests/test_person_knowledge.py`, `tests/test_query_evidence_canonical_ids.py`, `tests/test_mcp_ask_question_canonical_ids.py`, plus updates across ~25 existing test files |
