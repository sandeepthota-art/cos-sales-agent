---
name: email-ingestion
description: Stage 1 of a 3-stage email-processing sequence (email-ingestion -> context-building -> email-labelling). Does exactly one thing -- dedup, raw persistence, and thread resolution via ingest_email -- then stops. Never classifies, never resolves entities, never builds context, never decides a label. Use when you want ingestion isolated from everything downstream, for testing or for staged processing of your own email set.
---

# Email Ingestion (stage 1 of 3)

This is the first of three deliberately separated stages over the same
email: **email-ingestion** (this skill) -> **context-building** ->
**email-labelling**. Each stage does one job and stops. This skill's job is
getting the email into the canonical `emails`/`threads` collections,
correctly deduplicated and thread-resolved -- nothing else.

## Hard rules

- **Only ever call `ingest_email`.** Never call `persist_email_analysis`,
  `persist_context_delta`, `create_reply_draft`, `create_label`,
  `label_message`, or `mark_email_completed` from this skill -- those belong
  to stage 2 or stage 3.
- **Never call Claude, OpenAI, or any other LLM API.** `ingest_email` is pure
  dedup/thread-resolution logic; there is nothing to reason about yet.
- Do not read or act on the email's content at all in this stage -- no
  classification, no entity recognition. That starts in stage 2.

## Field mapping

Same shape every skill in this project uses: id -> `message_id`, threadId ->
`thread_id` (omit if unknown), sender/recipients -> `from`/`to`/`cc` as
`{name, email}` objects, date -> `timestamp` (ISO-8601). Subject and body
copied exactly as your source gives them.

## Step by step, per email, in order (oldest to newest)

1. **Get the raw email.** Read it from your source (a file, or a Gmail
   connector) and map it to the shape above.
2. **Call `ingest_email`** with the mapped object.
3. **Read the result:**
   - `already_completed: true` -> this email was already fully processed
     (stage 2 and stage 3 already ran for it) in an earlier run. Report it
     as already-done; do not proceed to stage 2 or 3 for this email.
   - `already_completed: false` -> record `message_id`, `thread_id`,
     `previous_context`, and `thread_timeline` -- stage 2 will need
     `message_id` and benefits from `previous_context`/`thread_timeline`
     for classification context. **Also keep the original real Gmail id**
     (the one you read the email with, before this call canonicalized it to
     `message_id`) -- stage 3 (`email-labelling`) needs it later to call
     `label_message`, which requires the real Gmail id, never the canonical
     one.

## Failure handling

If one email fails (a tool call raises), record which message_id failed and
why, then continue to the next email.

## Reporting back

After the batch, report in plain English:

- how many emails were examined
- how many were newly ingested (`already_completed: false`) vs. already
  fully processed (`already_completed: true`)
- the `message_id`/`thread_id` pairs for each newly-ingested email, so stage
  2 (context-building) can be run against them next
- any failures, by message_id and reason

Do not dump raw JSON/tool output. For example: "Ingested 10 emails: 8 new
(EML-011 through EML-018, across 6 threads), 2 already fully processed. No
failures."
