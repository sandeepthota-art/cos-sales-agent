# Labeling walkthrough

How the 7 labeling tools actually behave, read straight from the code
as of this commit. No message bodies or real addresses below -- field
names, logic, and synthetic placeholders only.

## Call path, per tool

Every tool is a thin `app/server.py` wrapper that calls `_get_db()` (a
cached `Database` handle) and the matching `app/tools.py` function,
returning its result verbatim. `app/tools.py` functions call into
`RawEmailDumpRepository` (`app/db.py`) for every actual read/write.

| Tool | `app/server.py` | `app/tools.py` function | `app/db.py` method(s) it calls |
|---|---|---|---|
| `get_next_unprocessed_raw_email` | `:45` | `get_next_unprocessed_raw_email` | `claim_next_unclassified`, then `_attach_thread_context` calls `siblings_in_thread` |
| `get_next_unprocessed_raw_email_batch` | `:64` | `get_next_unprocessed_raw_email_batch` | `claim_batch_unclassified` (a loop of `claim_next_unclassified`), then `_attach_thread_context` per claimed doc |
| `persist_raw_email_label` | `:78` | `persist_raw_email_label` | `find_one`, `siblings_in_thread` (via `_thread_searchable_text`), `update_if_claim_matches` |
| `mark_raw_email_label_failed` | `:111` | `mark_raw_email_label_failed` | `find_one`, `update_if_claim_matches` |
| `mark_gmail_label_synced` | `:124` | `mark_gmail_label_synced` | `find_one` (via `_require_existing`), `mark_synced_if_label_unchanged` |
| `get_unsynced_labels` | `:143` | `get_unsynced_labels` | `find_many` |
| `get_stuck_emails` | `:160` | `get_stuck_emails` | `find_many` |

## The claim query

Both claim tools share one atomic operation, `claim_next_unclassified`
(`app/db.py:83`) -- `claim_batch_unclassified` (`app/db.py:136`) is a
plain Python loop calling it up to `batch_size` times, all claims
sharing one caller-generated `claim_id`. One `find_one_and_update` per
claim, so two concurrent callers (single or batch) can never be handed
the same document.

**Eligibility** (all four must hold):
- `classification_version < max_classification_version` (the constant
  `app/tools.py::_CURRENT_LABEL_CLASSIFICATION_VERSION = 1`) -- never
  classified, or classified under an older taxonomy version. Bumping
  that constant re-opens every document, by design.
- `label_attempts < max_attempts` (`_MAX_LABEL_ATTEMPTS = 3`) -- the
  circuit breaker.
- `label_claimed_at < claim_cutoff_iso` -- an unclaimed document's
  `label_claimed_at` is `""`, which sorts before any real ISO
  timestamp, so this single comparison covers both "never claimed" and
  "claim lease expired" with no `$or`.
- `history_only != True` -- permanently excluded, set by
  `scripts/mark_history_only.py`.

**Ordering**: `sort=[("sort_key", 1)]` -- oldest-eligible-first, no
declared tie-breaker field (ties fall to Mongo's own order).

**Lease length**: `_LABEL_CLAIM_TTL_MINUTES = 10` (`app/tools.py:25`) --
`claim_cutoff_iso` is computed as `now - 10 minutes` at the top of
`get_next_unprocessed_raw_email(_batch)`.

**`claim_id` semantics**: caller-generated (`uuid.uuid4().hex`), one
value shared across an entire batch. Written onto the document
alongside `label_claimed_at` at claim time. It's the fencing token
`persist_raw_email_label`/`mark_raw_email_label_failed` compare-and-swap
against via `update_if_claim_matches(..., claim_field="claim_id")` (the
default) -- a writer whose claim was invalidated (lease expired and
reclaimed, or cleared by a concurrent reopen/auto-close, see
`docs/INGESTION_WALKTHROUGH.md`) can never silently overwrite newer
work.

**Fields written by the claim itself**: `label_claimed_at=<now>`,
`claim_id=<the shared value>`. Nothing else.

`claim_next_unclassified` uses `return_document=ReturnDocument.BEFORE`
and manually patches `label_claimed_at`/`claim_id` onto the returned
dict -- a documented workaround for a confirmed mongomock 4.3.0 bug
(also reproduced against real Atlas): `find_one_and_update` with a
multi-condition query + sort + projection + an update that `$set`s the
SAME field one condition filters on fails to match under
`return_document=AFTER`. `BEFORE` sidesteps it with no change in actual
write behavior.

## Evidence verification

Required on every `persist_raw_email_label` call (`app/tools.py:408-411`
raises `ValueError` if evidence is empty/whitespace-only or over 300
characters -- a caller contract violation, checked before anything else
runs).

**What it searches** (`_thread_searchable_text`, `app/tools.py:358`):
this message's own `subject` + `body_clean` (computed fresh via
`strip_quotes_and_signature`, never the stored raw `body`), PLUS every
OTHER message in the same thread's `subject` + `body_clean`
(`siblings_in_thread`) -- joined with newlines into one string. If the
message has no `thread_id`, only its own subject/body_clean are
searched.

