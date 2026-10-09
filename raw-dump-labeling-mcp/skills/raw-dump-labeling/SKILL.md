---
name: raw-dump-labeling
description: The one skill for the raw-dump-labeling MCP server -- dumps real Gmail emails into MongoDB's raw_emails_dump collection unanalyzed (Mode A, explicit list or scheduled sliding-window), and separately classifies already-dumped emails with one of six labels, in batches (Mode B), keeping labels current as a thread's conversation changes. Use when asked to "dump emails", "ingest raw emails", "run the scheduled dump", "label the ingested data", "classify the raw dump", or "run raw-dump-labeling". This connector exposes exactly 7 tools -- never assume any other tool (e.g. a stored-boundary/watermark tool) exists here.
---

# Raw Dump + Labeling (the only skill for this connector)

This connector exposes exactly 7 tools, nothing else: `ingest_raw_email_only`,
`get_next_unprocessed_raw_email`, `get_next_unprocessed_raw_email_batch`,
`persist_raw_email_label`, `mark_raw_email_label_failed`,
`mark_gmail_label_synced`, `get_unsynced_labels`. Two independent modes,
picked from the request -- never mix them in one run.

| Request sounds like | Mode |
|---|---|
| "dump my last N emails", "ingest these emails for testing" | **A: Dump (explicit)** |
| "run the scheduled dump", "scheduled task", no explicit count/date range given | **A: Dump (scheduled window)** |
| "label the ingested data", "classify the raw dump", "run raw-dump-labeling" with no dump request | **B: Label** |
| "retry Gmail sync", "fix unsynced labels", "catch up gmail_label_synced" | **Sync catch-up** (see end of Mode B) |

## Connector names -- check before running

- **Gmail connector** (Mode A only): the standard pre-built Gmail connector
  (`mcp__claude_ai_Gmail__*` or similar, depending on the account).
- **raw-dump-labeling MCP connector**: whatever this account named its
  connector to this server. If more than one similarly named connector is
  visible, confirm which one points at the intended MongoDB deployment
  before running, rather than guessing.

## Mode A: Dump (ingest only, zero analysis)

For each message you've been asked to dump:

