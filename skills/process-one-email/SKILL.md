---
name: process-one-email
description: Use when the user points at one specific email and asks to process/ingest it (e.g. "process this email", "run my latest email through the pipeline", "ingest the email from James about pricing") -- calls the cos-sales-agent process_email tool directly. Not for "what's new" checks (see cos-new-email-check) or scheduled sweeps (see gmail-auto-poll / gmail-historical-backfill).
---

# Process One Email (direct `process_email`)

Trigger phrases: "process this email", "process my latest email", "run this
email through the pipeline", "ingest the email from X about Y", or any request
that names or clearly identifies exactly one specific email to process.

This is deliberately the simplest possible skill: find the one email the user
means, hand it to `process_email`, report what happened. It does not search for
"what's new" (that's `cos-new-email-check`) and it does not sweep a time window
or a batch (that's `gmail-auto-poll` / `gmail-historical-backfill`).

## Hard rules

- Only ever use the `cos-sales-agent` connector's tools
  (`mcp__remote-devices__cos-sales-agent__*`). No other MCP connector is used
  for this skill's CoS processing job.
- Use Gmail only to *find* the one email the user means -- never send, reply,
  forward, or modify it.
- Call `process_email` -- never the granular Phase 1 tools (`ingest_email`,
  `persist_email_analysis`, `persist_context_delta`, `create_reply_draft`,
  `mark_email_completed`); those are for when you're doing the reasoning
  yourself, which this skill never does.
- Process exactly the one email the user identified. If their request is
  ambiguous (matches more than one email, or none), ask which one they mean
  rather than guessing or processing multiple.
- Never create a real calendar event. `process_email` only ever proposes a
  `calendar_action` in MongoDB (`awaiting_approval` / `needs_clarification`).
- Never invent people, projects, commitments, follow-ups, meetings, or reply
  content beyond what `process_email`'s own result actually returns.

## Procedure

1. **Identify the one email** the user means. If they gave a sender, subject,
   or "latest"/"most recent", search Gmail for it. If more than one message
   plausibly matches, ask for clarification instead of picking one.
2. **Map the Gmail message into the shape `process_email` expects**:
   `message_id` (Gmail id), `thread_id` (Gmail threadId), `from`/`to`/`cc` as
   `{name, email}` objects, `subject`, `body` (plaintext), `timestamp`
   (ISO-8601), and `in_reply_to`/`references` from the Message-ID headers if
   available.
3. **Call `process_email`** with that object. It runs the full pipeline
   (normalization, LLM analysis, context, knowledge extraction, meeting
   detection, reply drafting) and persists everything -- it is already
   idempotent (an already-`COMPLETED` message is skipped internally, not
   reprocessed), so calling it again on the same email is always safe.
4. **Report back in plain English** -- who the email is from, what it's
   about, the label and priority it was given, what was created (people,
   commitments, follow-ups, a meeting, a project), and whether a reply draft
   was generated (never sent -- it always waits for human approval). Do not
   dump raw JSON/tool output.

## If the email was already processed

`process_email`'s result will reflect that the pipeline reached `COMPLETED`
without creating anything new. Say so plainly ("this email was already
processed on \<date\>, nothing new to report") rather than re-describing
stale results as if they just happened.
