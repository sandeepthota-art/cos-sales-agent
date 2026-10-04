---
name: process-thread
description: Use when the user selects a specific THR-* thread and asks to process all its unprocessed messages (e.g. "process this thread", "process all unprocessed messages in THR-001") -- processes every unprocessed message in that thread, oldest to newest, via the cos-sales-agent granular tools, never process_email. Distinct from process-one-email (exactly one named message) and cos-new-email-check (inbox-wide "what's new" check).
---

# Process Thread (granular pipeline, one thread at a time)

Trigger phrases: "process this thread", "process all unprocessed messages in
THR-001", "catch up this thread", or any request that names or clearly
identifies exactly one specific thread (by `THR-*` id, or by enough context
to resolve to one) to process in full.

This skill processes every *unprocessed* message in one thread, oldest to
newest, using the same granular tools and the same reasoning-in-conversation
approach as `process-one-email` and `cos-new-email-check` -- it does not
introduce a new pipeline, and it never calls `process_email`. It is a
separate skill: it does not replace or modify `process-one-email` (which
still handles exactly one named message), and it is not `cos-new-email-check`
(which sweeps the inbox for what's new, not a specific thread).

## Hard rules

- Only ever use the `cos-sales-agent` connector's tools
  (`mcp__remote-devices__cos-sales-agent__*`). No other MCP connector is used
  for this skill's CoS processing job.
- Use Gmail to *read* the thread's messages; to *label* a message (step
  5's one-time setup, then step 6.3 per message, via `create_label`/
  `label_message`); and — only when a reply is warranted per step 6.4
  below — to *create a draft* via `create_draft`. Never actually send,
  reply (dispatch), forward, delete, trash, or mark spam. Labeling and
  draft-creation are the only write actions ever permitted here, and a
  draft must never be sent automatically by this skill.
- **Never call `process_email`.** Always use the granular tools, in this
  exact order, per message: `ingest_email` -> (you read and classify the
  message) -> `persist_email_analysis` -> `persist_context_delta` ->
  (`create_reply_draft` if warranted, or `set_reply_withheld_reason` if a
  reply is needed but deliberately not drafted) -> `mark_email_completed`.
- Process exactly one thread per run. If the user's request could match more
  than one thread, ask which one they mean rather than guessing.
- Within that thread, process only messages that are not already
  `COMPLETED` -- never re-run an already-completed message.
- **Process strictly oldest to newest.** Never process messages out of
  order -- context versions and thread history both depend on chronological
  order.
- **Stop immediately on the first failure.** Do not continue to the next
  message after one fails -- report the failed message clearly (its Gmail
  id, timestamp, and the error) and stop. A failed message is always safely
  retryable later (idempotent -- confirmed directly against EML-002's own
  history), so stopping costs nothing; silently continuing risks processing
  a later message against an incomplete or wrong context.
- Never invent people, projects, commitments, follow-ups, meetings, or reply
  content that isn't genuinely present in a message.
- Never call Claude, OpenAI, or any other LLM API for classification -- you
  are the classifier, using your own reading of each email. Do not expose
  your reasoning/chain-of-thought in any tool call or stored data -- only
  the resulting structured fields.
- Never create a real calendar event. `persist_email_analysis` only ever
  proposes a `calendar_action` in MongoDB (`awaiting_approval` /
  `needs_clarification`) -- never a real event.
- **Before creating any Project or Opportunity, check for an existing one
  with the same name/entity first** (e.g. via `get_project_summary` /
  `list_projects` / `list_opportunities`). Reuse it rather than creating a
  new one just because one message's own `MentionedProject` didn't repeat
  the `org`/entity value an earlier message in the same thread already
  supplied.

## Procedure

1. **Resolve the thread.** The user may give a `THR-*` id directly, or
   describe it (subject/sender/context) well enough to resolve to exactly
   one. If ambiguous, ask which thread they mean -- never guess.
2. **Get every message in the thread** (`get_thread`). Never treat the
   thread's own `id` field as a message id -- Gmail sets a thread's id equal
   to the id of the message that started it, not necessarily the one that
   still needs processing.
3. **Determine which messages are already processed**, using this
   connector's read-only lookups (`search_emails` / `list_processed_emails`),
   matching by sender, subject, and timestamp -- a message is unprocessed if
   no matching record exists or its status isn't `COMPLETED`.
