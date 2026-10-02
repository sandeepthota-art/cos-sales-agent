# CoS Sales Agent — MongoDB Data Dictionary

This document describes the current implementation only. It was produced by reading
the application code directly (models, repositories, pipeline, entity-resolution,
MCP tools, migration scripts) — no code, schema, or MongoDB data was changed to
produce it.

**Files inspected:** `app/database/repositories.py`, `app/database/indexes.py`,
`app/database/mongodb.py`, `app/entities/models.py`, `app/entities/resolution.py`,
`app/entities/ids.py`, `app/email/models.py`, `app/email/threading.py`,
`app/context/models.py`, `app/context/engine.py`, `app/replies/models.py`,
`app/replies/approval.py`, `app/calendar/models.py`, `app/calendar/actions.py`,
`app/knowledge/models.py`, `app/knowledge/deduplication.py`, `app/analysis/schemas.py`,
`app/processing/models.py`, `app/pipeline.py`, `app/mcp/tools.py`,
`app/entity_migration.py`, `app/duplicate_consolidation.py`,
`app/providers/source/folder.py`, `app/reminders.py`.

## How to read this document

This dictionary describes the **current implementation as it exists in the code
today** — not a proposed, planned, or future design. Every field below is one of:

- **Actively populated** — a live code path writes real, meaningful values to it (the
  field's row says what writes it and when).
- **Persisted but currently at its default** — the field is declared on a Pydantic
  model, gets written to MongoDB on every document (as `null`, `[]`, `false`, or
  whatever the model specifies as a default), but **no code path in this repository
  has ever been observed to set it to anything else**. These are real, persisted
  fields, not gaps in this document — they are marked **"Not observed to be set by
  any current code path"** or **"(declared default only)"**. This document keeps them
  rather than omitting them, because they exist in every document in the collection
  today, carrying no information but still present.

Each collection section also states, where relevant:
- **Backend-only** — persisted and used by pipeline/entity-resolution logic, but never
  displayed anywhere in the Streamlit dashboard (`app/ui/dashboard.py`).
- **Dashboard-visible** — actually rendered on a dashboard tab today.

Two collections (`entities`, `activities`) are declared in the repository layer
(`app/database/repositories.py`) but have **no code path anywhere in this repository
that reads from or writes to them**. They are documented separately, as
declared-but-unused, rather than given a field table.

## Conventions used below

- **"Populated By"** uses these labels: **Gmail/source ingestion** (raw email fields,
  written once on first ingest), **Claude Desktop analysis / internal LLM call**
  (whichever process produced the `EmailAnalysis`/`ContextDelta` — the internal LLM
  call inside `process_email`, or a human/Claude-Desktop-driven call to the granular
  MCP tools; both use the identical schema and downstream code), **deterministic
  pipeline** (plain Python logic, no LLM, no human judgment), **entity resolution**
  (`app/entities/resolution.py` — dedup/creation of canonical Person/Organization/
  Project/Opportunity/Commitment/Meeting/PersonalItem records), **human/MCP update**
  (a value settable only through an explicit MCP tool call, never inferred), and
  **system-generated** (a timestamp, an id, a run counter).
- **Datetimes are stored as ISO 8601 strings, not native BSON dates.** Every model in
  this codebase is written to MongoDB via Pydantic's `model_dump(mode="json")` (or an
  explicit `.isoformat()` call), which serializes `datetime` to a string. There is no
  collection in this database where a datetime field is a native Mongo `Date` type.
- **Required/Optional** reflects the Pydantic model where one exists (a field with no
  default and a non-`| None` type is Required). Three collections — `threads`,
  `processing_runs`/`migration_runs` (partially), and `ingested_files` — have **no
  Pydantic model at all**; they are built and read as plain Python dicts throughout.
  For those, "Required" reflects what the code that writes them always includes.
- Where the code does not define a field's precise meaning or scale, this document
  says so explicitly rather than guessing.

---

## `emails`

**Purpose:** One document per ingested email — its raw content, its pipeline
processing state, and its post-analysis classification.

**Schema cleanup (collection-by-collection pass, emails first):** this section
was rewritten after discovering it was stale against the actual code — the
"Canonical ID refactor" in `app.pipeline.ingest_raw_email` reassigns
`message_id` to the canonical `EML-nnn` value (it is **not** the original
Gmail id despite the name), with `source_message_id` now holding that
permanent original id instead. The table below reflects the current, verified
behavior. `id` and `record_id` (both true duplicates of `message_id` — always
identical to it) and `date` (`timestamp`'s date-only component) were removed
from the schema entirely as a result — see
`scripts/remove_email_id_record_id_date_fields.py` for the cleanup script.

**Dashboard visibility:** every field in this table is shown on the Emails tab
(`app/ui/dashboard.py:_render_emails_tab`), in the order defined by
`EMAIL_COLUMN_ORDER` (`app/ui/column_descriptions.py`), sorted newest-first by
`timestamp`.

There is a Pydantic model for the raw side (`app.email.models.Email`), but the
document actually stored in MongoDB has more fields than that model:
`processing_status`, `entities_referenced`, `goal_pillar`, and `label_applied`
are all added later, by `EmailRepository.set_stage`/`set_entity_metadata`
(`app/database/repositories.py`), and appear in no single Pydantic model —
`emails` is a composite of the `Email` model plus these repository-written
fields.

**Removed fields:** `confidence`, `priority`, `source_type`, `source_link`,
`id`, `record_id`, and `date` have all been removed from the `emails` document
entirely (no longer extracted, computed, or persisted). This also removed the
`priority` filter parameter from the `search_emails` MCP tool and the
`"p1_emails"` category from the `whats_on_my_table` MCP tool's response. If you
find any of these seven fields in an older document in a live database, it is
leftover from before these changes, not something current code still writes —
see `scripts/remove_email_confidence_priority_fields.py`,
`scripts/remove_email_source_type_and_link_fields.py`, and
`scripts/remove_email_id_record_id_date_fields.py` for the cleanup scripts
that strip them from existing documents.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `message_id` | string | Yes | **Canonical internal email identifier (`EML-nnn`).** The Mongo dedup/upsert key (unique index), and the value every other collection's reference/derived-id (`reply_drafts.reply_id`, `knowledge_items.source_emails`, `commitments.source_record`, etc.) is built from. Reassigned exactly once, atomically, at ingestion (existing value reused on a dedup match) — never the original Gmail id despite the field name; see `source_message_id` for that | Deterministic pipeline (`app.pipeline.ingest_raw_email`) | `"EML-001"` |
| `thread_id` | string | Yes | **Canonical internal thread identifier (`THR-nnn`)** this email belongs to — see the `threads` section below for how it's resolved | Deterministic pipeline (`app.pipeline.resolve_and_persist_thread`) | `"THR-001"` |
| `source_message_id` | string | Yes | **The original, permanent Gmail/provider message identifier.** Set once at ingestion, never reassigned. This is what dedup actually keys on (not `message_id`) — a retry of the exact same source message always resolves to the same existing `message_id` | Gmail/source ingestion | `"18f2a9b1c3d4e5f6"` |
| `source_thread_id` | string \| null | No | The original, permanent Gmail/provider thread identifier, if the source supplied one. Set once, never rewritten | Gmail/source ingestion | `null` |
| `from` | object `{name, email}` | Yes | Sender | Gmail/source ingestion | `{"name": "Ashok Ganapam", "email": "ashok@databeat.io"}` |
| `to` | list[object] | Yes (list, may be empty) | Recipients | Gmail/source ingestion | `[{"name": null, "email": "me@company.com"}]` |
| `cc` | list[object] | No (defaults to `[]`) | CC recipients | Gmail/source ingestion | `[]` |
| `subject` | string | Yes | Email subject line | Gmail/source ingestion | `"Re: Pricing for Q3"` |
| `body` | string | Yes | Full email body text | Gmail/source ingestion | `"Hi, following up on..."` |
| `timestamp` | string (ISO datetime) | Yes | When the email was sent. Used for the dashboard's newest-first sort | Gmail/source ingestion | `"2026-03-04T14:02:00Z"` |
| `in_reply_to` | string \| null | No | Threading header (message id this replies to) | Gmail/source ingestion | `null` |
| `references` | list[string] | No (defaults to `[]`) | Threading header (chain of prior message ids) | Gmail/source ingestion | `[]` |
| `attachments` | list[string] | No (defaults to `[]`) | Attachment filenames | Gmail/source ingestion | `[]` |
| `labels` | list[string] | No (defaults to `[]`) | Gmail label ids, plus (via `$addToSet`) the applied `label_applied` triage value once analysis completes | Gmail/source ingestion, then deterministic pipeline (appends `label_applied`) | `["INBOX", "Needs reply"]` |
| `processing_status` | object `{stage, error, failed_stage, updated_at}` | Always present after first ingest | Current pipeline stage (see `ProcessingStage` enum: RECEIVED → VALIDATED → THREADED → ANALYZED → CONTEXT_BUILT → KNOWLEDGE_PROCESSED → ENTITIES_PROCESSED → REPLY_PROCESSED → MEETING_PROCESSED → COMPLETED, or FAILED at any point) plus the error if one occurred | Deterministic pipeline (`EmailRepository.set_stage`) | `{"stage": "COMPLETED", "error": null, "failed_stage": null, "updated_at": "2026-03-04T14:02:31Z"}` |
| `entities_referenced` | object, keys: `people`, `projects`, `commitments`, `follow_ups`, `meetings`, `personal`, `opportunities` | Set only once analysis completes | Canonical IDs of every record this email caused to be created or updated — the audit trail from an email back to everything it produced | Entity resolution | `{"people": ["PER-004"], "projects": ["PRJ-002"], "commitments": [], "follow_ups": [], "meetings": [], "personal": [], "opportunities": ["OPP-001"]}` |
| `goal_pillar` | string (free text; only `"Sales"` has defined behavior) | Set only once analysis completes | Business category assigned to the email — see "Distinguishing `goal_pillar` and `label_applied`" below | Claude Desktop analysis / internal LLM call | `"Sales"` |
| `label_applied` | string, one of: `"Needs reply: ASAP"`, `"Needs reply"`, `"Needs reply: mention"`, `"Read only"`, `"Delete"`, `"Undecided"` | Set only once analysis completes (defaults to `"Undecided"` in the schema if analysis omits it) | Six-way triage label — independent of `goal_pillar` (see below) | Claude Desktop analysis / internal LLM call | `"Needs reply"` |

### Relationships

- `entities_referenced.people/projects/commitments/follow_ups/meetings/personal/opportunities` → the `id` field of documents in `people`, `projects`, `commitments`, `follow_ups`, `meetings`, `personal_items`, `opportunities` respectively.
- `message_id` is referenced by `threads.message_ids`, `context_snapshots.triggering_email_id`, `reply_drafts.source_email_id`, `knowledge_items.source_emails`, `commitments.source_record`.
- `source_message_id`/`source_thread_id` are never referenced by any other collection — they exist purely for traceability back to the original Gmail/provider identity, not as a foreign key.
- Indexes: unique on `message_id`; unique + sparse on `source_message_id` (sparse because a not-yet-backfilled historical document may lack it); non-unique on `processing_status.stage`.

---

## Distinguishing `goal_pillar` and `label_applied`

These two fields both live on `emails` (and `goal_pillar` also appears on `people`,
`projects`, and `commitments`), get set at the same time by the same analysis step,
and are easy to conflate. They are independent of one another:

- **`goal_pillar`** — a free-text business-category classification. The only value
  with defined downstream behavior is exactly `"Sales"` (case/whitespace-normalized):
  it is the field that, combined with a resolvable project mention, gates whether an
  `opportunities` record is created (see `opportunities` below). Every other value
  (`""`, `"Finance"`, `"Operations"`, `"Product Development"`, etc.) is accepted and
  stored but has no further defined behavior beyond being stored and displayed.
- **`label_applied`** — the BRD's six-way triage label (`"Needs reply: ASAP"`,
  `"Needs reply"`, `"Needs reply: mention"`, `"Read only"`, `"Delete"`, `"Undecided"`).
  It is completely independent of `goal_pillar` — an email can be
  `goal_pillar="Sales"` and `label_applied="Read only"` at the same time.