1. **Read the email from Gmail** (via the Gmail connector's read tool).
2. **Map it to the expected shape**: Gmail id -> `message_id`, threadId ->
   `thread_id`, sender/recipients -> `from`/`to`/`cc` as `{name, email}`
   objects, date -> `timestamp` (ISO-8601), `in_reply_to`/`references` from
   the Message-ID headers if available. Do not alter the subject or body
   text in any way.
3. **Call `ingest_raw_email_only`** with that mapped object.
4. **Confirm persistence** -- the return value is the stored document;
   check it came back with no error.
4a. **Check `closed_in_thread` on the return value.** If this message is
   from the configured agent's own mailbox and landed in a thread that had
   an earlier "Needs reply*" item, that item was just deterministically
   closed to `1. Read only` in MongoDB -- `closed_in_thread` lists its
   message_id(s). For each one: update the REAL Gmail label to match (same
   list_labels/create_label map as Mode B's step 3a -- build it once per run
   if you haven't already), then call **`mark_gmail_label_synced`** with
   that message_id once the Gmail call actually succeeds. If the Gmail call
   fails, do NOT call `mark_gmail_label_synced` -- leave it unsynced and
   note the failure in your report; it'll still be correct in MongoDB and
   simply retryable later (re-run this step against the same message_id).
4b. **Check `reopened_in_thread` on the return value.** If this message is
   an inbound message (not from the agent) and landed in a thread with
   already-classified siblings, those siblings just became eligible for
   re-evaluation again, SUBJECT to the normal claim/lease rule -- if one of
   them happens to be mid-claim from a concurrent or still-running pass, it
   won't surface until that lease expires (same TTL as any other claim, see
   Mode B step 1). No action needed from you here either way; the claim
   tool is the source of truth for what's actually claimable right now.
   Just note the reopened count in your report.
5. **STOP.** Do not call `get_next_unprocessed_raw_email`,
   `persist_raw_email_label`, or `mark_raw_email_label_failed` for this
   message in the same breath -- labeling is a separate, later run (Mode B).

**Hard rules for Mode A:**
- Not a summary, not a classification, not a label, not an extracted
  entity, not a reply draft. `ingest_raw_email_only` is the only
  classification-adjacent decision made in this mode -- steps 4a/4b are
  both deterministic (structural `from`-address matches), never a judgment
  call Claude makes.
- Use the Gmail connector only to *read*, except for step 4a's label
  correction. Never send, reply, forward, trash, or mark spam.
- There is no stored boundary/watermark tool on this connector. This
  explicit sub-mode always dumps exactly the messages the request named (a
  count, a date range, or specific messages) -- never guess a boundary
  date here. The separate scheduled sub-mode below computes its own
  2-hour window fresh each run instead of storing one; it is the only
  place Mode A picks messages without an explicit list/range from the
  request.
- Calling `ingest_raw_email_only` again for an already-dumped message is
  safe (idempotent on the Gmail id) and never resets its classification
  state if Mode B has already labelled it.

Report in plain English: how many messages were dumped, any that failed
(by Gmail id), how many prior items were auto-closed (step 4a) and whether
their Gmail labels were successfully synced, and how many siblings became
eligible for re-evaluation (step 4b). Do not dump raw JSON/tool output.

## Mode A: Scheduled incremental dump (sliding window)

For an unattended/scheduled run with no explicit message list -- "run the
scheduled dump", a Scheduled Task firing on a recurring interval. Same
`ingest_raw_email_only` call as above per message (steps 2-4b all still
apply per message); this section only covers how to pick WHICH messages.

1. **Compute the window**: `window_start = now - 2 hours`, `window_end =
   now`. 2 hours, not 1, deliberately -- a 1-hour schedule interval plus a
   1-hour overlap margin, so a late-firing or skipped tick can't create a
   gap. Do not shrink this below the actual schedule interval.
2. **Search Gmail broader than the window, then filter precisely
   yourself.** Gmail's `after:` search operator is day-granularity, not
   hour-granularity -- `after:<date>` returns the whole day, not a 2-hour
   slice. Query `after:<window_start's date, minus 1 day to be safe>` via
   the Gmail connector, then for each result compare its actual timestamp
   against `[window_start, window_end)` yourself and skip anything outside
   it. Over-fetching here is harmless: `ingest_raw_email_only` is
   idempotent, so re-reading a message already inside the window from a
   prior run's overlap just no-ops on the dedup key.
3. **Cap at 100 messages ingested this run.** If the filtered set exceeds
   100, ingest the oldest 100 (by timestamp) and stop -- do not silently
   process the rest. Report this run as **truncated, needs catch-up**, not
   complete, and name the cutoff timestamp so a follow-up run (or a manual
   wider Mode A dump) can pick up from there.
4. **For each message in the filtered, capped set**: run Mode A steps
   2-4b above exactly as written (map fields, `ingest_raw_email_only`,
   check `closed_in_thread`/`reopened_in_thread`). Do not call
   `get_next_unprocessed_raw_email(_batch)` or any Mode B tool here.
5. **Report**: window used (`window_start`-`window_end`), how many Gmail
   messages the broader search returned, how many fell inside the precise
   window, how many were newly ingested vs. already present (dedup
   no-oped), any that failed, and whether the 100 cap was hit (truncated)
   or not (complete for this window).

**Safety:**
- Never widen the window on your own guess if a run appears to have been
  missed -- there is deliberately no stored boundary/watermark on this
  connector (same hard rule as explicit Mode A: never guess a boundary
  date). If you suspect a gap (e.g. the schedule clearly didn't fire for
  a while), say so in the report and ask for an explicit wider date range
  for a manual catch-up dump instead of silently guessing one.
- If Gmail's connector exposes a native, finer-grained time filter beyond
  `after:`/`before:`, prefer it over the day-granularity `after:` plus
  client-side filtering above -- this procedure is the safe fallback, not
  a mandate to ignore a more precise tool if one exists.

**Catch-up note:** a multi-day outage (the schedule didn't fire for
several days) needs a manual, explicit-range Mode A dump covering the
missed days -- the 2-hour sliding window is only correct for normal,
on-schedule operation. Flag this to the user rather than trying to infer
the missed range from Mongo state yourself.

## Mode B: Label (classification, now synced to the real Gmail label too)

Operates on `raw_emails_dump` via this connector's own tools, THEN applies
the matching label in the real Gmail mailbox via the Gmail connector. The
email's `message_id` on this connector is the real, uncanonicalized Gmail
id (set by Mode A, never reassigned) -- the same id `label_message` needs.

### The six labels

Exactly one per email, re-evaluated fresh each time (every value carries a
"1. " prefix):

- `1. Needs reply: ASAP` -- must respond, time-critical or a key relationship.
- `1. Needs reply` -- a question or request has a reasonably identifiable
  purpose, from the message itself or its available `thread_context`. A
  SHORT message is NOT automatically `Undecided` -- "Thoughts on the
  attached pricing proposal?" has a clear, identifiable purpose (feedback on
  that proposal) despite being one line, and is `Needs reply`.
