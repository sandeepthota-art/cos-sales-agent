---
name: raw-dump-replay
description: Entities-only replay of your own email set (a Gmail-export JSON file, or Gmail messages previously stored by gmail-raw-dump) through the deterministic entity-resolution pipeline -- ingest_email, then you classify the email yourself, then persist_email_analysis with skip_knowledge=true, so people/projects/commitments/follow_ups/meetings/opportunities/personal_items get created without any knowledge_items noise. Use only when explicitly testing entity resolution against a test email set -- never for normal inbox processing (use gmail-initial-ingest for that), and never as a substitute for gmail-raw-dump (which stores raw copies only and never analyzes).
---

# Raw-Dump Replay (entities-only test path)

This skill exists to let you validate the canonical entity-resolution pipeline
(Person/Organization/Project/Opportunity/Commitment/FollowUp/Meeting/
PersonalItem) against a test set of your own emails, without the knowledge
layer (`knowledge_items`) cluttering the result, and without needing a live
Gmail connector at all if your emails already live in a local file.

## How this differs from the other two email skills

- **`gmail-raw-dump`**: stores raw emails untouched into `raw_emails_dump`.
  Never analyzes anything. A dead end by design -- nothing reads that
  collection back out, including this skill. If your test emails are a local
  file, you can call `ingest_raw_email_only` for safekeeping, but you do not
  need to go through `raw_emails_dump` to run this skill -- read your source
  file directly for the steps below.
- **`gmail-initial-ingest`**: the full production flow -- classification,
  Gmail labeling, reply drafting, knowledge extraction, the works.
- **`raw-dump-replay`** (this skill): classification and entity resolution
  only. No Gmail label is ever applied, no reply draft is ever created, and
  `persist_email_analysis` is always called with `skip_knowledge=true` so
  `knowledge_items` stays empty. This is a test harness, not a production
  path.

## Connector names -- check these before running

- **cos-sales-agent MCP connector**: whatever this account named its
  connector to the cos-sales-agent MCP server. Confirm it points at the
  intended MongoDB deployment (a test database, not production) before
  running a batch against your own email set.
- A Gmail connector is only needed if your source emails are live Gmail
  messages rather than a local file.

## Hard rules

- **Always call `persist_email_analysis` with `skip_knowledge=true`.** This
  is the entire point of this skill over `gmail-initial-ingest`.
- **Never call `create_label`, `label_message`, `create_reply_draft`,
  `set_reply_draft_gmail_id`, or `set_reply_withheld_reason`.** This is a
  test pass over entity resolution, not a reply workflow.
- **Never call `process_email`** (it doesn't exist in this codebase --
  removed; if you ever see a tool by that name, something is misconfigured).
- **Never call Claude, OpenAI, or any other LLM API** to do the
  classification. You read the email and classify it yourself, in this
  conversation, exactly as `gmail-initial-ingest` Step 2 describes.
- Do not act on `new_organizations_needing_research` or
  `people_profile_context` from the `persist_email_analysis` result -- this
  is a quick entities-only pass, not a full customer-intelligence run.

## Field mapping

Same shape every other skill in this project uses: id -> `message_id`,
threadId -> `thread_id` (omit if unknown), sender/recipients -> `from`/`to`/
`cc` as `{name, email}` objects, date -> `timestamp` (ISO-8601). Subject and
body copied exactly as your source gives them.

## Step by step, per email, in order (oldest to newest)

1. **Get the raw email.** Read it from your source (the file you're testing
   with, or a Gmail connector if your source is live Gmail) and map it to
   the shape above.
2. **(Optional, recommended) Call `ingest_raw_email_only`** with the mapped
   object, for a safe untouched copy in `raw_emails_dump`. This step is
   independent of step 3 below -- there is currently no tool to read
   `raw_emails_dump`'s contents back out, so don't rely on it as a staging
   area; always read the raw email from your actual source for step 3.
3. **Call `ingest_email`** with the same mapped object. Check
   `already_completed`:
   - `true` -> this email was already fully processed in an earlier run.
     Skip it, move to the next email.
   - `false` -> read `previous_context`/`thread_timeline` if present, then
     continue.
4. **Classify the email yourself**, using the exact same classification
   criteria as `gmail-initial-ingest` Step 2 (Sales vs. Not-Sales
   recognition rules, the six `label_applied` values, evidence-gated
   `person_facts_mentioned`, real names only in `people_mentioned`, etc.) --
   build the `EmailAnalysis` object. Do not invent entities or
   relationships; if nothing in a field applies, leave it empty.
5. **Call `persist_email_analysis(message_id, analysis, skip_knowledge=true)`.**
   Read the result: `entities_referenced` tells you what got created/reused
   this email; `possible_missed_commitment` is a non-authoritative heuristic
   -- re-read the email if it fires, don't treat it as an error;
   `calendar_proposal` is informational only (never approved here).
6. **Call `mark_email_completed(message_id)`** to close it out. Note: this
   makes the email idempotently skipped on a future replay of the same test
   set (`already_completed: true` at step 3) -- if you plan to re-run this
   same batch after a code change, reset your test database between runs
   rather than relying on this skill to reprocess already-completed emails.
7. **Do not** label Gmail, draft a reply, or approve/reject any calendar
   action for this email.

## Failure handling

If one email fails (a tool call raises), record which message_id failed and
why, then continue to the next email -- one bad email should never stop the
whole batch.

## Reporting back

After the batch, report in plain English:

- how many emails were examined, how many skipped as already-completed
- total counts of each entity type created/reused across the whole batch
  (people, organizations, projects, opportunities, commitments, follow_ups,
  meetings, personal_items) -- not knowledge_items, since none should exist
- any `possible_missed_commitment` flags raised, by message_id
- any failures, by message_id and reason

Do not dump raw JSON/tool output. For example: "Replayed 10 emails, 0
already-completed. Created: 3 people, 1 organization, 2 projects, 1
opportunity, 4 commitments, 2 follow-ups, 1 meeting, 1 personal item. No
missed-commitment flags. No failures."
