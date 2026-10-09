---
name: raw-dump-labeling
description: The one skill for the raw-dump-labeling MCP server -- dumps real Gmail emails into MongoDB's raw_emails_dump collection unanalyzed (Mode A), and separately classifies already-dumped emails with one of six labels (Mode B), keeping labels current as a thread's conversation changes. Use when asked to "dump emails", "ingest raw emails", "label the ingested data", "classify the raw dump", or "run raw-dump-labeling". This connector exposes exactly 5 tools -- never assume any other tool (e.g. an incremental-boundary tool) exists here.
---

# Raw Dump + Labeling (the only skill for this connector)

This connector exposes exactly 5 tools, nothing else: `ingest_raw_email_only`,
`get_next_unprocessed_raw_email`, `persist_raw_email_label`,
`mark_raw_email_label_failed`, `mark_gmail_label_synced`. Two independent
modes, picked from the request -- never mix them in one run.

| Request sounds like | Mode |
|---|---|
| "dump my last N emails", "ingest these emails for testing" | **A: Dump** |
| "label the ingested data", "classify the raw dump", "run raw-dump-labeling" with no dump request | **B: Label** |

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
   re-evaluation again -- no action needed from you here, they'll surface
   naturally the next time Mode B runs. Just note the count in your report.
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
- There is no boundary/incremental-sync tool on this connector -- Mode A
  always dumps exactly the messages the request named (a count, a date
  range, or specific messages). Never guess a boundary date.
- Calling `ingest_raw_email_only` again for an already-dumped message is
  safe (idempotent on the Gmail id) and never resets its classification
  state if Mode B has already labelled it.

Report in plain English: how many messages were dumped, any that failed
(by Gmail id), how many prior items were auto-closed (step 4a) and whether
their Gmail labels were successfully synced, and how many siblings became
eligible for re-evaluation (step 4b). Do not dump raw JSON/tool output.

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

### Procedure

Repeat up to 20 times, or until `get_next_unprocessed_raw_email` returns
`None`:

1. **Call `get_next_unprocessed_raw_email`.** Atomically claims and returns
   the oldest eligible raw-dumped email -- never-classified, classified
   under an older taxonomy version, whose prior claim lease expired, or just
   reopened by a new inbound message in its thread (Mode A step 4b).
   `None` -> stop, report "nothing left to label".
2. **Read the returned email's `subject`/`body`/`from`/`to`/`timestamp`,
   AND its `thread_context`** (every other raw-dumped email in the same
   thread, oldest first, if any) -- decide exactly one of the six labels
   based on the WHOLE conversation, not just this one message in isolation.
   An inbound "Thanks" after an earlier question-laden message means the
   loop closed; an inbound follow-up question keeps it (or a different one)
   open. If this email already carries a `label_applied` from a prior run
   (visible on the returned document, alongside `label_history`), re-evaluate
   fresh against the current thread_context; only change it if your own
   reading genuinely disagrees with what's there now.
3. **On a successful decision**, call **`persist_raw_email_label`** with
   that email's `message_id` and the chosen label.
   **If you genuinely cannot classify it** (unreadable/garbled body,
   content too ambiguous for any of the six -- different from
   `1. Undecided`, which IS a valid decision), call
   **`mark_raw_email_label_failed`** with a concise, specific reason
   instead, skip steps 3a-3b below for this message, and go back to step 1.
   Never call both `persist_raw_email_label` and
   `mark_raw_email_label_failed` for the same email in the same iteration.
3a. **Apply the matching Gmail label.** Once per run (not per message),
   call the Gmail connector's `list_labels`; for each of the six label
   names not already present, call `create_label` with that exact string
   as `displayName` (plain text, no color needed) and keep a
   name -> label id map in memory for the rest of this run. Then, for THIS
   message, call `label_message` with its `message_id` (the real Gmail id)
   and the label id matching the `label_applied` value you just persisted.
   If a different one of the six labels is already on this message from a
   prior run, remove that one first (`label_message` with the old label id
   removed, new one added) so a message never carries two of the six at
   once.
3b. **Confirm the sync.** If step 3a's Gmail call succeeded, call
   **`mark_gmail_label_synced`** with this message's `message_id`. If it
   failed, do NOT call it -- the document stays `gmail_label_synced: false`,
   which is the correct, honest state (MongoDB has the new label, Gmail
   doesn't yet); note the failure in your report so it can be retried.
4. **Confirm persistence** -- the `persist_raw_email_label` return value is
   the stored document; check it came back with no error.
5. Go back to step 1.

An email that fails 3 times stops being returned by
`get_next_unprocessed_raw_email` until a human reviews it directly -- never
work around this by forcing a guess through `persist_raw_email_label`. A
failed classification (`mark_raw_email_label_failed`) never gets a Gmail
label applied either -- step 3a only runs after a successful
`persist_raw_email_label`.

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
