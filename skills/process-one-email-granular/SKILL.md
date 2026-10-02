---
name: process-one-email-granular
description: Use when the user points at one specific email and asks to process/ingest it (e.g. "process this email", "analyze my latest email", "ingest the email from James about pricing") AND you must not depend on a server-side LLM API call -- processes exactly that one email via the granular tools, with Claude itself doing the reading/classification, never process_email. Distinct from process-one-email (same scope, but calls process_email instead).
---

# Process One Email (granular pipeline, no LLM API dependency)

Trigger phrases: "process this email", "analyze my latest email", "run this
email through the pipeline yourself", "ingest the email from X about Y
without using the API", or any request that names or clearly identifies
exactly one specific email, where the granular/no-LLM-API-key pipeline is
wanted instead of `process_email`.

This is the granular-tools sibling of `process-one-email`: same scope
(exactly one, user-identified email), but **Claude itself reads and
classifies the message in this conversation** -- no call to `process_email`
and no dependency on a separately configured `LLM_API_KEY` at all. Use this
skill whenever that LLM-API dependency is something to avoid (it has been
unreliable in this project -- expired-key/401 errors) or whenever the user
explicitly asks not to use "the API"/`process_email`.

## Hard rules

- Only ever use the `cos-sales-agent` connector's tools for this pipeline
  job. Never call `process_email` -- always the granular tools, in this
  exact order: `ingest_email` -> (you read and classify the message) ->
  `persist_email_analysis` -> `persist_context_delta` -> (`create_reply_draft`
  if warranted) -> `mark_email_completed`.
- Use Gmail to *find* the one email the user means and to *read* it, and —
  only immediately after `create_reply_draft` succeeds — to *create a
  draft* via the Gmail connector's `create_draft` tool. Never actually
  send, reply (dispatch), forward, label, delete, or otherwise modify
  anything in Gmail; creating a draft is the one write action ever
  permitted here, and it must never be sent automatically by this skill.
- Process exactly the one email the user identified. If their request is
  ambiguous (matches more than one email, or none), ask which one they
  mean rather than guessing or processing multiple.
- Skip (don't reprocess) if it's already `COMPLETED` -- `ingest_email`'s
  own `already_completed` flag is the authoritative check.
- Never create a real calendar event -- `persist_email_analysis` only ever
  proposes a `calendar_action` (`awaiting_approval` / `needs_clarification`).
- Never invent people, projects, commitments, follow-ups, meetings, or
  reply content that isn't genuinely present in the message.
- Never call Claude, OpenAI, or any other LLM API for classification --
  you are the classifier, using your own reading of the email. Do not
  expose your reasoning/chain-of-thought in any tool call or stored data --
  only the resulting structured fields.
- **Before creating any Project or Opportunity, check for an existing one
  with the same name/entity first** (e.g. via `get_project_summary` /
  `list_projects` / `list_opportunities`). Reuse it rather than creating a
  duplicate just because this message's own mention omitted an `org`/
  entity value an earlier message already supplied.

## Procedure

1. **Identify the one email** the user means. If they gave a sender,
   subject, or "latest"/"most recent", search Gmail for it. If more than
   one message plausibly matches, ask for clarification instead of
   picking one.
2. **Call `ingest_email`** (map Gmail id -> `message_id`, threadId ->
   `thread_id`, sender/recipients -> `from`/`to`/`cc`, date ->
   `timestamp`, include `in_reply_to`/`references` if available).
   - If it returns `already_completed: true`, stop here and report that
     plainly ("this email was already processed on \<date\>, nothing new
     to report") -- do not call any of the persist_* tools.
   - Otherwise, read `previous_context`/`thread_timeline` before
     reasoning about it.
3. **Read the message yourself and classify Sales vs Not Sales**:
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
4. **If the message genuinely needs a reply**, first check for
   phishing/spoofing red flags across the thread so far (a sender domain
   mismatch, implausible contact details, unfilled template placeholders
   still present) -- if any are present, never draft a committal reply;
   either skip the draft or keep it strictly non-committal, and flag the
   suspicion in the final report. Otherwise draft a reasonable reply and
   call `create_reply_draft`. Skip this step if no reply is warranted.
   - **Immediately after `create_reply_draft` succeeds**, also create a
     matching draft directly in the user's own Gmail mailbox via the
     Gmail connector's `create_draft` tool, using the exact same subject
     and body, addressed as a reply within the original thread (use the
     thread/message identifiers so it nests correctly under the original
     conversation). This is for the user to open, edit, and send himself
     from Gmail -- never sent automatically by this skill.
5. **Call `mark_email_completed`**.
6. **Report back in plain English** -- who the email is from, what it's
   about, the label/classification given, what was created (people,
   commitments, follow-ups, a meeting, a project), whether a reply draft
   was generated and a matching Gmail draft created, and whether it
   showed any phishing/spoofing red flags. Do not dump raw JSON/tool
   output.

## Relationship to the other email skills

- `process-one-email-granular` (this skill) -- exactly one user-named
  email, granular tools, Claude does the classification, no LLM API key.
- `process-one-email` -- same scope, but calls `process_email` (a
  server-side LLM call) instead; use only when that dependency is fine.
- `gmail-initial-ingest` -- an exact count of the most recent inbox
  messages, for a one-time initial backfill.
- `process-thread` -- one named thread, all its unprocessed messages.
- `cos-new-email-check` -- ongoing "what's new" inbox check.

None of these skills depend on each other; each is self-contained.
