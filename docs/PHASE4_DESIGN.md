# Phase 4 design — context model

Built 2026-10-13, one pass, per the Phase 4 task spec. This doc records
the design as built, including every deliberate deviation from the
literal spec text. No network/Atlas/Gmail/LLM calls anywhere in this
phase; all tests use `mongomock` and synthetic data.

## Collections

| Collection | Key | Purpose |
|---|---|---|
| `observations` | append-only, unique on `(entity_type, entity_key, attribute, value_hash, source_message_id)` | Raw extracted facts, one per (entity, attribute, value, source message). Source of truth; profiles are rebuilt from this, never mutated directly. |
| `people` | `_id` = normalized email | Rebuilt profile view of a person: `display_names[]`, `is_role_address`, `profile{}`, `first_seen_at`/`last_seen_at`, `message_count`. |
| `organizations` | `_id` = domain (never free-mail) | Rebuilt profile view of an org: `names[]`, `profile{}`, `first_seen_at`/`last_seen_at`. |
| `person_org_links` | `_id` = `"{person_email}\|{org_domain}\|{valid_from}"` | Employment/affiliation periods. `valid_to` set only by explicit evidenced departure. |
| `threads` | `_id` = `thread_id` (or `message_id` if none) | `participants[]`, `status`, `summary`, `open_items[]`, `last_message_sort_key`, `message_count`, `needs_rebuild`, `context_version` (reprocess counter, independent of `raw_emails_dump.context_version`). |
| `threads_history` | `_id` = `thread_id`, one doc, `snapshots[]` capped at 20 via `$push`+`$slice` | Append-only audit trail of thread state over time. |
| `email_insights` | `_id` = `source_message_id` | `intent`, `sentiment`, `sender_type`, `summary`, `evidence` per message. |
| `raw_emails_dump` (extended) | existing | New fields: `context_version`, `context_claim_id`, `context_claimed_at`, `context_attempts`, `context_last_error`. Independent of `label_*`/`classification_version`/`claim_id`. |

## Tools (`app/server.py` + `app/context.py`)