**Normalization** (`_normalize_for_match`, `app/tools.py:354`):
collapses all whitespace runs to a single space, strips leading/
trailing whitespace, lowercases. Applied to BOTH the evidence string
and the searchable text before the substring check -- so matching is
case- and whitespace-insensitive, not byte-exact.

**Length limit**: `_EVIDENCE_MAX_LENGTH = 300` characters
(`app/tools.py:14`) -- enforced as a hard raise, not a silent
truncation.

**Order**: the evidence-not-found check runs strictly BEFORE the
`claim_id` compare-and-swap (`app/tools.py:418-420` happens before the
call to `update_if_claim_matches` at `:439`) -- a bad quote never
consumes or risks a valid claim.

## `label_history` format

Two different shapes exist in this one array field, depending on
which code path appended the entry:

- **`persist_raw_email_label`** appends (`app/tools.py:423-428`):
  `{"label_applied": <str>, "labelled_at": <iso>, "classification_version": <int>, "evidence": <str>}`.
- **The auto-close path** (`_close_open_needs_reply_in_thread`,
  see `docs/INGESTION_WALKTHROUGH.md`) appends a DIFFERENT shape:
  `{"label_applied": "1. Read only", "labelled_at": <iso>, "classification_version": <int, unchanged>, "basis": <str naming the closing message_id>}` --
  note `"basis"` instead of `"evidence"`. A reader of `label_history`
  must not assume every entry has an `"evidence"` key.

Both are append-only -- `persist_raw_email_label` and the auto-close
path both read the existing list and append one new entry, never
replacing or dropping prior ones.

## `classification_version` handling

- Starts at `0` on first ingest (`app/tools.py:218`).
- Advanced to `_CURRENT_LABEL_CLASSIFICATION_VERSION` (currently `1`)
  by `persist_raw_email_label` on every successful persist
  (`app/tools.py:432`) -- this is what removes the document from future
  claim eligibility (its own re-evaluation trigger is a NEW inbound
  message resetting it back to `0`, not a version bump).
- Reset to `0` by `_reopen_thread_siblings_for_reevaluation` when an
  inbound message lands in the thread (see `docs/INGESTION_WALKTHROUGH.md`)
  -- the only other writer of this field.
- `mark_raw_email_label_failed` does NOT touch it at all -- a failed
  attempt is not a classification, so the document stays exactly as
  eligible/ineligible as it was before the attempt (modulo
  `label_attempts` incrementing toward the cap).
- Bumping the module constant `_CURRENT_LABEL_CLASSIFICATION_VERSION`
  itself re-opens every previously-classified document globally (the
  claim query's `classification_version < max_classification_version`
  condition) -- a deliberate "re-label everything" lever, not something
  any tool call triggers per-document.

## The compare-and-swap in `mark_gmail_label_synced`

`mark_synced_if_label_unchanged` (`app/db.py:212`) -- filters on
`{"message_id": message_id, "label_applied": expected_label_applied}`,
`$set`s `gmail_label_synced=True` only if that filter matches (i.e.
only if `label_applied` is STILL the exact value the caller says it
just applied to the real Gmail message). Uses
`return_document=ReturnDocument.AFTER` (not `BEFORE`) -- safe here
because the filtered field (`label_applied`) is different from the
field being set (`gmail_label_synced`), so this doesn't hit the
documented mongomock AFTER-bug the claim/CAS methods work around.
Returns `None` if `label_applied` no longer matches (a relabel raced
ahead) -- `app/tools.py::mark_gmail_label_synced` then falls back to
`repo.find_one` and returns the CURRENT document instead, with
`gmail_label_synced` correctly left `False`, never silently stamped
`True` against a stale Gmail value.

