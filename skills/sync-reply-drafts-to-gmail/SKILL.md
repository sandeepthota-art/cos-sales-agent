---
name: sync-reply-drafts-to-gmail
description: Use when asked to sync, mirror, or push pending reply drafts into the CTO's real Gmail drafts folder (e.g. "sync reply drafts to Gmail", "put the pending drafts in my Gmail", "mirror MongoDB reply drafts into Gmail"). Creates real Gmail drafts matching this app's own reply_drafts records -- never sends anything, never approves/rejects/edits a draft in MongoDB.
---

# Sync Reply Drafts to Gmail

Trigger phrases: "sync reply drafts to Gmail", "put the pending drafts in my
inbox", "mirror the MongoDB drafts into Gmail", or any request to make this
app's generated reply drafts visible/editable directly in the CTO's own Gmail
account, as real Gmail drafts.

## Why this skill exists

This project has no real email-sending or draft-creation integration of its
own (`app.replies.approval.simulate_send` only ever prints to a log and marks
a MongoDB record -- see its own docstring). Every real Gmail interaction in
this codebase happens the same way: an interactive Claude session uses its
own Gmail connector tools. This skill is the reply-draft equivalent of
`cos-new-email-check`/`gmail-historical-backfill` (which use the Gmail
connector to *read* mail) -- it uses the Gmail connector to *create drafts*,
so the CTO can review/edit/send a reply from their own Gmail, as an
alternative to approving it inside this app.

## Hard rules

- Only ever use the `cos-sales-agent` connector's tools
  (`mcp__remote-devices__cos-sales-agent__*`) to read/update reply drafts.
  Only ever use the Gmail connector's `create_draft` tool to touch Gmail --
  never `send_message`, `reply`, or `forward`. This skill must never send a
  real email.
- Never call `approve`/`reject`/`edit` on a reply draft, and never change a
  draft's `status` in MongoDB. Syncing to Gmail is completely independent of
  this app's own approval decision -- a draft can be approved here, in
  Gmail, in both, or in neither.
- Never invent a recipient, subject, or body. The Gmail draft's content must
  be byte-for-byte the same subject/body already stored in MongoDB's
  `reply_drafts.draft`.
- Idempotent: only syncs a reply draft that doesn't already have a
  `gmail_draft_id` recorded. Re-running this skill is always safe -- it
  never creates a second Gmail draft for the same reply.
- Cap at 10 drafts per run (same spirit as `gmail-historical-backfill`'s
  20-email cap) -- if more than 10 are pending sync, sync the first 10 and
  tell the user how many remain.

## Procedure

1. Call `list_reply_drafts` with `status="awaiting_approval"`.
2. Filter out any draft that already has a non-null `gmail_draft_id` --
   those are already synced. If nothing remains, report "Nothing to sync"
   and stop.
3. Cap the remaining list at 10 (oldest first, by `created_at` when
   present). Note how many are left over, if any.
4. For each draft in the capped batch:
   a. Call `get_thread` with the draft's `thread_id`.
   b. Find the message in `messages` whose `message_id` equals the draft's
      `source_email_id`. If no match is found, skip this draft and record
      why -- never guess a recipient.
   c. The reply's recipient is that message's `from.email` (the original
      email's sender -- the person this draft replies to).
   d. That message's `source_message_id` (the real Gmail message id,
      distinct from the canonical `message_id`) becomes `replyToMessageId`
      for threading. If it's `None` (a message ingested without a real
      Gmail source), create the draft without `replyToMessageId` -- still
      create it, just not threaded -- and note this in the final report.
   e. Call the Gmail connector's `create_draft` with `to=[recipient]`,
      `subject=<draft.draft.subject>`, `body=<draft.draft.body>`, and
      `replyToMessageId` from step (d) when available. Do not set
      `htmlBody` -- plain text only, matching what's actually stored.
   f. Take the returned draft's `id` and call `set_reply_draft_gmail_id`
      with this reply's `reply_id` and that Gmail draft id, so a future run
      of this skill skips it.
5. Report back in plain English: how many were synced, how many were
   skipped and why (already synced / no matching source message / creation
   failed), whether each synced draft was threaded or not, and how many
   remain beyond the 10-per-run cap (if any). Do not dump raw tool output.

## What this skill deliberately does not do

- It does not send any email, real or simulated.
- It does not change a reply draft's approval status in MongoDB.
- It does not resolve a recipient any other way than the original message's
  own `from` field -- no name-guessing, no fuzzy matching.
- It does not run on a schedule by itself -- like
  `gmail-historical-backfill`, it is on-demand only, invoked explicitly.