`emails` no longer has a `confidence` or `priority` field at all (removed; see the
note at the top of the `emails` section above). `knowledge_items.confidence` is an
unrelated field on a different collection, with its own code-defined update rule
(starts at `0.75`, +0.01 per re-confirmation, capped at `0.99`) — see the
`knowledge_items` section below.

**Also worth separating:** `context_snapshots.context.opportunity` (a free-form dict
of deal notes on one thread, no fixed schema) is a completely different thing from the
`opportunities` collection (a structured, dedicated sales-deal record). Sharing the
word "opportunity" is coincidental naming, not a data relationship — nothing in the
code links one to the other.

---

## `threads`

**Schema cleanup (collection-by-collection pass, threads second, after
emails):** this section was rewritten after discovering it was stale against
the actual code, same pattern as the `emails` section above —
`app.pipeline._upsert_thread` reassigns `thread_id` to the canonical `THR-nnn`
value (it is **not** "a genuine source/Gmail thread id... or the application's
own synthetic fallback `thread_{message_id}`" as this section used to say —
that description predates the canonical ID refactor). `source_thread_id` now
holds the genuine original Gmail thread id instead (or `null` if the source
never supplied one). `id` (a true duplicate of `thread_id` — always
identical) was removed from the schema entirely — see
`scripts/remove_thread_id_field.py`.

**Purpose:** One row per resolved conversation thread — the grouping unit for
context, knowledge, commitments, meetings, and canonical people/orgs.

**Dashboard visibility:** `thread_id` and `source_thread_id` (when set) appear
together on the dashboard, as the entries of the Thread Explorer / Context
Evolution thread-selector dropdown (e.g. `"THR-001 (gmail_thread_xyz)"`,
falling back to just `"THR-001"` when there's no source thread id —
`app/ui/data.py:_thread_display_label`). There is no raw table/dataframe view
of this collection anywhere in the dashboard. `normalized_subject`,
`participant_emails`, `message_ids`, `source_message_ids`, `last_message_at`,
`person_ids`, and `org_ids` are Backend-only — never displayed as values
anywhere in `app/ui/dashboard.py`.

**No Pydantic model exists for this collection.** It is built and read as a plain
Python dict throughout `app/pipeline.py`; `app.email.threading.ThreadCandidate` is a
read-side helper used only to decide which thread an email belongs to, not what gets
persisted.

**Removed fields:** `id` has been removed from the `threads` document entirely
(no longer written). If you find it in an older document in a live database,
it is leftover from before this change — see `scripts/remove_thread_id_field.py`
for the cleanup script that strips it from existing documents.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `thread_id` | string | Yes | **Canonical internal thread identifier (`THR-nnn`).** The unique upsert key, and the value every other collection's `thread_id` field/derived-id (`knowledge_items.knowledge_id`, `calendar_actions.meeting_fingerprint`'s ambiguous fallback, etc.) is built from. Assigned exactly once, atomically, the moment a genuinely new thread is first created — never regenerated when a later message is added to an existing thread. Not the original Gmail id despite sometimes looking like one historically; see `source_thread_id` for that | Deterministic pipeline (`app.pipeline._upsert_thread`) | `"THR-001"` |
| `source_thread_id` | string \| null | No | **The original, permanent Gmail/provider thread identifier**, if the source supplied one (`null` if not — many threads are resolved by subject/participant/`in_reply_to` matching rather than an explicit source thread id). Set once at creation, never rewritten on a later message | Gmail/source ingestion | `"gmail_thread_xyz"` or `null` |
| `normalized_subject` | string | Yes | De-duplicated subject line (case/prefix-normalized), used only for thread matching | Deterministic pipeline | `"pricing for q3"` |
| `participant_emails` | list[string] (sorted) | Yes | Every email address that has posted (From/To/CC) in this thread | Deterministic pipeline | `["ashok@databeat.io", "me@company.com"]` |
| `message_ids` | list[string] (sorted) | Yes | Every canonical `message_id` (`EML-nnn`) in this thread | Deterministic pipeline | `["EML-001"]` |
| `source_message_ids` | list[string] (sorted) | Yes | Every original Gmail message id in this thread — used solely for `in_reply_to`/`references` matching, which must compare against raw Gmail ids, never canonical ones | Deterministic pipeline | `["18f2a9b1c3d4e5f6"]` |
| `last_message_at` | string (ISO datetime) | Yes | Timestamp of the most recent message (kept as a running max, not overwritten with an older value) | Deterministic pipeline | `"2026-03-04T14:02:00Z"` |
| `person_ids` | list[string] (sorted) | Set only after entity resolution runs | Every canonical Person resolved for this thread (`person.open_threads` contains this `thread_id`) — **always recomputed fresh from current Person records, never accumulated/backfilled from stale state** | Entity resolution (`app.pipeline._link_thread_to_entities`) | `["PER-004", "PER-011"]` |
| `org_ids` | list[string] (sorted) | Set only after entity resolution runs | Every Organization those same people belong to | Entity resolution | `["ORG-002"]` |