## Return shapes, including every `ok: false` reason

| Tool | Success shape | `ok: false` shapes |
|---|---|---|
| `get_next_unprocessed_raw_email` | the claimed document + `thread_context` + `body_clean`, or `None` if nothing eligible | n/a (no `ok` key at all -- `None` IS the "nothing eligible" signal) |
| `get_next_unprocessed_raw_email_batch` | `list` of claimed documents (each with `thread_context`/`body_clean`), possibly `[]` | n/a (`[]` is the "nothing eligible" signal) |
| `persist_raw_email_label` | the updated document | `{"ok": False, "reason": "evidence_not_found"}`; `{"ok": False, "reason": "claim_lost"}`. Raises `ValueError` (not a return value) if evidence is empty/too long, or if `message_id` doesn't exist at all. |
| `mark_raw_email_label_failed` | the updated document | `{"ok": False, "reason": "claim_lost"}`. Raises `ValueError` if `message_id` doesn't exist. |
| `mark_gmail_label_synced` | the updated (or current, if CAS didn't match) document | n/a -- never returns an `ok: false` shape; a CAS miss just returns the current document with `gmail_label_synced` left `False`. Raises `ValueError` if `message_id` doesn't exist. |
| `get_unsynced_labels` | `list` of documents, possibly `[]` | n/a |
| `get_stuck_emails` | `list` of documents, possibly `[]` | n/a |

## Edge cases, each paired with its test

| Case | Behavior | Test (file::name) |
|---|---|---|
| Nothing eligible at all | `None` / `[]` | `tests/test_core_engine_reliability.py::test_get_next_unprocessed_raw_email_returns_none_when_nothing_eligible`, `test_batch_claim_returns_empty_list_when_nothing_eligible` |
| Oldest-by-sort_key claimed first | Verified directly | `tests/test_core_engine_reliability.py::test_get_next_unprocessed_raw_email_claims_oldest_by_sort_key`, `test_batch_claim_claims_oldest_first_up_to_batch_size` |
| A claimed document is claimed again by a second call | Not returned twice | `tests/test_core_engine_reliability.py::test_claimed_email_is_not_returned_again_by_a_second_claim_call`, `test_batch_claim_does_not_return_already_claimed_docs` |
| `thread_context` shape | Excludes self, sorted oldest first, attached per document in a batch | `tests/test_core_engine_reliability.py::test_thread_context_excludes_self_and_is_sorted_oldest_first`, `test_batch_claim_attaches_thread_context_per_document` |
| Claim lease not yet expired | Not reclaimable | `tests/test_core_engine_reliability.py::test_abandoned_claim_is_not_immediately_reclaimable` |
| Claim lease expired | Reclaimable | `tests/test_core_engine_reliability.py::test_abandoned_claim_becomes_reclaimable_after_ttl_expires` |
| `mark_raw_email_label_failed` success | Increments `label_attempts`, releases claim | `tests/test_core_engine_reliability.py::test_mark_failed_increments_attempts_and_releases_claim` |
| Attempt cap reached | No longer claimable | `tests/test_core_engine_reliability.py::test_email_stops_being_claimable_after_max_attempts` |
| Successful persist | Resets `label_attempts` to 0; document no longer claimable | `tests/test_core_engine_reliability.py::test_persist_label_resets_the_attempts_counter`, `test_persist_label_makes_the_email_no_longer_claimable` |
| Stale-lease writer tries to persist after reclaim | `{"ok": false, "reason": "claim_lost"}`, no write applied | `tests/test_claim_fencing_and_chronology.py::test_stale_writer_persist_after_lease_expiry_is_rejected` |
| Unknown/wrong `claim_id` passed | `{"ok": false, "reason": "claim_lost"}` on both persist and mark-failed | `tests/test_claim_fencing_and_chronology.py::test_persist_rejects_an_unknown_claim_id`, `test_mark_failed_rejects_an_unknown_claim_id` |
| Batch documents share one `claim_id` | Verified directly | `tests/test_claim_fencing_and_chronology.py::test_batch_claimed_documents_carry_a_shared_claim_id` |
| Two concurrent batch claims | Never overlap | `tests/test_claim_fencing_and_chronology.py::test_batch_claim_never_reclaims_a_document_another_claim_already_holds` |
| Stuck-document visibility | `get_stuck_emails` surfaces docs at the cap, excludes docs below it | `tests/test_claim_fencing_and_chronology.py::test_get_stuck_emails_surfaces_documents_at_the_retry_cap`, `test_get_stuck_emails_excludes_documents_below_the_cap` |
| Exact quote from body, subject, or a thread sibling | Accepted | `tests/test_evidence_check.py::test_persist_accepts_an_exact_quote_from_the_body`, `test_persist_accepts_a_quote_from_the_subject`, `test_persist_accepts_a_quote_from_thread_context` |
| Quote differs only in case/whitespace | Accepted | `tests/test_evidence_check.py::test_persist_accepts_a_quote_case_and_whitespace_insensitively` |
| Quote not present anywhere | `{"ok": false, "reason": "evidence_not_found"}` | `tests/test_evidence_check.py::test_persist_rejects_a_quote_not_present_anywhere` |
| Quote only present in the stripped quoted-reply block (not in `body_clean`) | Rejected | `tests/test_evidence_check.py::test_persist_rejects_a_quote_only_present_in_the_quoted_reply_block` |
| Empty or over-300-char evidence | Raises `ValueError` | `tests/test_evidence_check.py::test_persist_raises_on_empty_evidence`, `test_persist_raises_on_evidence_over_300_chars` |
| `get_unsynced_labels` finds a document regardless of `classification_version` | Verified directly | `tests/test_unsynced_labels.py::test_unsynced_label_returned_despite_current_classification_version` |
| Already-synced documents excluded | Verified | `tests/test_unsynced_labels.py::test_already_synced_labels_are_excluded` |
| `limit` respected, ordering stable oldest-stuck-first with a tie-breaker | Verified | `tests/test_unsynced_labels.py::test_limit_is_respected`, `test_get_unsynced_labels_is_ordered_oldest_stuck_first_for_stable_pagination` |
| `get_unsynced_labels` never touches classification state | Verified | `tests/test_unsynced_labels.py::test_get_unsynced_labels_never_touches_classification_state` |
| Gmail retry fails, then succeeds | Document stays discoverable until confirmed, then disappears | `tests/test_unsynced_labels.py::test_failed_gmail_retry_leaves_message_discoverable`, `test_successful_retry_marks_synced_and_removes_from_results` |
| A relabel races ahead of a stale sync confirmation | CAS refuses to confirm, stays `false` | `tests/test_catch_up_scheduled_workflow.py::test_stale_sync_confirmation_does_not_clobber_a_newer_relabel` |
| Catch-up across multiple runs | No starvation, makes real progress | `tests/test_catch_up_scheduled_workflow.py::test_catch_up_drains_backlog_across_runs_without_starvation` |
| Same Gmail label applied twice | Idempotent | `tests/test_catch_up_scheduled_workflow.py::test_repeated_gmail_label_application_is_idempotent` |
| Persistent sync failure | Stays visible across many runs, never silently dropped | `tests/test_catch_up_scheduled_workflow.py::test_persistent_sync_failure_stays_visible_across_many_runs` |
| `history_only` documents | Permanently excluded from both single and batch claim, even after a reopen | `tests/test_mark_history_only.py::test_history_only_docs_excluded_from_single_claim`, `test_history_only_docs_excluded_from_batch_claim`, `test_history_only_doc_never_claimable_even_after_a_thread_reopen` |

### NOT covered by a passing test

No labeling-specific edge case was found uncovered during this review.
The one known gap in this whole call path is ingestion-side
(auto-close ignoring chronology), already documented in
`docs/INGESTION_WALKTHROUGH.md`'s own "NOT covered by a passing test"
section -- it affects what a labeling run sees (which siblings get
auto-closed), but the gap itself lives in `_close_open_needs_reply_in_thread`,
not in any of the 7 tools this doc covers.

## Skill text vs. code

Reviewed `skills/raw-dump-labeling/SKILL.md`'s full Mode B and sync
catch-up sections line by line against this walkthrough's findings --
claim ordering, lease length, evidence search scope/normalization,
`mark_gmail_label_synced`'s CAS semantics, `get_unsynced_labels`'
ordering/tie-breaker, and the attempt cap all match the code exactly.
**No new disagreement found in Mode B.** The one real disagreement in
this skill file was in Mode A's step 4b (claim-clear timing after a
thread reopen) and was already fixed in the previous commit -- see
`docs/INGESTION_WALKTHROUGH.md`.
