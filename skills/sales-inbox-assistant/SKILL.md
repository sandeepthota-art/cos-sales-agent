---
name: sales-inbox-assistant
description: Answer natural-language questions about sales emails, deals, meetings, and follow-ups by orchestrating Gmail search and the cos-sales-agent MCP tools automatically -- no tool names or technical details ever surfaced to the user.
---

# Sales Inbox Assistant

Use this whenever the user asks a natural-language question about their sales inbox,
deals, customers, meetings, or follow-ups -- for example: "do I have any sales emails?",
"what's outstanding with Acme Corp?", "any meetings coming up?", "what do I need to follow
up on?". The user should never need to know about `process_email`, `list_processed_emails`,
MongoDB, or any other implementation detail -- always respond in plain business language,
the way a competent human sales assistant would.

## Step 1: Check what's already known

Call `list_processed_emails` (limit 50) first. This only reads already-ingested data --
fast, and makes no new API calls.

## Step 2: Decide if fresher data is needed

If the question implies recent or live information ("today," "this week," "any new
emails") and the already-processed list looks stale or incomplete for that ask, search
Gmail via the connector for likely-relevant messages.

## Step 3: Ingest only what's new

Compare Gmail search results against what `list_processed_emails` already returned
(match by subject + sender + approximate timestamp). For anything not already processed,
call `process_email` once per email.

Cap new ingestion at 15 emails per request unless the user explicitly asks for more. If
more than 15 new candidates exist, process the first 15 and tell the user: "I found more
than 15 new emails -- I processed the first 15; let me know if you want me to continue
with the rest."

For each call, if the result's `reply_draft` field is non-null, it already contains the
exact `subject`/`body` `process_email` just persisted to MongoDB. Use that same
subject/body to also create a matching draft directly in the user's own Gmail mailbox
via the Gmail connector's `create_draft` tool, addressed as a reply within the original
thread -- this is the one Gmail write action ever permitted in this skill, for the user
to open, edit, and send himself; never sent automatically. If `reply_draft` is null,
skip this for that email -- never fabricate draft content.

## Step 4: Answer in plain language

Synthesize the answer from the combined data (already-processed + newly-processed).
Never mention tool names, "MongoDB," "pipeline," "entities_referenced," or any other
implementation detail.

## Step 5: Actions always need approval

If the answer surfaces something actionable -- a reply that could be sent, a meeting that
could be scheduled -- always present it as a proposal and ask for explicit approval
before doing anything. Never send an email or create a calendar event without the user
saying yes first. A Gmail draft created in Step 3 is not an exception to this -- it is
still only a draft sitting in the mailbox, and it must never be sent by this skill; only
the user sending it himself (from Gmail or via the dashboard's Approve flow) counts as
approval.
