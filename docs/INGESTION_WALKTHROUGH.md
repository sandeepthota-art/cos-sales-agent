# Ingestion walkthrough

How `ingest_raw_email_only` actually behaves, read straight from the code
as of this commit. No message bodies or real addresses below -- field
names, logic, and synthetic placeholders only.

## Call path

| # | Function | File | What it does |
|---|---|---|---|
| 1 | `ingest_raw_email_only` (MCP tool) | `app/server.py:23` | Thin wrapper. Takes a validated `Email` (FastMCP already ran pydantic validation on the tool's `email: Email` parameter). Calls `tools.ingest_raw_email_only(_get_db(), email, get_settings())` and returns its result verbatim. |
| 2 | `ingest_raw_email_only` | `app/tools.py:175` | The actual logic -- everything below this line. |
| 3 | `email.model_dump(mode="json", by_alias=True)` | pydantic, called from `app/tools.py:210` | Turns the validated `Email` object into a plain JSON-safe dict (`by_alias=True` so `from_` is dumped back out as `"from"`). |
| 4 | `RawEmailDumpRepository.find_one` | `app/db.py` (via `_BaseRepository.find_one`) | Checked once at `app/tools.py:215` to decide `is_new` (does a document with this `source_message_id` already exist). |
| 5 | `build_thread_candidates` | `app/threading.py:54` | Only called when `is_new` AND `thread_id` is still falsy after the dump (`app/tools.py:242-243`). Groups every existing `raw_emails_dump` document by `thread_id` into one `ThreadCandidate` per thread. |
| 6 | `resolve_thread_id_fallback` | `app/threading.py:88` | Tries to match this email against the candidates (see "thread_id resolution" below). Returns an existing `thread_id` to reuse, or `None`. |
| 7 | `RawEmailDumpRepository.upsert_by_key` | `app/db.py` (`_BaseRepository.upsert_by_key`) | `app/tools.py:253`. The actual write: `update_one({"source_message_id": ...}, {"$set": document}, upsert=True)`, then reads the result back with `_id` projected out. |
| 8 | `_clear_active_claims_in_thread` | `app/tools.py:141` | Called unconditionally whenever `thread_id` is truthy (`app/tools.py:261`), before anything else. |
| 9 | `_flag_rebuild_if_out_of_order` | `app/tools.py:159` | Called next, also unconditionally when `thread_id` is truthy (`app/tools.py:262`). Reads `ThreadRepository.find` (`app/context_db.py`) and, if needed, writes `ThreadRepository.upsert` plus `RawEmailDumpRepository.update_many_by_key`. |
| 10 | `_close_open_needs_reply_in_thread` (`app/tools.py:64`) OR `_reopen_thread_siblings_for_reevaluation` (`app/tools.py:113`) | `app/tools.py:263-266` | Exactly one of these two runs, decided by whether the sender is `settings.agent_email`. |

## Exact fields stored

Every call writes these fields (from the `Email` model dump, `by_alias=True`):
`message_id`, `source_message_id`, `thread_id`, `source_thread_id`,
`from`, `to`, `cc`, `subject`, `body`, `timestamp`, `in_reply_to`,
`references`, `attachments`, `labels` -- plus, added by
`ingest_raw_email_only` itself: `source` (always the literal string
`"gmail"`), `ingested_at` (now, ISO-8601 UTC), `sort_key` (see below).

**Only on first creation** (`is_new`), these are ALSO set, once, and
never touched again by a later re-ingest of the same message:
`classification_version=0`, `label_applied=None`, `labelled_at=None`,
`label_history=[]`, `label_claimed_at=""`, `claim_id=None`,
`label_attempts=0`, `label_error=None`, `gmail_label_synced=True`,
`context_version=0`, `context_claim_id=None`, `context_claimed_at=""`,
`context_attempts=0`, `context_last_error=None`.

On a **re-ingest** of an already-dumped message (`is_new` is `False`),
`thread_id`/`source_thread_id` are explicitly popped from the dict
before the upsert (`app/tools.py:251-252`) so the `$set` never touches
them -- every field above this paragraph IS re-written on every
re-ingest (subject/body/from/to/cc/etc. all refresh to whatever was
just read from Gmail), but `thread_id` and every classification/context
field are left exactly as they were.

## Dedup key and unique index

Dedup key: `source_message_id` (the Gmail message id, never
re-assigned). Enforced by `db.raw_emails_dump.create_index("source_message_id", unique=True)`
(`app/db.py::initialize_indexes`). The upsert itself
(`update_one({"source_message_id": ...}, ..., upsert=True)`) is what
makes re-ingesting the same Gmail message idempotent at the document
level -- the unique index is the backstop guaranteeing Mongo itself
would refuse a second document with the same key even if the upsert
logic were ever bypassed.

## thread_id resolution

1. If Gmail supplied a `thread_id` on the `Email` object, it's used
   as-is -- no fallback logic runs at all.
2. If `thread_id` is falsy AND this is a brand-new document
   (`is_new`), the fallback matcher runs, in this exact order inside
   `resolve_thread_id_fallback` (`app/threading.py:88`):
   a. **`in_reply_to`** -- if set, match any existing candidate whose
      `source_message_ids` contains it.
   b. **`references`** -- if set, match any candidate whose
      `source_message_ids` intersects it.
   c. **Subject + participants** -- normalized subject (strips
      leading `Re:`/`Fwd:`/`Fw:` prefixes, repeatedly, then collapses
      whitespace) must match a candidate's normalized subject AND at
      least one participant email must overlap.
   d. **Participants within a 14-day window** (`window_days=14`,
      the only tunable) -- any candidate sharing a participant whose
      `last_message_at` is within 14 days of this email's timestamp;
      ties broken by picking the most recently active candidate.
   First matching rule wins; later rules are never tried once one
   fires. No match at any step returns `None` -- a `thread_id` is
   **never synthesized**, only ever reused from an existing document.
3. **Freeze rule**: once `thread_id`/`source_thread_id` are set at
   creation time (from Gmail directly, or via step 2's fallback), they
   are frozen forever. A later re-ingest of the *same* `source_message_id`
   can never recompute or overwrite them (`app/tools.py:251-252`'s
   explicit `pop`) -- recomputing on every call could drift the match
   as more documents accumulate, so the first decision is permanent.

Candidates themselves (`build_thread_candidates`, `app/threading.py:54`)
are built fresh on every call by scanning the whole `raw_emails_dump`
collection and grouping by `thread_id` -- a documented O(n) scan,
acceptable at this project's current scale, called out in that
function's own docstring as the thing to optimize first if ingestion
volume ever makes it slow.

## sort_key

`email.timestamp.astimezone(timezone.utc).isoformat()` -- computed
and written exactly once, at ingest time (`app/tools.py:213`), from
whatever offset-aware datetime the `Email` model parsed. This
normalization is why two emails with different UTC offsets still sort
into correct chronological order natively in MongoDB -- sorting on the
raw, un-normalized timestamp string would NOT be chronologically
correct across differing offsets. `sort_key` drives every ordering
claim query in this project (`claim_next_unclassified`,
`claim_next_context_candidate`, `siblings_in_thread`, etc.) -- it is
never recomputed after creation (same freeze reasoning as `thread_id`,
though there's no explicit pop for it -- a re-ingest's `$set` simply
writes the same value again since the underlying Gmail timestamp for a
given `source_message_id` doesn't change).

## `closed_in_thread` / `reopened_in_thread`

Exactly one of these two runs per ingest call, and only when
`thread_id` is truthy on the just-upserted document:

### `_close_open_needs_reply_in_thread` (`app/tools.py:64`)

**Condition**: the ingested email's `from.email` (lowercased,
stripped) equals `settings.agent_email` (also lowercased/stripped) AND
`agent_email` is non-empty.

**What changes**: every OTHER document in the same `thread_id` whose
current `label_applied` is one of `_NEEDS_REPLY_LABELS` (`"1. Needs
reply: ASAP"`, `"1. Needs reply"`, `"1. Needs reply: mention"`).

**Fields set on each matched sibling**: `label_applied="1. Read only"`,
`labelled_at=<now>`, `label_history` gets ONE entry appended (never
overwrites prior history) containing `label_applied`, `labelled_at`,
the sibling's own (unchanged) `classification_version`, and a `basis`
string naming the closing message's id. Also `gmail_label_synced=False`
(the real Gmail label hasn't been touched by this MongoDB-only write)
and the claim fields are cleared via `_cleared_claim_fields()`
(`label_claimed_at=""`, `claim_id=None`).

**What does NOT change**: `classification_version` is left exactly as
it was -- this is a correction to an existing classification, not a
fresh re-evaluation, so it deliberately does NOT make the document
eligible for `get_next_unprocessed_raw_email`'s claim query again on
this account alone.

### `_reopen_thread_siblings_for_reevaluation` (`app/tools.py:113`)

**Condition**: the ingested email's sender is NOT `agent_email` (an
inbound message), AND `thread_id` is truthy.

**What changes**: every OTHER document in the same `thread_id` whose
`classification_version >= 1` (i.e., every already-classified sibling,
regardless of which of the six labels it currently holds -- not
limited to `_NEEDS_REPLY_LABELS` the way the close path is).

**Fields set**: `classification_version=0` plus `_cleared_claim_fields()`
(`label_claimed_at=""`, `claim_id=None`) via one `update_many_by_key`
call. `label_applied` itself is left untouched -- Claude decides the
new label the next time the document is claimed, same as any other
eligible document.

## Claim clearing on siblings

`_clear_active_claims_in_thread` (`app/tools.py:141`) runs
**unconditionally** on every threaded ingest, BEFORE the close/reopen
branch -- it is strictly broader than either of the two functions
above: it clears `label_claimed_at`/`claim_id` on every OTHER sibling
in the thread that currently has `claim_id != None`, regardless of
that sibling's `label_applied` or `classification_version`. This exists
specifically to cover a sibling claimed for its very first classification
(`classification_version=0`, `label_applied=None`) -- such a sibling
matches neither the close path's label filter nor the reopen path's
`classification_version >= 1` filter, so without this unconditional
clear, nothing would have invalidated its claim even though the new
message just made its `thread_context` snapshot stale.

Net effect: a mid-claim sibling's claim is invalidated **immediately**,
in the same ingest call that introduced the new message -- not after
its lease TTL expires. It becomes claimable again on the very next
`get_next_unprocessed_raw_email(_batch)` call.

## `needs_rebuild` handling

`_flag_rebuild_if_out_of_order` (`app/tools.py:159`) also runs
unconditionally on every threaded ingest (right after the claim-clear,
before close/reopen). It reads the existing `threads` document for this
`thread_id` (`ThreadRepository.find`, `app/context_db.py`) -- if none
exists yet, it's a no-op (nothing to compare against). If one exists
AND the newly-ingested message's `sort_key` is **earlier** than that
thread document's `last_message_sort_key`, it:
1. Sets `threads.needs_rebuild = True` for that thread (`ThreadRepository.upsert`).
2. Resets `context_version = 0` on **every** `raw_emails_dump` document
   in that thread (`RawEmailDumpRepository.update_many_by_key`, matched
   on `thread_id` alone -- the whole thread, not just the out-of-order
   message), so the context-building pipeline reprocesses the whole
   thread from scratch.

This never touches any label field -- `context_version` is completely
independent of `classification_version`. Safe to reprocess because the
context layer's own `observations` collection dedupes via its unique
index (documented in `docs/PHASE4_DESIGN.md`), so redoing the context
extraction for messages already processed is a no-op, not a duplicate.

## Shape of the return value

The upserted document (every field listed under "Exact fields stored,"
with Mongo's own `_id` projected out), plus two keys ALWAYS added by
`ingest_raw_email_only` itself, even when both lists are empty:
- `closed_in_thread`: `list[str]` of message_ids closed by the agent-reply
  path (`[]` if that path didn't run, or ran but matched nothing).
- `reopened_in_thread`: `list[str]` of message_ids reopened by the
  inbound-reply path (same `[]` semantics).

Exactly one of the two lists can be non-empty per call (they come from
mutually exclusive branches); both are always present as keys,
never omitted.

## Errors and edge cases, each paired with its test

| Case | Behavior | Test (file::name) |
|---|---|---|
| Same `source_message_id` ingested twice | Upsert, not a duplicate; classification/context progress untouched | `tests/test_core_engine_reliability.py::test_ingest_is_idempotent_and_never_resets_existing_classification_progress` |
| `thread_id` missing, fallback resolves via `in_reply_to` | Reuses matched candidate's `thread_id` | `tests/test_thread_resolution_fallback.py::test_resolves_by_in_reply_to` |
| `thread_id` missing, `in_reply_to` doesn't match, `references` does | Reuses matched candidate's `thread_id` | `tests/test_thread_resolution_fallback.py::test_resolves_by_references_when_in_reply_to_misses` |
| `thread_id` missing, resolved by subject+participants | Reuses matched candidate's `thread_id` | `tests/test_thread_resolution_fallback.py::test_resolves_by_subject_and_participants_when_no_reply_headers` |
| `thread_id` missing, resolved only by participants within the 14-day window | Reuses the most-recently-active matching candidate | `tests/test_thread_resolution_fallback.py::test_resolves_by_participants_within_window_as_last_resort` |
| Participant overlap exists but candidate is outside the 14-day window | No match | `tests/test_thread_resolution_fallback.py::test_outside_window_does_not_match` |
| No signal matches at all | Returns `None`, never synthesizes an id | `tests/test_thread_resolution_fallback.py::test_no_match_returns_none_never_synthesizes` |
| End-to-end: ingest with no `thread_id`, fallback resolves it | `thread_id`/`source_thread_id` both set from the match | `tests/test_thread_resolution_fallback.py::test_ingest_resolves_missing_thread_id_via_fallback` |
| End-to-end: ingest with no `thread_id`, nothing matches | `thread_id` stays `None`, never synthesized | `tests/test_thread_resolution_fallback.py::test_ingest_leaves_thread_id_none_when_nothing_matches` |
| Re-ingest of a message whose `thread_id` was already resolved | `thread_id` is never recomputed/overwritten | `tests/test_thread_resolution_fallback.py::test_reingest_never_recomputes_or_overwrites_a_resolved_thread_id` |
| Differing UTC offsets across two messages | `sort_key` normalization gives correct chronological claim order | `tests/test_core_engine_reliability.py::test_sort_key_normalizes_differing_utc_offsets_to_correct_chronological_order` |
| Agent's own reply lands in a thread with an open "Needs reply*" sibling | Sibling closed to "1. Read only", `classification_version` untouched | `tests/test_core_engine_reliability.py::test_agent_reply_closes_open_needs_reply_siblings_to_read_only`, `test_closed_sibling_classification_version_is_untouched_so_it_is_not_reclaimed` |
| `AGENT_EMAIL` unset/empty | Every threaded ingest takes the reopen path, auto-close never fires, even for what would be the agent's own address | `tests/test_core_engine_reliability.py::test_empty_agent_email_takes_reopen_path_never_auto_closes` |
| Inbound reply lands in a thread with already-classified siblings | Siblings' `classification_version` reset to 0 regardless of label | `tests/test_core_engine_reliability.py::test_inbound_reply_reopens_already_classified_siblings_for_reevaluation`, `test_reopened_sibling_becomes_claimable_again` |
| Close/reopen must never touch documents outside the thread | Verified directly | `tests/test_core_engine_reliability.py::test_close_and_reopen_never_touch_emails_outside_the_thread` |
| No `thread_id` at all | Close/reopen logic skipped entirely, both lists `[]` | `tests/test_core_engine_reliability.py::test_no_thread_id_skips_close_and_reopen_logic` |
| A new thread message lands while a sibling is actively claimed (any label/version, including a sibling's first-ever claim) | That sibling's claim is cleared unconditionally and immediately | `tests/test_claim_fencing_and_chronology.py::test_new_thread_message_clears_the_claim_of_any_actively_claimed_sibling`, `test_auto_close_clears_the_claim_of_a_concurrently_claimed_sibling` |
| A new message's `sort_key` is earlier than its thread's `last_message_sort_key` | `threads.needs_rebuild=True`, every message in the thread reset to `context_version=0` | `tests/test_context_model.py::test_out_of_order_ingestion_triggers_needs_rebuild_and_reprocessing` |
| `history_only` document reopened by a thread-sibling ingest | Stays permanently excluded from classification claims regardless | `tests/test_mark_history_only.py::test_history_only_doc_never_claimable_even_after_a_thread_reopen` |

### NOT covered by a passing test

**Chronology**: the auto-close path (`_close_open_needs_reply_in_thread`)
closes every matching sibling in the thread with no regard for
`sort_key` order -- it will close a "Needs reply*" sibling that was
sent chronologically *after* the agent's closing reply, which is
arguably wrong (that later sibling may be asking something new, not
the thing being closed). This gap is explicitly tracked, not silently
missing: `tests/test_claim_fencing_and_chronology.py::test_auto_close_never_closes_a_sibling_sent_after_the_agent_reply`
exists and is marked `@pytest.mark.xfail(reason="deferred")` -- it
documents the expected-but-not-yet-implemented behavior rather than
asserting the current (wrong) one. See that file's own module
docstring ("Phase 2... chronology... DEFERRED").

**`_flag_rebuild_if_out_of_order` when no `threads` document exists
yet for that thread at all** (the early-return no-op branch) has no
test asserting it explicitly by name -- it's exercised implicitly by
every other threaded-ingest test that never pre-creates a `threads`
document (which is all of them except the one `needs_rebuild` test
itself), but there's no test whose entire point is "this is a safe
no-op when `threads.find` returns `None`."

## Skill text vs. code: one real disagreement, now fixed

`skills/raw-dump-labeling/SKILL.md`'s step 4b said:

> those siblings just became eligible for re-evaluation again, SUBJECT
> to the normal claim/lease rule -- if one of them happens to be
> mid-claim from a concurrent or still-running pass, it won't surface
> until that lease expires (same TTL as any other claim...)

This is wrong against the actual code. `_clear_active_claims_in_thread`
(`app/tools.py:141`) runs unconditionally on every threaded ingest and
clears a mid-claim sibling's `claim_id`/`label_claimed_at` **immediately**,
in the same call -- it does not wait for the lease TTL to expire. A
mid-claim sibling becomes claimable on the very next claim call, not
after a TTL wait. Fixed in the skill file (same commit as this doc) to
say so.