### Relationships

- `thread_id` is referenced by `emails.thread_id`, `context_snapshots.thread_id`, `knowledge_items.thread_id`, `commitments.thread_id`, `follow_ups.thread_id`, `meetings.thread_id`, `reply_drafts.thread_id`, `calendar_actions.thread_id`, `people.open_threads`, `thread_events.thread_id`.
- `person_ids`/`org_ids` → `people.id` / `organizations.id`.
- `source_thread_id` is never referenced by any other collection — purely for traceability back to the original Gmail/provider identity, not as a foreign key.
- Index: unique on `thread_id`; non-unique sparse on `source_thread_id`. (`entity_migration.py`'s optional, never-auto-run "indexes" stage additionally recommends non-unique indexes on `person_ids`/`org_ids` — not created by default; see `migration_runs` below.)

---

## `context_snapshots`

**Purpose:** A versioned, cumulative snapshot of one thread's evolving
deal/conversation context — one document per version, never overwritten.

**Dashboard visibility:** `context_version` and `triggering_email_id` are shown in
the Thread Explorer expander title; `context` is shown there as a raw JSON dump; both
tabs show `changes_from_previous_context` as formatted text lines (Thread Explorer:
every version's changes; Context Evolution: only the latest version's). `thread_id` is
used only to select which snapshots to show, not displayed as a value. `created_at` is
Backend-only.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `thread_id` | string | Yes | Which thread this version belongs to | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |
| `context_version` | int | Yes | 1-based version number, incremented per triggering email | Deterministic pipeline | `2` |
| `triggering_email_id` | string | Yes | The `message_id` that produced this version | Deterministic pipeline | `"18f2a9b1c3d4e5f6"` |
| `context` | object (`ThreadContext`: `summary`, `participants`, `company`, `opportunity`, `requirements`, `pain_points`, `products_discussed`, `competitors`, `pricing`, `objections`, `buying_signals`, `decisions`, `commitments`, `open_questions`, `next_actions`, `meetings`) | Yes | The full accumulated context as of this version. Most sub-fields are lists of `{value, basis, source_email_ids}`; `company`/`opportunity`/`pricing` are free-form dicts | Claude Desktop analysis / internal LLM call (produces the bounded `ContextDelta`), merged deterministically by `app.context.engine.apply_context_delta` | see note below |
| `changes_from_previous_context` | list[object `{type, field, detail, source_email_id}`] | No (defaults to `[]`) | What this version added/removed/updated versus the prior one; `type` is one of `"ADDED"`, `"REMOVED"`, `"UPDATED"` | Deterministic pipeline (`app.context.diff.diff_context`) | `[{"type": "ADDED", "field": "pricing", "detail": "budget: $50k", "source_email_id": "18f2a9b1c3d4e5f6"}]` |
| `created_at` | string (ISO datetime) | Yes | When this snapshot was written | System-generated | `"2026-03-04T14:02:15Z"` |

**Important naming collision:** `context.opportunity` here is a **free-form dict of
deal notes on one thread** (e.g. `{"stage": "negotiating"}` — whatever the LLM/analysis
puts there; the code does not constrain its shape). It is a **different thing** from
the `opportunities` collection below, which is a structured, dedicated CRM record. See
"Distinguishing `goal_pillar` and `label_applied`" above for the full explanation.

### Relationships

- `thread_id` → `threads.thread_id`. `triggering_email_id` → `emails.message_id`.
- Indexes: unique on `(thread_id, triggering_email_id)`; non-unique on `(thread_id, context_version)`.

---

## `knowledge_items`

**Purpose:** Individual extracted facts (stated or inferred), each with a confidence
score and a full edit history, deduplicated per thread.

**Dashboard visibility:** `subject_key`, `predicate`, `current_value`, `basis`,
`confidence`, and `history` are shown on the Knowledge tab. `knowledge_id`,
`thread_id`, `fact_key`, `person_id`, `org_id`, `source_emails`, `first_seen_at`,
`last_confirmed_at`, and `status` are Backend-only — not displayed anywhere.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `knowledge_id` | string | Yes | `knowledge_{thread_id}_{subject_key}_{predicate}_{fact_key}` | System-generated | `"knowledge_thread_18f2..._databeat_requires_seat_count"` |
| `thread_id` | string | Yes | Thread this fact belongs to | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |
| `subject_key` | string | Yes | Slugified subject (a person/org name, or the thread itself for a thread-level fact) | Deterministic pipeline | `"databeat"` |
| `predicate` | string | Yes | The relation, e.g. `"requires"`, `"has_pain_point"`, `"mentioned_competitor"`, `"showed_buying_signal"`, `"raised_objection"`, or a free-text predicate from an LLM-extracted `Fact` | Claude Desktop analysis / internal LLM call, deterministic pipeline (for the five fixed predicates) | `"requires"` |
| `fact_key` | string | Yes | A finer-grained classification of what kind of value this is (e.g. `"seat_count"`) | Deterministic pipeline (`app.knowledge.normalize.classify_fact_key`) | `"seat_count"` |
| `current_value` | string | Yes | The fact's current value | Claude Desktop analysis / internal LLM call | `"100 seats"` |
| `person_id` | string \| null | No | Set only when `subject_key` could be unambiguously matched against a Person already resolved for this thread | Entity resolution | `"PER-004"` |
| `org_id` | string \| null | No | Set only when `subject_key` matched an Organization instead (not every fact is about a specific person or company — neither being set is a correct, common outcome) | Entity resolution | `null` |
| `history` | list[object `{value, source_email_id, recorded_at}`] | No (defaults to `[]`) | Every prior value this fact has had, oldest first | Deterministic pipeline | `[{"value": "80 seats", "source_email_id": "...", "recorded_at": "..."}]` |
| `source_emails` | list[string] | No (defaults to `[]`) | Every `message_id` that confirmed/updated this fact | Deterministic pipeline | `["18f2a9b1c3d4e5f6"]` |
| `basis` | string, one of: `"stated"`, `"inferred"` | Yes | Whether the fact was directly stated or inferred | Claude Desktop analysis / internal LLM call | `"stated"` |
| `first_seen_at` | string (ISO datetime) | Yes | When this fact was first extracted | System-generated | `"2026-03-01T09:00:00Z"` |
| `last_confirmed_at` | string (ISO datetime) | Yes | Most recent email that re-confirmed or updated this fact | Deterministic pipeline | `"2026-03-04T14:02:00Z"` |
| `confidence` | float | Yes | **Code-defined scale:** starts at `0.75` on creation, and increases by `+0.01` (capped at `0.99`) every time the same fact is re-confirmed by a later email (`app.knowledge.deduplication._apply_update`). Never decreases | Deterministic pipeline | `0.78` |
| `status` | string, declared as one of: `"active"`, `"contradicted"`, `"retracted"` | No (defaults to `"active"`) | **`"contradicted"` and `"retracted"` are declared in the model but no code path in this repository ever sets them — every `knowledge_items.status` in practice is `"active"`.** | (declared only; never set to anything but the default) | `"active"` |

### Relationships

- `thread_id` → `threads.thread_id`. `person_id`/`org_id` → `people.id`/`organizations.id`. `source_emails` → `emails.message_id`.
- Index: unique on `(thread_id, subject_key, predicate, fact_key)`.

---

## `reply_drafts`

**Purpose:** One draft reply per email that warranted one, plus its approval-workflow
state.

**Dashboard visibility:** only `draft.subject` and `draft.body` are shown as values
on the Reply Approval tab; `status` is used only to filter which drafts appear there
(only `"awaiting_approval"` drafts are shown), not displayed as a column. Every other
field (`reply_id`, `thread_id`, `source_email_id`, `person_id`, `org_id`,
`created_by`, `created_at`, `approved_by`, `sent_at`) is Backend-only.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `reply_id` | string | Yes | `reply_{message_id}` | System-generated | `"reply_18f2a9b1c3d4e5f6"` |
| `thread_id` | string | Yes | Thread this reply belongs to | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |
| `source_email_id` | string | Yes | The email being replied to (the upsert key) | Deterministic pipeline | `"18f2a9b1c3d4e5f6"` |
| `status` | string, one of: `"no_reply_required"`, `"awaiting_approval"`, `"approved"`, `"edited"`, `"rejected"`, `"cancelled"`, `"simulated_sent"`, `"sent"` | Yes | Approval-workflow state. **In this codebase's actual code paths, only `"awaiting_approval"` (on creation), `"approved"`, `"edited"` (→ back to `"awaiting_approval"`), `"rejected"`, and `"simulated_sent"` are ever produced** — `"no_reply_required"`, `"cancelled"`, and a literal `"sent"` (as opposed to `"simulated_sent"`) are declared but not set anywhere in this repository | Deterministic pipeline (create); human/MCP update (Approve/Reject/Edit, via the dashboard buttons — the only human-facing UI for this) | `"awaiting_approval"` |
| `draft` | object `{subject, body}` | Yes | The reply text itself | Claude Desktop analysis / internal LLM call | `{"subject": "Re: Pricing for Q3", "body": "Hi Ashok, ..."}` |
| `person_id` | string \| null | No | The sender's canonical Person (via `resolve_canonical_person_for_email`, lifecycle-aware — never a raw email lookup) | Entity resolution | `"PER-004"` |
| `org_id` | string \| null | No | That person's Organization | Entity resolution | `"ORG-002"` |
| `created_by` | string | No (defaults to `"sales_agent"`) | Not observed to be set to anything else | System-generated | `"sales_agent"` |
| `created_at` | string (ISO datetime) \| null | No | When the draft was created. Optional because a draft persisted before this field existed has none in MongoDB at all, and it is never backfilled | System-generated | `"2026-03-04T14:02:20Z"` |
| `approved_by` | string \| null | No | Set to `"ui_user"` by the dashboard's Approve button; otherwise null | Human/MCP update | `"ui_user"` |
| `sent_at` | string (ISO datetime) \| null | No | Set when `simulate_send` runs (dashboard Approve button). **This is a simulated send — it never calls a real email-sending API; see the dashboard reference for detail** | System-generated | `"2026-03-04T15:00:00Z"` |

### Relationships

- `thread_id` → `threads.thread_id`. `source_email_id` → `emails.message_id`. `person_id`/`org_id` → `people.id`/`organizations.id`.
- Index: unique on `source_email_id`.

---

## `calendar_actions`

**Purpose:** One detected-meeting proposal per candidate calendar event, plus its
approval-workflow state.

**Dashboard visibility:** `event.title`, `event.start`, `event.end`, `event.timezone`
are shown on the Calendar Approval tab; `reason` is shown only when
`status == "needs_clarification"`. `status` is used only to filter/branch which
actions appear and what buttons show, not displayed as a column. Every other field
(`thread_id`, `meeting_fingerprint`, `event.description`, `event.attendees`,
`actor_type`, `person_id`, `org_id`, `meeting_id`) is Backend-only.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `thread_id` | string | Yes | Thread this proposal belongs to (part of the upsert key) | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |
| `meeting_fingerprint` | string | Yes | Dedup key: normalized title + start + end (or `needs_clarification_{thread_id}` when ambiguous) — part of the upsert key | Deterministic pipeline (`app.calendar.actions.make_fingerprint`) | `"pricing_call_2026-03-10t15:00:00+00:00_2026-03-10t15:30:00+00:00"` |
| `status` | string, one of: `"pending"`, `"awaiting_approval"`, `"approved"`, `"scheduled"`, `"failed"`, `"rejected"`, `"needs_clarification"` | Yes | Approval-workflow state. **Only `"awaiting_approval"`/`"needs_clarification"` (on creation), `"scheduled"`/`"failed"` (Create-on-calendar button), and `"rejected"` (Ignore button) are ever actually produced — `"pending"` and `"approved"` are declared but never set anywhere in this repository** | Deterministic pipeline (create); human/MCP update (dashboard Create/Ignore buttons) | `"awaiting_approval"` |
| `event` | object `{title, start, end, timezone, description, attendees}` | Yes | The proposed meeting. `attendees` is **always an empty list** — a Pydantic validator on `CalendarEvent` rejects any non-empty value outright | Deterministic pipeline (regex-based `app.calendar.detector.detect_meeting` — never an LLM call) | `{"title": "Pricing call", "start": "2026-03-10T15:00:00Z", "end": "2026-03-10T15:30:00Z", "timezone": "UTC", "description": "...", "attendees": []}` |
| `actor_type` | string, always `"authenticated_user"` | No (default) | Not observed to be set to anything else — no other value is declared | System-generated | `"authenticated_user"` |
| `reason` | string \| null | No | Populated only when `status == "needs_clarification"` (why), or `"failed"` (why it failed) | Deterministic pipeline | `"missing meeting duration"` |
| `person_id` | string \| null | No | The triggering email's sender's canonical Person | Entity resolution | `"PER-004"` |
| `org_id` | string \| null | No | That person's Organization | Entity resolution | `"ORG-002"` |
| `meeting_id` | string \| null | No | Linked to a `meetings` record only when **exactly one** candidate meeting from this email shares this action's date — genuine ambiguity (0 or >1 same-date candidates) is left `null` rather than guessed | Entity resolution | `"MTG-014"` |

### Relationships

- `thread_id` → `threads.thread_id`. `person_id`/`org_id` → `people.id`/`organizations.id`. `meeting_id` → `meetings.id`.
- Index: unique on `(thread_id, meeting_fingerprint)`.
- **Non-negotiable safety invariant, enforced in code, not just convention:** `event.attendees` must never be non-empty; several code paths (`CalendarEvent`'s own validator, `approve_calendar_action`'s defense-in-depth re-check, `duplicate_consolidation`'s safety gate) all independently refuse to let a non-empty value through.

---

## `people`

**Purpose:** Every person the system has identified from email traffic, resolved to
one canonical record per real individual.

`Person` (`app.entities.models.Person`) is a full Pydantic model; every field below is
persisted exactly as modeled.

**Schema cleanup (collection-by-collection pass, people third, after emails and
threads):** this section was corrected in two ways while investigating a
request to clean up `goal_pillar`/`role`/`source`. First, it still documented
a `type` field (`"operator"`/unset) that no longer exists — it was replaced by
`role` in an earlier session change (operator identity is resolved purely by
email, never by any Person field), but this section was never updated to
match; `role` was missing from this table entirely. Second, `source` and
`note_link` were found to be genuinely unused (see "Removed fields" below) and
removed.

**Dashboard visibility:** the People tab (`app/ui/dashboard.py:_render_people_tab`)
shows fields in the order defined by `PEOPLE_COLUMN_ORDER`
(`app/ui/column_descriptions.py`) — no longer an unfiltered raw dump.
`goal_pillar` is a real, persisted field but is hidden from the dashboard (see
its own row below for why).

**Removed fields:** `reports_to`, `review_flag`, `role_in_pillar`, `tier`,
`voice_register`, `preferences`, `type`, `source`, and `note_link` have all been
removed from `Person` entirely. Of the original six: four (`reports_to`,
`role_in_pillar`, `tier`, `voice_register`) had never been written by any code
path; `review_flag` was written (on a no-email Person) but never read back
anywhere; `preferences` was genuinely load-bearing (a per-person reply-drafting
customization) and its removal is covered in that migration's own history
(`scripts/remove_people_legacy_fields.py`). `type` was replaced by `role` in a
later change (see `role`'s own row). `source` was always just the unused
Pydantic default `"gmail"` — never read or branched on anywhere. `note_link`
was never read or written by any code path at all — the clearest "fully dead"
field found in this collection. If you find any of these nine fields in an
older document in a live database, it is leftover from before the relevant
change — see `scripts/remove_people_legacy_fields.py` and
`scripts/remove_person_source_note_link_fields.py`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `PER-###` | System-generated (`app.entities.ids.next_id`) | `"PER-004"` |
| `name` | string | Yes | Display name | Entity resolution | `"Ashok Ganapam"` |
| `email` | string \| null | No | Lowercased email address. **Omitted entirely from the document (not stored as `null`) when absent** — required so MongoDB's sparse unique index on this field allows more than one no-email person | Entity resolution / Gmail source ingestion | `"ashok@databeat.io"` |
| `aliases` | list[string] | No (defaults to `[]`) | Alternate names proven to belong to this person by an actual resolution match (never speculative) | Entity resolution | `["Ashok"]` |
| `org` | string \| null | No | Free-text company name, as mentioned. Also the lookup key `get_company_summary` filters on | Entity resolution | `"DataBeat"` |
| `org_id` | string \| null | No | Canonical Organization, resolved by email domain only — never by company-name similarity | Entity resolution | `"ORG-002"` |
| `role` | string \| null | No | Professional designation (e.g. `"CTO"`), extracted from an email's own content. Set at creation and backfilled on reuse — but never overwrites an already-stated value. Never used to detect the operator, which is resolved purely by email | Entity resolution | `"CTO"` |
| `goal_pillar` | string \| null | No | Never written by any Person-creation path — always `null` in practice today. IS read by `app.entities.person_context.get_bounded_person_context_for_llm`, which feeds the real pipeline's LLM-context step — kept in the schema for that reason, just hidden from the dashboard | — | `null` |
| `last_inbound` | string (ISO datetime) \| null | No | Latest email known received FROM this person. Forward-only (never moves backward, regardless of processing order) | Entity resolution | `"2026-03-04T14:02:00Z"` |
| `last_outbound` | string (ISO datetime) \| null | No | Latest email known sent TO this person. Forward-only | Entity resolution | `null` |
| `open_threads` | list[string] | No (defaults to `[]`) | Every `thread_id` this person has been part of — append-only | Entity resolution | `["THR-001"]` |
| `status` | string, values actually used: `"active"`, `"merged"` | No (defaults to `"active"`) | `"merged"` is set only by the (separately invoked, admin-only) duplicate-consolidation CLI (`app/duplicate_consolidation.py`), never by the live pipeline. Drives the live `_reuse_target` redirect — actively used, not just bookkeeping | Deterministic pipeline (admin CLI only) | `"active"` |
| `merged_into` | string \| null | No | Set together with `status="merged"` — the canonical Person's id this record was consolidated into. Read by the same live redirect logic as `status` | Deterministic pipeline (admin CLI only) | `null` |

### Relationships

- `org_id` → `organizations.id`. `merged_into` → another `people.id`.
- Referenced by: `threads.person_ids`, `projects.person_ids`, `opportunities.person_ids`, `commitments.person_id`, `follow_ups.person_id`, `meetings.person_ids`, `knowledge_items.person_id`, `reply_drafts.person_id`, `calendar_actions.person_id`, `person_context_snapshots.person_id`.
- Indexes: unique on `id`; unique-sparse on `email`; non-unique on `open_threads` (a multikey index, since `open_threads` is an array).

---

## `organizations`

**Purpose:** Companies resolved from email domains.

**Dashboard visibility:** every field in this table is shown on the Organizations tab
— an unfiltered raw dump.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `ORG-###` | System-generated | `"ORG-002"` |
| `name` | string | Yes | Display name — the domain itself as a placeholder until a real name hint is supplied (never overwrites a real name once set) | Entity resolution | `"DataBeat"` |
| `domain` | string \| null | No | Email domain — the sole identity key for this collection; a company name alone is never sufficient to create or match one | Entity resolution | `"databeat.io"` |
| `aliases` | list[string] | No (defaults to `[]`) | Not observed to be set by any current code path | — | `[]` |
| `source` | string | No (defaults to `"gmail"`) | Not observed to be set to anything else | System-generated | `"gmail"` |

### Relationships

- Referenced by: `people.org_id`, `threads.org_ids`, `projects.org_id`, `opportunities.org_id`, `commitments.org_id`, `follow_ups.org_id`, `meetings.org_id`, `knowledge_items.org_id`, `reply_drafts.org_id`, `calendar_actions.org_id`.
- No index is created by `app/database/indexes.py`'s `initialize_indexes` (the one that runs automatically). A unique index on `domain` is only in `app/entity_migration.py`'s optional "indexes" stage, which is **never auto-run**.

---

## `projects`

**Purpose:** Named engagements or initiatives extracted from emails — the mechanism
that predates and underlies the `opportunities` layer.

**"Project product gap" closed:** `status`, `owner`, `health`, `next_milestone`,
`due` existed on this schema with no way to ever be set (confirmed directly in
`app.entities.resolution._resolve_project_impl`, which only ever passes
`id`/`project`/`entity`/`goal_pillar`/`person_ids`/`org_id`). Rather than
remove them (they're genuine, intended business fields — Project predates
Opportunity's CRM-overlay role and still needs its own status for a non-Sales
project that never gets an Opportunity), a new `update_project_fields` MCP
tool was added, mirroring `update_opportunity_fields` exactly. They are no
longer hidden on the dashboard.

**Dashboard visibility:** `PROJECTS_COLUMN_ORDER` (`app/ui/column_descriptions.py`)
controls order/visibility — no longer an unfiltered raw dump. `cluster`,
`objective`, `target`, `collaborators`, `last_movement`, `note_link`, `source`
remain hidden (real MongoDB fields, confirmed never populated, but not named
in the product-gap ask as needing a write mechanism — deferred, not removed).

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `PRJ-###` | System-generated | `"PRJ-002"` |
| `project` | string | Yes | Project/deal name, as mentioned | Claude Desktop analysis / internal LLM call | `"DataBeat Q3 Rollout"` |
| `cluster` | string \| null | No | Not observed to be set by any current code path | — | `null` |
| `entity` | string \| null | No | Free-text company name (mirrors the LLM's `org` hint) | Claude Desktop analysis / internal LLM call | `"DataBeat"` |
| `goal_pillar` | string \| null | No | Copied from the triggering email's `goal_pillar` — this is the field that (combined with a resolved project mention) gates Opportunity creation; see "Distinguishing `goal_pillar` and `label_applied`" above | Claude Desktop analysis / internal LLM call | `"Sales"` |
| `objective` | string \| null | No | Not observed to be set by any current code path (the schema that feeds project creation, `MentionedProject`, has no `objective` field — only `objective_hint`, which is never copied across) | — | `null` |
| `target` | string \| null | No | Not observed to be set by any current code path | — | `null` |
| `status` | string \| null | No | Human-managed only, via the `update_project_fields` MCP tool (new). Never inferred from email content | `update_project_fields` (admin/human-invoked) | `"on_track"` |
| `owner` | string \| null | No | Human-managed only, via `update_project_fields`. Different field from the human-managed `opportunities.owner` | `update_project_fields` (admin/human-invoked) | `"Sandeep"` |
| `collaborators` | list[string] | No (defaults to `[]`) | Not observed to be set by any current code path | — | `[]` |
| `person_ids` | list[string] | No (defaults to `[]`) | Every Person already resolved for the same email whose `org` matches this project's `entity` — never inferred from name-text alone | Entity resolution | `["PER-004"]` |
| `org_id` | string \| null | No | That matching Organization | Entity resolution | `"ORG-002"` |
| `next_milestone` | string \| null | No | Human-managed only, via `update_project_fields` | `update_project_fields` (admin/human-invoked) | `"Signed contract"` |
| `due` | string (ISO datetime) \| null | No | Human-managed only, via `update_project_fields` | `update_project_fields` (admin/human-invoked) | `"2026-10-01T00:00:00Z"` |
| `health` | string \| null | No | Human-managed only, via `update_project_fields` | `update_project_fields` (admin/human-invoked) | `"green"` |
| `last_movement` | string (ISO datetime) \| null | No | Not observed to be set by any current code path | — | `null` |
| `note_link` | string \| null | No | Not observed to be set by any current code path | — | `null` |
| `source` | string | No (defaults to `"gmail"`) | Not observed to be set to anything else | System-generated | `"gmail"` |

**Dedup rule:** two mentions resolve to the same Project only when normalized
`entity` AND normalized `project` name AND `goal_pillar` all match exactly
(`app.entities.resolution.resolve_project` — accent-folding and `+`/`&`/`/`/`-`
joiner-normalization applied, never fuzzy/embedding similarity).

### Relationships

- `person_ids` → `people.id`. `org_id` → `organizations.id`.
- Referenced by: `emails.entities_referenced.projects`, `opportunities.project_ids`, `commitments.project_id`.

---

## `opportunities`

**Purpose:** A confirmed sales deal — a CRM overlay on top of `projects`, never a
replacement for it. Created only when an email is classified `goal_pillar == "Sales"`
**and** names a project mention that itself resolves to a real `project_id` — never
merely because an email is Sales-classified.

`Opportunity` (`app.entities.models.Opportunity`) is a full Pydantic model.

**Dashboard visibility:** every field in this table is shown on the Opportunities tab
(`app/ui/dashboard.py:_render_opportunities_tab`) — an unfiltered raw dump, read-only
(no edit controls; the human-managed fields below are still settable only through the
`update_opportunity_fields` MCP tool, never from the dashboard itself).

**Automatic (pipeline-derived) fields** — written and updated exclusively by
`app.entities.resolution.resolve_opportunity`, append-only, never touched by a human:
`id`, `name`, `entity`, `org_id`, `status` (creation only, always `"open"`),
`source_email_ids`, `project_ids`, `meeting_ids`, `person_ids`, `buying_signals`,
`last_activity_at`, `created_at`, `updated_at`.

**Human-managed fields** — settable **only** through the `update_opportunity_fields`
MCP tool, never inferred from email content, never touched by the pipeline: `stage`,
`owner`, `value`, `currency`, `expected_close_date`, `next_action`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `OPP-###` | System-generated | `"OPP-001"` |
| `name` | string | Yes | Copied from the resolving project mention's name | Entity resolution | `"DataBeat Q3 Rollout"` |
| `entity` | string \| null | No | Free-text company name, mirrors `projects.entity` | Entity resolution | `"DataBeat"` |
| `org_id` | string \| null | No | Canonical Organization | Entity resolution | `"ORG-002"` |
| `description` | string \| null | No | Not observed to be set by any current code path (no writer sets it — declared on the model but never populated) | — | `null` |
| `status` | string, one of: `"open"`, `"won"`, `"lost"` | No (defaults to `"open"`) | Deal outcome. **Only `"open"` is ever set by any current code path** (`"won"`/`"lost"` are valid values a caller of `update_opportunity_fields` could set, but the tool as written does not currently accept a `status` parameter at all — so in practice this field never changes from `"open"` today) | Entity resolution (creation only) | `"open"` |
| `stage` | string \| null | No | Free-text sales-pipeline stage (e.g. "Discovery"). **Human-managed only** — never inferred from email content | Human/MCP update (`update_opportunity_fields`) | `null` |
| `owner` | string \| null | No | Free-text CRM owner. **Human-managed only** | Human/MCP update | `null` |
| `value` | float \| null | No | Deal value. **Human-managed only** | Human/MCP update | `null` |
| `currency` | string \| null | No | **Human-managed only** | Human/MCP update | `null` |
| `expected_close_date` | string (ISO datetime) \| null | No | **Human-managed only** | Human/MCP update | `null` |
| `next_action` | string \| null | No | **Human-managed only** | Human/MCP update | `null` |
| `source_email_ids` | list[string] | No (defaults to `[]`) | Every email that touched this deal — append-only | Entity resolution | `["18f2a9b1c3d4e5f6"]` |
| `project_ids` | list[string] | No (defaults to `[project_id]` on creation) | The Project(s) this deal is linked to — **also the sole dedup key**: a second Sales email resolving to the same `project_id` is always the same Opportunity | Entity resolution | `["PRJ-002"]` |
| `meeting_ids` | list[string] | No (defaults to `[]`) | Meetings detected in the same email(s) — append-only | Entity resolution | `["MTG-014"]` |
| `person_ids` | list[string] | No (defaults to `[]`) | People resolved alongside this deal — append-only | Entity resolution | `["PER-004"]` |
| `buying_signals` | list[string] | No (defaults to `[]`) | Plain-text buying-signal strings copied from the triggering email's analysis — append-only, deduplicated | Entity resolution (copies from Claude Desktop analysis / internal LLM call) | `["asked about enterprise pricing"]` |
| `last_activity_at` | string (ISO datetime) | Yes | Bumped every time a new email/meeting touches this deal | Entity resolution | `"2026-03-04T14:02:00Z"` |
| `created_at` | string (ISO datetime) | Yes | — | System-generated | `"2026-03-01T09:00:00Z"` |
| `updated_at` | string (ISO datetime) | Yes | — | System-generated / human/MCP update | `"2026-03-04T14:02:00Z"` |

**Known V1 limitation** (stated directly in `resolve_opportunity`'s own docstring):
two textually distinct Projects that a human would call the same deal do not
auto-merge into one Opportunity — dedup is exclusively by `project_id`, with no
fuzzy/embedding matching.

### Relationships

- `project_ids` → `projects.id` (dedup key). `person_ids` → `people.id`. `org_id` → `organizations.id`. `meeting_ids` → `meetings.id`. `source_email_ids` → `emails.message_id`.
- Index: unique on `id`.

---

## `commitments`

**Purpose:** Promises made or owed, extracted from emails.

Stored field name `class` (a reserved word in Python) maps to the model's
`commitment_class` attribute via a Pydantic alias.

**Dashboard visibility:** every field in this table is shown on the Commitments tab
— an unfiltered raw dump.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `CMT-###` | System-generated | `"CMT-021"` |
| `what` | string | Yes | The commitment text | Claude Desktop analysis / internal LLM call | `"send updated pricing sheet"` |
| `class` | string, one of: `"mine"`, `"owed_to_me"`, `"theirs"`, `"recap"` | Yes | Per BRD 6.3: only `"mine"`/`"owed_to_me"` are ever chased (produce a `FollowUp`); `"theirs"`/`"recap"` are recorded but never chased | Claude Desktop analysis / internal LLM call | `"mine"` |
| `importance` | string \| null | No | Free-text importance hint from analysis | Claude Desktop analysis / internal LLM call | `null` |
| `owed_by` | string \| null | No | Free-text name | Claude Desktop analysis / internal LLM call | `"Me"` |
| `owed_to` | string \| null | No | Free-text name | Claude Desktop analysis / internal LLM call | `"Ashok"` |
| `person_id` | string \| null | No | Set only when `owed_by`/`owed_to` unambiguously matched a Person already resolved for this same email | Entity resolution | `"PER-004"` |
| `org_id` | string \| null | No | That matched person's Organization | Entity resolution | `"ORG-002"` |
| `source_record` | string | Yes | The `message_id` this commitment was extracted from | Deterministic pipeline | `"18f2a9b1c3d4e5f6"` |
| `made_on` | string (ISO datetime) | Yes | The triggering email's timestamp | Deterministic pipeline | `"2026-03-04T14:02:00Z"` |
| `committed_date` | string (ISO datetime) \| null | No | Resolved due date, if a date phrase was recognized | Deterministic pipeline (`app.entities.dates.resolve_date_phrase`, regex-based) | `"2026-03-06T00:00:00Z"` |
| `date_type` | string, one of: `"stated"`, `"inferred"`, `"window"` | No | How `committed_date` was derived | Deterministic pipeline | `"inferred"` |
| `status` | string | No (defaults to `"open"`) | **Declared as plain text, not an enum; no code path in this repository ever changes it from `"open"`** | (declared default only) | `"open"` |
| `goal_pillar` | string \| null | No | Copied from the triggering email | Claude Desktop analysis / internal LLM call | `"Sales"` |
| `project_id` | string \| null | No | Linked only when the commitment's counterparty's `org_id` matches **exactly one** project mentioned in the same email — ambiguous (0 or >1 matches) is left `null`, never guessed | Entity resolution | `"PRJ-002"` |
| `thread_id` | string \| null | No | The resolved thread (always set on the live creation path; optional only for backward compatibility with older direct-construction call sites) | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |

**Dedup rule:** an existing commitment in the same thread is reused when normalized
`what` + `class` + `date_type` + (day-level match for inferred dates, exact-instant
match for stated dates) all match.

### Relationships

- `person_id`/`org_id` → `people.id`/`organizations.id`. `project_id` → `projects.id`. `thread_id` → `threads.thread_id`. `source_record` → `emails.message_id`.
- Referenced by: `follow_ups.commitment_id`.
- Indexes: unique on `id`; non-unique on `thread_id`.

---

## `follow_ups`

**Purpose:** Chase items derived exclusively from a `"mine"`/`"owed_to_me"`
Commitment (never from a Meeting or PersonalItem, however "actionable").

**"Follow-up/Commitment product gap" closed, UI-layer only:** this document
has no `what` text of its own — `app.ui.data.list_follow_ups` resolves it
live from the parent Commitment (`commitment_id`), plus `person_name`/
`org_name` resolved from `person_id`/`org_id`, all display-only and never
written back to MongoDB. The stored schema below is unchanged.

**Dashboard visibility:** `FOLLOW_UPS_COLUMN_ORDER` (`app/ui/column_descriptions.py`)
controls order/visibility, with the derived `what`/`person_name`/`org_name`
leading — no longer an unfiltered raw dump. `escalation_level`/`surfaced`
remain hidden (confirmed never advanced from their constant defaults).

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `FUP-###` | System-generated | `"FUP-009"` |
| `commitment_id` | string \| null | At least one of `commitment_id`/`thread_id` required (model-level validator) | The parent Commitment | Entity resolution | `"CMT-021"` |
| `thread_id` | string \| null | See above | Copied from the parent commitment's own `thread_id` | Entity resolution | `"thread_18f2a9b1c3d4e5f6"` |
| `person_id` | string \| null | No | Inherited directly from the parent Commitment — never independently inferred | Entity resolution | `"PER-004"` |
| `org_id` | string \| null | No | Inherited from the parent Commitment | Entity resolution | `"ORG-002"` |
| `escalation_level` | int, one of `1`/`2`/`3`/`4` | No (defaults to `1`) | BRD 6.4's escalation ladder. **No scheduler exists to advance this** — every FollowUp created today stays at level 1 forever; this field is structural only | Entity resolution (creation only, always `1`) | `1` |
| `surfaced` | boolean | No (defaults to `false`) | Same as above — no code path ever sets this `true` | (declared default only) | `false` |
| `status` | string, one of: `"active"`, `"resolved"`, `"dropped"` | No (defaults to `"active"`) | No code path in this repository transitions this away from `"active"` today | (declared default only) | `"active"` |
| `audience` | string, one of: `"internal"`, `"client_fixed_date"`, `"client_open_window"`, `"his_own_question"` \| null | No | Only computed for `"owed_to_me"` commitments (`app.entities.dates.classify_follow_up_timing`). **`"his_own_question"` is declared but never actually produced** — nothing in the current extraction distinguishes the operator's own unanswered question from any other commitment | Deterministic pipeline | `"client_fixed_date"` |
| `follow_up_earliest_at` | string (ISO datetime) \| null | No | Start of the BRD's stated timing range | Deterministic pipeline | `"2026-03-05T00:00:00Z"` |
| `follow_up_latest_at` | string (ISO datetime) \| null | No | End of the BRD's stated timing range | Deterministic pipeline | `"2026-03-07T00:00:00Z"` |

### Relationships

- `commitment_id` → `commitments.id`. `thread_id` → `threads.thread_id`. `person_id`/`org_id` → `people.id`/`organizations.id`.
- Indexes: unique on `id`; non-unique (sparse) on `commitment_id` and on `thread_id`.

---

## `meetings`

**Purpose:** Meetings mentioned or held, extracted from emails.

**"Meeting product gap" closed, UI-layer only:** this document has no title/
subject field of its own — `app.ui.data.list_meetings` derives a display-only
`title` from the earliest email in the meeting's own `thread_id` (consistent
with how `threads.normalized_subject` is itself derived from the first
email), never written back to MongoDB. The stored schema below is unchanged.

**Dashboard visibility:** `MEETINGS_COLUMN_ORDER` (`app/ui/column_descriptions.py`)
controls order/visibility, with the derived `title` leading — no longer an
unfiltered raw dump. `minutes_record`, `next_meeting_date`, `agenda_target`,
`agenda_written` remain hidden (confirmed never set by `resolve_meeting`).

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `MTG-###` | System-generated | `"MTG-014"` |
| `date` | string (ISO datetime) \| null | No | Resolved meeting date, if a date phrase was recognized | Deterministic pipeline | `"2026-03-10T15:00:00Z"` |
| `attendees` | list[string] | No (defaults to `[]`) | Free-text attendee names, as mentioned | Claude Desktop analysis / internal LLM call | `["Ashok", "Me"]` |
| `person_ids` | list[string] | No (defaults to `[]`) | Attendees matched against people already resolved for this same email — never guessed | Entity resolution | `["PER-004"]` |
| `org_id` | string \| null | No | An attendee's Organization | Entity resolution | `"ORG-002"` |
| `project_or_pillar` | string \| null | No | Copied directly from the email's `goal_pillar` — a real, already-extracted value, never an independent guess (used by the meeting-category query/filter logic) | Claude Desktop analysis / internal LLM call | `"Sales"` |
| `minutes_record` | string \| null | No | Not observed to be set by any current code path | — | `null` |
| `actions_raised` | list[string] | No (defaults to `[]`) | Free-text action items raised in the meeting mention | Claude Desktop analysis / internal LLM call | `[]` |
| `next_meeting_date` | string (ISO datetime) \| null | No | Not observed to be set by any current code path | — | `null` |
| `agenda_target` | string \| null | No | Not observed to be set by any current code path | — | `null` |
| `actionable` | boolean | No (defaults to `false`) | `true` for any future-oriented meeting mention; `false` only when explicitly flagged historical (`is_past`) | Deterministic pipeline | `true` |
| `agenda_written` | boolean | No (defaults to `false`) | Not observed to be set by any current code path | — | `false` |
| `thread_id` | string \| null | No | The resolved thread (always set on the live creation path) | Deterministic pipeline | `"thread_18f2a9b1c3d4e5f6"` |

**Dedup rule:** an existing meeting in the same thread is reused when its resolved
`date` matches exactly.

### Relationships

- `person_ids` → `people.id`. `org_id` → `organizations.id`. `thread_id` → `threads.thread_id`.
- Referenced by: `emails.entities_referenced.meetings`, `calendar_actions.meeting_id`, `opportunities.meeting_ids`.
- Indexes: unique on `id`; non-unique on `thread_id`.

---

## `personal_items`

**Purpose:** Non-business personal to-dos found in email, with an independent
reminder mechanism.

**Dashboard visibility:** every field in this table is shown on the Personal Items
tab — an unfiltered raw dump.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `id` | string | Yes | `PSN-###` | System-generated | `"PSN-006"` |
| `type` | string | Yes | Free-text item type | Claude Desktop analysis / internal LLM call | `"reminder"` |
| `description` | string | Yes | Item description (also the dedup key, together with `sender_email`) | Claude Desktop analysis / internal LLM call | `"pick up dry cleaning"` |
| `date_or_deadline` | string (ISO datetime) \| null | No | Resolved deadline, if a date phrase was recognized | Deterministic pipeline | `"2026-03-06T00:00:00Z"` |
| `status` | string | No (defaults to `"open"`) | **Two real values in practice: `"open"` (default) and `"reminded"`** — set by a wholly separate CLI process, `app/reminders.py` (`ReminderScheduler`), once `date_or_deadline` has passed. Not part of the main pipeline or the dashboard | Deterministic pipeline (default); system-generated (`app/reminders.py`, once due) | `"open"` |
| `sender_email` | string \| null | No | Lowercased sender address — also the dedup-lookup key | Deterministic pipeline | `"ashok@databeat.io"` |

**Note:** "Triggering a reminder" means writing a structured log line — there is no
email/Slack/desktop-notification delivery channel anywhere in this codebase.

### Relationships

- No foreign-key-style reference to another collection (no `person_id`/`org_id`/`thread_id` field exists on this model).
- Index: unique on `id`.

---

## `processing_runs`

**Purpose:** One summary record per `run_pipeline()` invocation (the `process_email`/
batch-file code path) — operational bookkeeping, not read by the dashboard.

Backed by `app.processing.models.PipelineRunSummary`.

**Dashboard visibility:** none — Backend-only. Not read anywhere in `app/ui/`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `run_id` | string | Yes | `run_{uuid4 hex}` — the upsert key | System-generated | `"run_3f9a2b1c..."` |
| `started_at` | string (ISO datetime) | Yes | — | System-generated | `"2026-03-04T14:00:00Z"` |
| `completed_at` | string (ISO datetime) | Yes | — | System-generated | `"2026-03-04T14:05:00Z"` |
| `processed` | int | Yes | Total emails fetched this run | System-generated | `12` |
| `completed` | int | Yes | Count that reached `COMPLETED` | System-generated | `10` |
| `failed` | int | Yes | Count that reached `FAILED` | System-generated | `1` |
| `skipped` | int | Yes | Count already `COMPLETED` before this run (deduped) | System-generated | `1` |
| `results` | list[object `{message_id, final_stage, error}`] | No (defaults to `[]`) | Per-email outcome | System-generated | `[{"message_id": "...", "final_stage": "COMPLETED", "error": null}]` |

### Relationships

- `results[].message_id` → `emails.message_id`.
- Index: non-unique on `started_at`.

---

## `migration_runs`

**Purpose:** Checkpoint/resume state for the admin-only identity-migration and
duplicate-consolidation CLIs (`app/entity_migration.py`, `app/duplicate_consolidation.py`).
**Not written by, or relevant to, the live email pipeline or the dashboard at all.**

No single Pydantic model — different migration stages write different subsets of
fields onto the same document shape (a plain dict, upserted by `run_id`).

**Dashboard visibility:** none — Backend-only. Not read anywhere in `app/ui/`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `run_id` | string | Yes | `migrun_{uuid4 hex}` — the upsert key | System-generated | `"migrun_7a1b..."` |
| `stage` | string | Yes | Which migration stage this run is for (e.g. `"organizations"`, `"canonical-references"`, `"duplicate-consolidation"`) | System-generated | `"organizations"` |
| `started_at` | string (ISO datetime) | Yes | When this run began | System-generated | `"2026-03-04T14:00:00Z"` |
| `completed_at` | string (ISO datetime) \| null | No (set only once the run finishes) | When this run finished | System-generated | `"2026-03-04T14:02:00Z"` |
| `status` | string, values used: `"running"`, `"completed"`, `"completed_with_errors"`, `"failed"` | Yes | Current run state | System-generated | `"completed"` |
| `last_processed_id` | string \| null | No | Resume checkpoint for the `organizations` stage | System-generated | `"PER-402"` |
| `processed_count` | int | No (default `0`) | Records processed so far (`organizations` stage) | System-generated | `120` |
| `updated_count` | int | No (default `0`) | Records actually updated so far (`organizations` stage) | System-generated | `84` |
| `skipped_count` | int | No (default `0`) | Records skipped so far (`organizations` stage) | System-generated | `30` |
| `error_count` | int | No (default `0`) | Records that errored so far (`organizations` stage) | System-generated | `0` |
| `completed_phases` | list[string] | No | Resume checkpoint for the `canonical-references` stage's 3 sub-phases (`"threads"`, `"relationships"`, `"knowledge_items"`) | System-generated | `["threads", "relationships"]` |
| `updated_counts_by_collection` | object | No | Per-collection counters for `canonical-references`/`duplicate-consolidation` | System-generated | `{"commitments": 4}` |
| `plan_hash` | string \| null | No | Hash of the duplicate-consolidation plan being executed, to detect a resume against a changed plan | System-generated | `"a1b2c3..."` |
| `completed_mappings` | list[string] | No | `"{duplicate_person_id}->{canonical_person_id}"` pairs already executed (`duplicate-consolidation`) | System-generated | `["PER-401->PER-004"]` |
| `blocked_mappings` | list[object `{mapping, reason}`] | No | Mappings that hit a safety block (e.g. a calendar attendee conflict) and were never applied | System-generated | `[]` |
| `execution_log` | list[object] | No | Per-mapping audit trail: timestamp, collections/documents changed, verification result | System-generated | `[]` |
| `rollback_snapshot_checksum` | string \| null | No | SHA-256 checksum of the rollback snapshot's contents | System-generated | `"a1b2c3..."` |
| `rollback_snapshot_path` | string \| null | No | Local filesystem path where the rollback snapshot was written (the snapshot itself is not stored in MongoDB) | System-generated | `"rollback_2026-03-04.json"` |
| `last_error` | string \| null | No | Set only when `status == "failed"` | System-generated | `null` |

### Relationships

- No foreign-key-style reference; this is pure run bookkeeping.
- No index is created for this collection anywhere in this repository (not in `initialize_indexes`, not in the optional migration "indexes" stage).

---

## `ingested_files`

**Purpose:** Per-file tracking for the folder-watching ingestion scheduler
(`app/scheduler.py` + `app/providers/source/folder.py`) — a pure efficiency
optimization (skip re-reading an unchanged file), not a correctness mechanism (the
`emails` collection's own `message_id`+`COMPLETED` dedup already makes a full re-scan
safe).

No Pydantic model — a plain dict, upserted by `filename`.

**Dashboard visibility:** none — Backend-only. Not read anywhere in `app/ui/`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `filename` | string | Yes | The upsert key | System-generated | `"export_2026_03.json"` |
| `path` | string | Yes | Full path to the file at time of write | System-generated | `"data/inbox/export_2026_03.json"` |
| `fingerprint` | string | Yes | SHA-256 of the file's bytes — detects any content change under the same filename | System-generated | `"9f8e7d..."` |
| `size` | int | Yes | File size in bytes | System-generated | `4096` |
| `all_completed` | boolean | Yes | `true` once the most recent batch from this file had zero pipeline failures (or the file was fully malformed, so nothing more it could ever contribute); `false` if any message in the last batch failed — a file with `false` is retried on every poll | System-generated | `true` |
| `message_count` | int | Only present on the "fully malformed file" branch | How many messages the file contained (always `0` in that branch — every message was individually malformed) | System-generated | `0` |

### Relationships

- No foreign-key-style reference to another collection.
- Index: unique on `filename`.

---

## `entities` and `activities` — declared, never populated

Both `EntityRepository` (collection `entities`) and `ActivityRepository` (collection
`activities`) are declared in `app/database/repositories.py`, but **no other file in
this repository ever imports or instantiates either class.** Nothing writes to, reads
from, or documents a schema for either collection anywhere in the current codebase.
They are not used by the pipeline, the MCP tools, the dashboard, or any migration
script. If either collection contains documents in a live deployment, they were not
put there by any code path that exists today.

**Dashboard visibility:** none — neither collection is read by `app/ui/dashboard.py`.

---

## `counters`

**Purpose:** Backing store for sequential id generation (`PER-001`, `PRJ-002`, etc.) —
one document per id prefix.

**Dashboard visibility:** none — Backend-only. Not read anywhere in `app/ui/`.

| Field | Type | Required | Description | Populated By | Example |
|---|---|---|---|---|---|
| `_id` | string | Yes | The id prefix itself (e.g. `"PER-"`) — MongoDB's own `_id`, not a separate field | System-generated | `"PER-"` |
| `seq` | int | Yes | Current counter value, atomically incremented via `$inc` on every call (`app.entities.ids.next_id`) | System-generated | `412` |

### Relationships

- Purely internal bookkeeping; not referenced by any other collection's data, though its output (the `seq` value) forms the numeric suffix of every `PER-`/`ORG-`/`PRJ-`/`OPP-`/`CMT-`/`MTG-`/`PSN-`/`FUP-` id used throughout this database.
- No index beyond MongoDB's automatic `_id` index.

---

## Dashboard Tooltips

The Streamlit dashboard (`app/ui/dashboard.py`) surfaces a concise version of most
descriptions in this document directly in the UI: every raw-dump table's column
headers carry an info icon (hover/click for a tooltip), and the custom-rendered
Thread Explorer / Context Evolution / Knowledge / Reply Approval / Calendar Approval
tabs attach the same tooltips to their labeled fields. The tooltip strings live in
`app/ui/column_descriptions.py`, collection-keyed exactly like this document, and are
meant to be restated concisely from here, not a separate source of truth — this
document remains authoritative; update it first, then mirror any wording change into
`column_descriptions.py`.

## Collection Overview

| Collection | Purpose | Main Relationships |
|---|---|---|
| `emails` | Raw email + processing/classification state | `entities_referenced.*` → people/projects/commitments/follow_ups/meetings/personal_items/opportunities |
| `threads` | Resolved conversation groupings | `person_ids`/`org_ids` → people/organizations; referenced by nearly every other collection's `thread_id` |
| `context_snapshots` | Versioned per-thread deal/conversation context | `thread_id` → threads; `triggering_email_id` → emails |
| `knowledge_items` | Extracted facts with confidence + history | `thread_id` → threads; `person_id`/`org_id` → people/organizations |
| `reply_drafts` | Draft replies + approval workflow | `source_email_id` → emails; `person_id`/`org_id` → people/organizations |
| `calendar_actions` | Detected meetings + approval workflow | `thread_id` → threads; `meeting_id` → meetings; `person_id`/`org_id` → people/organizations |
| `people` | Canonical individuals | `org_id` → organizations; referenced by nearly every entity collection |
| `organizations` | Canonical companies (domain-keyed) | Referenced by people/projects/opportunities/commitments/meetings/etc. |
| `projects` | Named engagements/deals | `person_ids`/`org_id` → people/organizations; referenced by `opportunities.project_ids` |
| `opportunities` | Confirmed sales deals (CRM overlay on projects) | `project_ids` → projects (dedup key); `person_ids`/`org_id`/`meeting_ids`/`source_email_ids` → people/organizations/meetings/emails |
| `commitments` | Promises made or owed | `person_id`/`org_id`/`project_id`/`thread_id` → people/organizations/projects/threads |
| `follow_ups` | Chase items derived from commitments | `commitment_id` → commitments; `person_id`/`org_id` → people/organizations |
| `meetings` | Meetings mentioned or held | `person_ids`/`org_id`/`thread_id` → people/organizations/threads |
| `personal_items` | Non-business personal to-dos | None (no foreign-key fields) |
| `processing_runs` | Per-`run_pipeline()`-call summary | `results[].message_id` → emails |
| `migration_runs` | Admin migration/consolidation checkpoint state | None (pure bookkeeping) |
| `ingested_files` | Folder-scheduler per-file dedup tracking | None (pure bookkeeping) |
| `entities` | **Declared, never populated by any code path** | — |
| `activities` | **Declared, never populated by any code path** | — |
| `counters` | Sequential id generator backing store | Feeds the numeric suffix of every generated id |