4. **Sort the unprocessed messages strictly oldest to newest** by timestamp.
5. **Set up the six Gmail labels once, before the per-message loop.** Call
   the Gmail connector's `list_labels`. For each of the six label names --
   `Needs reply: ASAP`, `Needs reply`, `Needs reply: mention`, `Read only`,
   `Delete`, `Undecided` -- not already present, call `create_label` with
   that exact string as `displayName` (plain text, no color needed). Keep
   a name -> label id map in memory for the rest of this run.
6. **For each unprocessed message, in that order:**
   1. Call `ingest_email`.
      - If it unexpectedly returns `already_completed: true` (e.g. a race
        with something else that just processed it), skip this one,
        record it under "skipped" in the final report, and continue to the
        next message -- this is not a failure.
      - Read `previous_context`/`thread_timeline` before reasoning about it.
   2. Read the message yourself and classify Sales vs Not Sales, same
      rubric as `process-one-email`/`cos-new-email-check`:
      - Sales only for genuine activity tied to a specific prospect/
        customer: active negotiation, pricing/quote, proposal, contract
        discussion, pilot/POC, renewal, expansion, purchase discussion, or
        other concrete deal evidence.
      - Not Sales for an unsolicited inbound vendor pitch, generic
        marketing, a newsletter, purely informational content, or generic
        sales language with no genuine deal evidence -- "Sales" means the
        user's own pipeline, not someone else's pitch to the user.
      - Produce the full `EmailAnalysis` reflecting only what's actually in
        this message, then call `persist_email_analysis`.
      - Produce a bounded `ContextDelta` (only what this message changes)
        and call `persist_context_delta`.
   3. **Apply the matching Gmail label**: call the Gmail connector's
      `label_message` with this message's real Gmail id (never the
      canonical `EML-nnn`) and the label id from step 5's map matching
      this message's own `label_applied` value exactly.
   4. If the message genuinely needs a reply, first check whether drafting
      one is actually appropriate: phishing/spoofing red flags across the
      whole thread so far (a sender domain mismatch, implausible contact
      details, unfilled template placeholders like `[Target Date]` still
      present anywhere in the thread), a request for sensitive data (bank
      details, credentials, passwords, government IDs), or any other reason
      a drafted reply would be unsafe or premature. If any apply, do not
      draft a committal reply -- either skip the draft or keep it strictly
      non-committal, call `set_reply_withheld_reason` with the message_id
      and a concise, specific reason, and flag the suspicion in the final
      report. Otherwise draft a reasonable reply and call
      `create_reply_draft`. Skip this entire step if no reply is warranted
      at all.
      - **Immediately after `create_reply_draft` succeeds**, also create a
        matching draft directly in the user's own Gmail mailbox: call the
        Gmail connector's `create_draft` tool with the exact same subject
        and body, addressed as a reply to the original sender within the
        original thread (use the thread/message identifiers so Gmail nests
        it correctly under the original conversation, not as a new,
        unthreaded draft). This lets the user open it directly in Gmail,
        edit it, and send it himself -- it is still never sent
        automatically by this skill, in Gmail or anywhere else.
   5. Call `mark_email_completed`.
   6. **If any of steps 1-5 fails**, stop the loop immediately -- do not
      attempt the next message in this run.
7. **Report the run**, in this form:

```
Thread <THR-id> processed.
Processed (newly completed): <message ids, one-line summary each>
Skipped (already completed): <message ids, or "None">
Failed: <message id + error, if the loop stopped early -- "None" otherwise>
Remaining unprocessed in this thread (if stopped early): <count, or "None">
Gmail drafts created (awaiting your review/send): <message ids, or "None">
Needs reply but withheld (message id + reason, or "None"):
Gmail labels applied: <count, and any newly created label names, or "None">
```

Report only what the tools' own results actually show. Never add a person,
commitment, meeting, follow-up, reply-draft, or classification detail that
didn't come back from a tool call.

## Relationship to the other email skills

- `process-one-email` -- exactly one specific, user-named message.
  Unchanged by this skill.
- `cos-new-email-check` -- "what's new" across the inbox, not scoped to one
  thread.
- `gmail-auto-poll` / `gmail-historical-backfill` -- time-window/batch
  sweeps across many threads.
- `process-thread` (this skill) -- exactly one thread, all of its
  unprocessed messages, in chronological order.

None of these skills depend on each other; each is self-contained and uses
the same underlying granular tools.