- `1. Needs reply: mention` -- a thread being read asks something by name.
- `1. Read only` -- informational, nothing asked.
- `1. Delete` -- meeting accept/decline notices, cold outreach with no prior
  relationship.
- `1. Undecided` -- the message is too ambiguous to determine the
  appropriate action, even after checking `thread_context`. This means
  genuinely CONTEXTLESS, not merely short: a standalone "Thoughts?" with no
  accessible prior message or stated topic (nothing to know what's being
  asked about) is `Undecided` -- but the same word attached to a clear
  subject ("Thoughts on the attached pricing proposal?") is `Needs reply`,
  not `Undecided`, despite being equally short. Judge by whether the
  sender's intent is identifiable, never by message length alone.

### Procedure (batched -- 10 claimed at a time, up to 20 total)

**Outer loop**, up to 2 iterations (20 emails / 10 per batch), or until a
batch comes back empty:

1. **Call `get_next_unprocessed_raw_email_batch`** (default `batch_size=10`).
   Atomically claims and returns up to 10 eligible raw-dumped emails in ONE
   call -- never-classified, classified under an older taxonomy version,
   whose prior claim lease expired, or just reopened by a new inbound
   message in their thread (Mode A step 4b). Empty list `[]` -> stop,
   report "nothing left to label". This is the only step that batches --
   everything below still happens per email, one at a time.

**Inner loop**, for each email in the returned batch:

2. **Read that email's `subject`/`body`/`from`/`to`/`timestamp`, AND its
   `thread_context`** (every other raw-dumped email in the same thread,
   oldest first, if any) -- decide exactly one of the six labels based on
   the WHOLE conversation, not just this one message in isolation. An
   inbound "Thanks" after an earlier question-laden message means the loop
   closed; an inbound follow-up question keeps it (or a different one)
   open. If this email already carries a `label_applied` from a prior run
   (visible on the returned document, alongside `label_history`),
   re-evaluate fresh against the current thread_context; only change it if
   your own reading genuinely disagrees with what's there now.
3. **On a successful decision**, call **`persist_raw_email_label`** with
   that email's `message_id` and the chosen label.
   **If you genuinely cannot classify it** (unreadable/garbled body,
   content too ambiguous for any of the six -- different from
   `1. Undecided`, which IS a valid decision), call
   **`mark_raw_email_label_failed`** with a concise, specific reason
   instead, skip steps 3a-3b below for this email, and move to the next
   email in the batch.
   Never call both `persist_raw_email_label` and
   `mark_raw_email_label_failed` for the same email.
3a. **Apply the matching Gmail label.** Build the name -> label id map ONCE
   per run (not per batch, not per email): call the Gmail connector's
   `list_labels`; for each of the six label names not already present, call
   `create_label` with that exact string as `displayName` (plain text, no
   color needed). Then, for THIS email, call `label_message` with its
   `message_id` (the real Gmail id) and the label id matching the
   `label_applied` value you just persisted. If a different one of the six
   labels is already on this message from a prior run, remove that one
   first (`label_message` with the old label id removed, new one added) so
   a message never carries two of the six at once.
