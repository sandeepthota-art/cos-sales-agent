---
name: gmail-raw-dump
description: Ingestion-only raw dump of Gmail emails into MongoDB's raw_emails_dump collection, for later test data -- no classification, no analysis, no knowledge/entity extraction, no labels, no reply drafts. Supports both an explicit count/named dump and an incremental "what's new since last dump" sweep. Use only when explicitly asked to "raw dump", "ingest only", or "collect emails for testing" -- never for normal inbox processing (use gmail-initial-ingest for that).
---

# Gmail Raw Dump (ingestion-only, no analysis)

This skill exists for exactly one purpose: pulling real Gmail emails into
MongoDB's `raw_emails_dump` collection, untouched, for later use as test
data. It is **not** a replacement for `gmail-initial-ingest` -- the two are
deliberately separate pipelines, writing to separate collections
(`raw_emails_dump` vs `emails`), with no shared processing state, no shared
checkpoint, and no shared dedup.

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
- Only ever call **`get_raw_ingestion_status`** and **`ingest_raw_email_only`**
  on the cos-sales-agent connector. Never call `ingest_email`,
  `get_last_ingested_email`, `persist_email_analysis`, `persist_context_delta`,
  `create_reply_draft`, `set_reply_draft_gmail_id`, or any other tool from
  this skill.
- Never call `create_label` or `label_message` -- no Gmail label is ever
  applied by this skill.
- Use the Gmail connector only to *read* messages. Never send, reply,
  forward, label, trash, or mark spam.
- Never call Claude, OpenAI, or any other LLM API. There is nothing to
  reason about here.
- Do not alter the subject or body text in any way -- no Re:/Fwd: stripping,
  no whitespace collapsing, no summarizing. Map fields, don't transform them.

## Step 0: Determine the mode

- **"Ingest/dump my last N emails"**, or a specific named email/thread ->
  **Mode A: Explicit dump**. Skip the checkpoint entirely; dump exactly the
  requested message(s).
- **"What's new", "catch up the raw dump", "dump anything since last time"**,
  or no count/name given at all -> **Mode B: Incremental sweep**, driven by
  the checkpoint below.

## Mode A: Explicit dump

For each requested message, in order (oldest to newest):

1. **Read the email from Gmail.**
2. **Map it to the expected shape** (see "Field mapping" below).
3. **Call `ingest_raw_email_only`** with that mapped object.
4. Record its `already_existed` value (true = duplicate, already there;
   false = newly stored).
5. **STOP after the batch.** Do not proceed to any other cos-sales-agent tool.
   Report per "Reporting back" below.

## Mode B: Incremental sweep

1. **Call `get_raw_ingestion_status`** first, before searching Gmail.
2. **Determine the start date:**
   - `has_existing_data: false` -> nothing has ever been raw-dumped. Start
     from this project's agreed fallback: **2026-09-15**.
   - `has_existing_data: true` -> start from `latest_email_timestamp`'s own
     date (not `latest_ingested_at` -- that field only records when the tool
     happened to run, not what mail is actually new).
3. **Search Gmail** `after:<start date, formatted YYYY/MM/DD>` -- **never add
   a `before:<today>` bound.** Gmail's `before:` is exclusive of the named
   day, so it would silently exclude mail that arrived today, the single
   most common case this mode exists for. An open-ended `after:` search
   already means "everything since that date, including today."
4. If the search surfaces a thread, open it and enumerate its actual
   `messages[]` -- never treat a thread's own id as a single message. Every
   individual message in a thread gets dumped on its own; never deduplicate
   by `thread_id`.
5. For each message the search returns, oldest to newest:
   - **Map it to the expected shape** (see "Field mapping" below).
   - **Call `ingest_raw_email_only`.** Record its `already_existed` value.
     A message on the same day as the search boundary may be re-surfaced
     across runs -- this is expected, not an error: the tool's own dedup on
     `source_message_id` (never on timestamp, never on thread_id) makes
     re-including it safe. It comes back `already_existed: true` and is
     simply skipped.
6. **STOP after the sweep.** Do not proceed to any other cos-sales-agent
   tool. Report per "Reporting back" below.

## Field mapping

Gmail id -> `message_id`, threadId -> `thread_id`, sender/recipients ->
`from`/`to`/`cc` as `{name, email}` objects, date -> `timestamp` (ISO-8601),
`in_reply_to`/`references` from the Message-ID headers if available. Subject
and body are copied exactly as Gmail gives them -- no normalization.

## Reporting back

Tally each message's `ingest_raw_email_only` result as it comes back, then
report in plain English, including:

- the start date/checkpoint used (Mode B only; omit for Mode A)
- how many messages were examined
- how many were newly stored (`already_existed: false`)
- how many were duplicates, already present (`already_existed: true`)
- any that failed, by Gmail id

For example: "Start date: 2026-10-07 (from the last raw-dumped email).
Examined 25, stored 18 new, skipped 7 already-dumped duplicates." Do not
dump raw JSON/tool output. If a sweep finds nothing new at all, say so
plainly ("0 new emails since the last dump") rather than treating it as an
error.
