---
name: gmail-initial-ingest
description: Use for a one-time initial backfill of a specific, exact count of the most recent Gmail messages (e.g. "ingest my last 50 emails", "pull in the 50 most recent emails from my inbox") into the cos-sales-agent pipeline -- processes exactly that count, oldest to newest, via the granular tools, never process_email. Distinct from gmail-historical-backfill (date-range or larger count, repeatable in 20-message pages) and process-thread (one specific thread).
---

# Gmail Initial Ingest (one-time, exact count, granular pipeline)

Trigger phrases: "ingest my last 50 emails", "pull in the 50 most recent
emails from my inbox", "do an initial backfill of N emails", or any request
naming an exact number of the most recent messages in *this account's*
Gmail inbox to bring into the pipeline for the first time.

This skill is for a one-time, bounded initial load -- e.g. seeding a brand
new cos-sales-agent deployment with recent history before relying on
`cos-new-email-check` / `process-thread` for everything going forward. It
is not for ongoing polling and not for an open-ended backlog sweep (that's
`gmail-historical-backfill`, which pages through a date range or a larger
count 20 at a time, and is explicitly repeatable).

## Connector names -- check these before running

The exact connector names differ per Claude account. Before the first run,
confirm:

- **Gmail connector**: the standard, pre-built Gmail connector
  (`mcp__claude_ai_Gmail__*` tools) connected to the Gmail account whose
  inbox you want to ingest (e.g. `ashok@databeat.io`).
- **cos-sales-agent MCP connector**: whatever this account named its
  connector to the cos-sales-agent MCP server (e.g. `CoS_Ashok`). Use
  *that* connector's tools throughout -- if the account has more than one
  similarly-named cos-sales-agent connector, confirm which one points at
  the intended MongoDB/Render deployment before running, rather than
  guessing.

## Hard rules

- Use the Gmail connector to *read*, and — only immediately after
  `create_reply_draft` succeeds in step 5.3 below — to *create a draft*
  via the Gmail connector's `create_draft` tool. Never actually send,
  reply (dispatch), forward, label, delete, or otherwise modify anything
  in Gmail; creating a draft is the one write action ever permitted here,
  and it must never be sent automatically by this skill.
- Only ever use the one confirmed cos-sales-agent MCP connector's tools
  for this pipeline job. Never call `process_email` -- always the granular
  tools, in this exact order, per message: `ingest_email` -> (you read and
  classify the message) -> `persist_email_analysis` -> `persist_context_delta`
  -> (`create_reply_draft` if warranted) -> `mark_email_completed`.
