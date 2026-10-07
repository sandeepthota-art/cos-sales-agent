---
name: gmail-raw-dump
description: Ingestion-only raw dump of Gmail emails into MongoDB's raw_emails_dump collection, for later test data -- no classification, no analysis, no knowledge/entity extraction, no labels, no reply drafts. Use only when explicitly asked to "raw dump", "ingest only", or "collect emails for testing" -- never for normal inbox processing (use gmail-initial-ingest for that).
---

# Gmail Raw Dump (ingestion-only, no analysis)

This skill exists for exactly one purpose: pulling real Gmail emails into
MongoDB's `raw_emails_dump` collection, untouched, for later use as test
data. It is **not** a replacement for `gmail-initial-ingest` -- the two are
deliberately separate pipelines, writing to separate collections
(`raw_emails_dump` vs `emails`), with no shared processing state.

## Connector names -- check these before running

- **Gmail connector**: the standard, pre-built Gmail connector
  (`mcp__claude_ai_Gmail__*` tools, or `mcp__remote-devices__Gmail__*`
  depending on the account).
- **cos-sales-agent MCP connector**: whatever this account named its
  connector to the cos-sales-agent MCP server. If more than one similarly
  named connector is visible, confirm which one points at the intended
  MongoDB deployment before running, rather than guessing.

## Hard rules

- **Do not analyze the email.** Not a summary, not a classification, not a
  label, not an extracted entity, not a knowledge item, not a reply draft.
  Your only job is to move the email from Gmail into MongoDB unchanged.
- Only ever call **`ingest_raw_email_only`** on the cos-sales-agent
  connector. Never call `ingest_email`, `persist_email_analysis`,
  `persist_context_delta`, `create_reply_draft`, `set_reply_draft_gmail_id`,
  or any other tool from this skill.
- Never call `create_label` or `label_message` -- no Gmail label is ever
  applied by this skill.
- Use the Gmail connector only to *read* messages. Never send, reply,
  forward, label, trash, or mark spam.
- Never call Claude, OpenAI, or any other LLM API. There is nothing to
  reason about here.

## The procedure

For each message you've been asked to dump:

1. **Read the email from Gmail** (via the Gmail connector's read tool).
2. **Map it to the expected shape**, exactly as `gmail-initial-ingest` does
   for `ingest_email`: Gmail id -> `message_id`, threadId -> `thread_id`,
   sender/recipients -> `from`/`to`/`cc` as `{name, email}` objects, date ->
   `timestamp` (ISO-8601), `in_reply_to`/`references` from the Message-ID
   headers if available. Do not alter the subject or body text in any way.
3. **Call `ingest_raw_email_only`** with that mapped object.
4. **Confirm persistence** -- the tool's return value is the stored
   document; check it came back with no error.
5. **STOP.** Do not proceed to any other cos-sales-agent tool for this
   message. Report back in plain English: how many messages were dumped,
   and any that failed, by Gmail id. Do not dump raw JSON/tool output.

Calling this tool again for a message already dumped is safe -- it updates
the same stored document in place rather than creating a duplicate (keyed
on the Gmail message's own stable id).