3b. **Confirm the sync.** If step 3a's Gmail call succeeded, call
   **`mark_gmail_label_synced`** with this email's `message_id`. If it
   failed, do NOT call it -- the document stays `gmail_label_synced: false`,
   which is the correct, honest state (MongoDB has the new label, Gmail
   doesn't yet); note the failure in your report so it can be retried.
4. **Confirm persistence** -- the `persist_raw_email_label` return value is
   the stored document; check it came back with no error.
5. Move to the next email in the batch. Once the batch is exhausted, go
   back to step 1 for the next batch.

An email that fails 3 times stops being returned by either claim tool
until a human reviews it directly -- never work around this by forcing a
guess through `persist_raw_email_label`. A failed classification
(`mark_raw_email_label_failed`) never gets a Gmail label applied either --
step 3a only runs after a successful `persist_raw_email_label`.

Use the single-claim `get_next_unprocessed_raw_email` instead of the batch
version only when the request is clearly about one specific email, or a
small handful -- batching is for the "dump 100, label them all" case.

**Hard rules for Mode B:**
- Never call Claude, OpenAI, or any other LLM API for classification -- you
  are the classifier, using your own reading of each email (and its
  `thread_context`).
- Never call `ingest_raw_email_only` in this mode -- that's Mode A's job.
- Relabeling an already-labelled email is expected, not a bug -- it's
  recorded in `label_history`, never silently lost -- and the real Gmail
  label must be updated to match (step 3a's remove-old-add-new), never left
  stale from a prior run.
- Mongo succeeding is never proof Gmail succeeded too -- always run step 3b
  as its own check, never assume it from step 3a alone.
- Use the Gmail connector only to read, create a label, and apply/remove a
  label on a message. Never send, reply, forward, trash, or mark spam.

Report in plain English when the loop ends: how many were newly labelled
vs. corrected (relabelled after a reopen) vs. failed this run, the label
breakdown (e.g. "3x Needs reply: ASAP, 5x Read only, ..."), any failures by
message id and reason, any Gmail-sync failures separately from MongoDB
successes (these are retryable -- note which message_ids), newly created
Gmail label names (if any), and how many remain eligible beyond the 20 cap,
if any. Do not dump raw JSON/tool output.

### Sync catch-up (closing a gap neither claim tool can see)

Once `persist_raw_email_label` succeeds, `classification_version` becomes
current -- so if the matching Gmail write then failed (step 3a/3b),
`get_next_unprocessed_raw_email(_batch)` will NEVER surface that message
again; its MongoDB label is already correct, only its real Gmail label is
stale. Run this whenever asked to retry/catch up on sync, or at the end of
a Mode B run that reported any Gmail-sync failures:

This budget is separate from, and does not count against, Mode B's 20-email
classification cap above -- that cap limits classification attempts only;
this one bounds Gmail re-write attempts on already-classified documents.

1. **Call `get_unsynced_labels`** (default `limit=10`). Returns every
   document with `gmail_label_synced: false` -- read-only, no claim/lease,
   never touches `classification_version` or `label_applied`. Ordered
   oldest-stuck-first (by when the label was persisted), with a stable
   tie-breaker, so repeated calls make real progress through a backlog
   instead of risking the same or an arbitrary subset each time.
2. **For each returned document**, re-run step 3a verbatim -- same label
   map (built once per run, shared with Mode B's 3a and Mode A's 4a: never
   build it twice, never remove anything outside the six taxonomy labels),
   apply `label_applied` (already decided, nothing to reclassify).
3. **On success**, call `mark_gmail_label_synced`. On failure, leave it
   and note it in the report -- still retryable later, same as Mode B's
   3a/3b.
4. Repeat from step 1 if the page was full (`limit` results), **up to 5
   pages (50 documents) per scheduled run.** If a 6th page would still be
   full, stop and report **truncated, more unsynced remain** -- rather than
   draining an unbounded backlog's worth of Gmail writes in one run.

Report how many were found, how many were successfully synced this run,
any still failing by message id, and whether the 5-page cap was hit.