- **Process exactly the requested count** (default 50 if the user just
  says "my last emails" and doesn't restate a number) of the most
  recent messages in the inbox -- never more, never fewer. If Gmail
  returns threads, enumerate the individual messages within each thread
  rather than treating a thread as one item, and still stop once the
  requested count of individual messages is reached.
- **Process strictly oldest to newest** by timestamp, even though Gmail
  returns most-recent-first -- sort the capped batch before processing.
- Skip (don't reprocess) any message that's already `COMPLETED` --
  `ingest_email`'s own `already_completed` flag is the authoritative check.
  A skipped message still counts toward the requested count (it was
  already in scope), it just isn't persisted again.
- **A failure on one message does not stop the run** -- these are
  typically unrelated messages from different threads, so continue to the
  next message, record the failure, and keep going. (This differs from
  `process-thread`, where messages share one thread's chronological
  context and a stop-on-failure rule applies instead.)
- Never create a real calendar event -- `persist_email_analysis` only ever
  proposes a `calendar_action` (`awaiting_approval` / `needs_clarification`).
- Never invent people, projects, commitments, follow-ups, meetings, or
  reply content that isn't genuinely present in a message.
- Never call Claude, OpenAI, or any other LLM API for classification --
  you are the classifier, using your own reading of each email. Do not
  expose your reasoning/chain-of-thought in any tool call or stored data --
  only the resulting structured fields.
- **Before creating any Project or Opportunity, check for an existing one
  with the same name/entity first** (e.g. via `get_project_summary` /
  `list_projects` / `list_opportunities`). Reuse it rather than creating a
  duplicate just because one message's own mention omitted an `org`/entity
  value an earlier message already supplied.
- This skill runs once, on request -- never on a timer or schedule.

## Procedure

1. **Confirm the two connectors** per the section above if there's any
   ambiguity (more than one Gmail or cos-sales-agent connector visible).
   If genuinely ambiguous, ask which to use rather than guessing.
2. **Determine the count** from the request (default 50 if unstated).
3. **Search the Gmail inbox** for the most recent messages
   (`in:inbox -in:drafts -in:sent`, newest first), enumerating individual
   messages out of any threads, until you have at least that many
   candidates.
4. **Cap and sort**: take exactly the requested count of the most recent
   messages, then sort that batch oldest to newest by timestamp.
5. **For each message in that batch, in order:**
   1. Call `ingest_email` (map Gmail id -> `message_id`, threadId ->
      `thread_id`, sender/recipients -> `from`/`to`/`cc`, date ->
      `timestamp`, include `in_reply_to`/`references` if available).
      - If it returns `already_completed: true`, record it under
        "skipped" and move to the next message.
      - Otherwise, read `previous_context`/`thread_timeline` before
        reasoning about it.
   2. Read the message yourself and classify Sales vs Not Sales:
      - Sales only for genuine activity tied to a specific prospect/
        customer: active negotiation, pricing/quote, proposal, contract
        discussion, pilot/POC, renewal, expansion, purchase discussion, or
        other concrete deal evidence.
      - Not Sales for an unsolicited inbound vendor pitch, generic
        marketing, a newsletter, purely informational content, or generic
        sales language with no genuine deal evidence.
      - Produce the full `EmailAnalysis` reflecting only what's actually in
        this message, then call `persist_email_analysis`.
      - Produce a bounded `ContextDelta` (only what this message changes)
        and call `persist_context_delta`.
   3. If the message genuinely needs a reply, first check for
      phishing/spoofing red flags (sender domain mismatch, implausible
      contact details, unfilled template placeholders) -- if present,
      never draft a committal reply; skip the draft or keep it strictly
      non-committal, and flag the suspicion in the final report.
      Otherwise draft a reasonable reply and call `create_reply_draft`.
      Skip this step if no reply is warranted.
      - **Immediately after `create_reply_draft` succeeds**, also create a
        matching draft directly in the user's own Gmail mailbox via the
        Gmail connector's `create_draft` tool, using the exact same
        subject and body, addressed as a reply within the original thread
        (use the thread/message identifiers so it nests correctly under
        the original conversation). This is for the user to open, edit,
        and send himself from Gmail -- never sent automatically by this
        skill.
   4. Call `mark_email_completed`.
   5. **If any of steps 1-4 fails**, record the message id, timestamp, and
      error under "failed" in the final report, and continue to the next
      message in the batch -- do not stop the run.
6. **Report the run**, in this form:

```
Gmail initial ingest @ <current time>
Account: <Gmail address> via <Gmail connector name>
MCP connector used: <cos-sales-agent connector name>
Requested count: <N>
Candidates found: <N found before capping, if more than requested>
Processed (newly completed): <message ids, one-line summary each>
Skipped (already completed): <message ids, or "None">
Failed: <message id + error, for each failure, or "None">
Gmail drafts created (awaiting your review/send): <message ids, or "None">
```

Report only what the tools' own results actually show. Never add a
person, commitment, meeting, follow-up, reply-draft, or classification
detail that didn't come back from a tool call.

## Relationship to the other email skills

- `gmail-initial-ingest` (this skill) -- exactly one run, an exact count
  of the most recent inbox messages, for seeding a new deployment.
- `gmail-historical-backfill` -- date-range or larger-count backlog sweep,
  pages through 20 at a time, explicitly repeatable, uses `process_email`.
- `process-thread` -- exactly one named thread, all its unprocessed
  messages, stop-on-first-failure.
- `cos-new-email-check` -- ongoing "what's new" inbox check.
- `process-one-email` -- exactly one specific, user-named message.

None of these skills depend on each other; each is self-contained and uses
the same underlying granular tools (except `gmail-historical-backfill`,
which intentionally still uses `process_email`).