| Tool | Does |
|---|---|
| `claim_next_thread_for_context()` | Atomic anchor-claim + per-document loop (mirrors `claim_batch_unclassified`'s shape) claiming one thread's unprocessed messages, up to 15, one shared `claim_id`. Returns `{"empty": true}` when nothing eligible. |
| `persist_context_delta(claim_id, thread_id, delta)` | Validates everything (structural via pydantic, then whitelist/evidence/cross-reference checks) before writing anything; applies observations → people/orgs/links/threads/insights → threads_history snapshot → releases the claim (CAS) last. |
| `mark_context_failed(claim_id, thread_id, reason)` | CAS release with attempt increment; terminal at 3 attempts. |
| `get_stuck_context(limit)` | Read-only, threads/messages at the retry cap. |

Scripts (never MCP tools): `scripts/rebuild_profiles.py` (dry-run default,
`--apply`), `scripts/reset_stuck_context.py` (dry-run default, `--apply`).

## Rules enforced by `persist_context_delta`

| Rule | Mechanism |
|---|---|
| Every message_id belongs to the claimed set | checked against `claim_id`'s claimed documents |
| Evidence must appear in the right text | entity-level evidence checked against its own `source_message_id`'s text; thread-level evidence (status/summary/open_items) checked against the UNION of all claimed messages |
| Attributes whitelisted | `app/context_config.py`'s `PERSON_ATTRIBUTE_WHITELIST`/`ORGANIZATION_ATTRIBUTE_WHITELIST` — sensitive categories (health, religion, politics, ethnicity, sexual orientation, finances/credentials) are never listed, so rejected by construction, not by a blocklist |
| Person emails never invented | must be syntactically valid AND appear in thread participants or the evidence text |
| Role addresses get no person attributes | local-part membership in `ROLE_LOCAL_PARTS` |
| Free-mail domains get no organization | membership in `FREE_MAIL_DOMAINS` |
| Org domain grounded | must match a participant domain or appear in evidence |
| Caps | value/evidence length, messages per claim, entities per delta, attempts, lease TTL — all in `app/context_config.py`, never hardcoded in `app/context.py` |
| All-or-nothing | every check collected into one `errors` list before any write; one bad item rejects the whole delta |
| Crash-safety | steps (a) observations, (b) people/org/link/thread/insight upserts, (c) threads_history snapshot are all idempotent; only step (d) (claim release) is the point of no return — a crash before it is safe to reprocess |

## Deviations from the literal spec

| Spec text | What was built instead | Why |
|---|---|---|
| `observations.entity_type (person\|organization\|thread)` | `ENTITY_TYPES = {"person", "organization"}` only; thread-level facts go through the structured `delta["thread"]` section (status/summary/open_items), not free-form `entity_type="thread"` observations | Avoids a third, undefined attribute whitelist for threads; keeps the thread-write path fully structured/type-checked instead of string-attribute/value pairs |
| `value_hash` for the unique index | `sha256(value)` truncated to 16 hex chars, not the full 64 | Shorter, still effectively collision-free for this cardinality, smaller index entries |
| No explicit keying scheme given for `person_org_links` | Deterministic `_id = "{person_email}\|{org_domain}\|{valid_from}"` | Makes re-persisting the identical link idempotent while still allowing multiple concurrent "current" links per person (spec explicitly allows this since domain change alone never closes a prior link) |
| `threads_history` described as append-only snapshots, keep last 20 | Implemented as one document per thread with a `snapshots[]` array, capped via Mongo's own `$push`+`$slice: -20` | Gets "keep last 20" atomically from Mongo itself; no separate trim/cleanup step needed |
| `people`/`organizations`' `first_seen_at`/`last_seen_at`/`message_count` | **Superseded by Phase 4B** (see below): originally derived purely from `observations`; now also merges in claimed-message participation | See Phase 4B section |
| `organizations.names[]` | **Superseded by Phase 4B** (see below): originally left empty/unpopulated | See Phase 4B section |
| `threads.message_count` | Count of ALL `raw_emails_dump` documents with that `thread_id` (total thread size), not "messages processed so far by context" | Simpler, stable definition; the processed-count is already visible via `context_version`/claim history on the raw documents themselves |
| Departure evidence for `valid_to` | Code enforces only that the evidence is grounded in the claimed message text (exact quote check) — it cannot semantically verify the quote actually states a departure | Matches the stated principle: Claude's judgment is the quality gate, code's job is structural safety (grounding), not semantic fact-checking |

## Phase 4B follow-up (2026-10-13)

Small code follow-ups, done first with tests before the rest of Phase 4B:

- **Participation-derived people/organizations.** `persist_context_delta`
  now touches EVERY participant (`from`/`to`/`cc`) of every claimed
  message, not just entities named in an observation or link — a person
  with zero extracted facts still gets/updates a `people` doc (role
  addresses flagged `is_role_address`, still no attributes). Every
  non-free-mail participant domain gets/updates an `organizations` doc
  the same way; free-mail domains still never do. `first_seen_at`/
  `last_seen_at`/`message_count` are a union-merge of the entity's
  `observations` AND this call's claimed-message participation against
  the document's own previously-stored `participant_message_ids` —
  re-persisting the same claimed messages is a no-op merge (set union),
  so `message_count` cannot be double-counted by a reprocess.
- **`organizations.names[]` now populated.** Added `"name"` to
  `ORGANIZATION_ATTRIBUTE_WHITELIST`; `name` observations accumulate into
  `names[]` (deduplicated, oldest-first, capped at
  `ORGANIZATION_NAMES_MAX_COUNT = 5`) instead of overwriting a single
  profile field — a company can legitimately go by more than one name
  across emails. `name` is excluded from the generic `profile{}` dict
  (that's for single-valued attributes only).

### Golden-case note for Phase 5

Departure statements (what makes `person_org_links.valid_to` get set) are
judgment calls this phase can only validate structurally (evidence must
be grounded in the message text) — it cannot semantically verify the
quote actually states a departure. **Departure statements must be
covered in Phase 5 golden cases** so extraction quality on this specific
judgment is measured, not just assumed.

## Deferred (not in this phase)

| Item | Status |
|---|---|
| Weekly consolidation pass | Not built — profiles rebuild correctly on every `persist_context_delta` call and via `scripts/rebuild_profiles.py`; a separate scheduled consolidation pass was judged unnecessary for correctness, only for cost/performance at scale |
| Relabel-on-context-arrival / `label_basis` | Not built — labeling and context building stay fully independent per this phase's own principle; wiring context facts back into label decisions is a labeling-side change, out of scope here |
| Run log | Not built — no persistent log of scheduled-task runs exists for ingestion/labeling either (pre-existing gap, not introduced by this phase) |
| Sender-type gate in labeling | Not built — `email_insights.sender_type` exists now but labeling's `app/tools.py` was explicitly not changed by this phase ("do not change labeling or ingestion behavior") |

## `.env.example`

No new entries. `app/context_config.py` is a plain module (free-mail
list, role local-parts, whitelists, caps, enums) — nothing in it is a
secret or a per-environment value, so it was deliberately built as
constants rather than a `Settings`/env-var-backed config. There is
nothing new to add to `.env.example` for this phase.
