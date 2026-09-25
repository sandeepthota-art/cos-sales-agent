---
name: gmail-historical-backfill
description: On-demand (never scheduled) sweep of older Gmail messages -- a message count or an explicit date range you specify -- through the existing cos-sales-agent-v2 process_email tool. Capped per run, safe to re-run over the same scope any number of times thanks to message_id/COMPLETED dedup. Never sends email, never creates real calendar events, never invents data. Separate from gmail-auto-poll, which only ever looks at the last 15 minutes.
---

# Gmail Historical Backfill (on-demand)

This skill is for catching up older Gmail messages that predate `gmail-auto-poll`'s
15-minute window -- e.g. "process the last 15 emails" or "process everything
between Sept 17 and Sept 23." It is **never invoked on a timer**. Run it only when
you're explicitly asked to.

It shares its safety model with `gmail-auto-poll` (same dedup, same hard rules) but
is a distinct skill: do not merge this logic into `gmail-auto-poll`, and do not let
`gmail-auto-poll` widen its own window to cover this job instead.

## Step 1: Determine scope from the request

Before searching, resolve exactly one of these from what the user asked for:

- **A count** (e.g. "the last 15 emails," "the last 20") -- most recent N messages
  by timestamp.
- **A date range** (e.g. "between Sept 17 and Sept 23," "since last Monday") --
  every message whose timestamp falls in that range.

If neither is clear from the request, ask before searching -- never guess a scope
for a backfill sweep.

## Step 2: Search Gmail for candidates in that scope

Use the Gmail connector (Gmail only -- never any other source, never Gmail
API/OAuth) with a query matching the resolved scope, e.g.:

```
in:inbox -in:drafts -in:sent after:2026/09/17 before:2026/09/23
```

or, for a count-based scope, the most recent N messages from a plain `in:inbox`
search. Treat results as candidates; if Gmail returns threads, enumerate the
individual messages within each thread rather than treating a thread as one item.

## Step 3: Cap the batch at 20 per run

Process at most **20 messages per invocation**, oldest-scope-violation-safe: if the
resolved scope contains more than 20 candidates, process the first 20 (by
timestamp, oldest first) and tell the user exactly how many remain and offer to
continue: *"Found <N> messages in this range -- processed the first 20; say the
word to continue with the rest."* Never silently process more than 20 in one run.

## Step 4: Process every message within the capped batch

Call the `cos-sales-agent-v2` `process_email` tool once for every message in the
capped batch -- the same tool and field mapping already used for single-email
processing today. Call it even for messages you suspect are already processed:
`process_email`'s pipeline checks `message_id` + `processing_status.stage ==
COMPLETED` and safely skips anything already done. This is what makes it safe to
re-run this skill over the same range repeatedly (e.g. to work through a backlog
20 at a time) -- never pre-filter or skip a message based on a guess instead of
letting `process_email` make that call.

Never call any tool from a connector other than Gmail to *find* messages, and
never call any `cos-sales-agent`/`cos-sales-agent-v2` tool other than
`process_email` for this skill's CoS processing job.

## Step 5: Report the run

```
Gmail historical backfill @ <current time>
Scope: <count or date range as resolved in Step 1>
Candidates found: <N>
Processed this run (capped at 20): <count>
Completed (newly processed): <count>
Skipped (already COMPLETED): <count>
Failed: <count, with message_id + error if any>
Remaining in scope (if any): <count> -- say the word to continue
```

Report only what the Gmail search and `process_email` results actually show.
Never add a person, project, commitment, meeting, follow-up, or reply-draft
detail that didn't come back from the tool.

## Hard rules (same as gmail-auto-poll)

- Use Gmail only to find emails -- never send, reply, or forward.
- Use `cos-sales-agent-v2` only for CoS processing -- `process_email` only, never
  `mark_email_completed`, `persist_email_analysis`, `persist_context_delta`,
  `create_reply_draft`, or any other tool in its place.
- Never create a real Google Calendar event. `process_email` only ever proposes a
  `calendar_action` in MongoDB (`awaiting_approval` / `needs_clarification`) --
  it never creates a real event, and this skill must not either.
- Rely on `message_id`/`COMPLETED` deduplication -- never invent separate skip
  logic for completed messages.
- Never invent people, projects, commitments, follow-ups, meetings, or reply
  content that `process_email`'s own result didn't actually return.
- Never process more than 20 messages in a single run.
- Never guess a scope -- confirm count or date range before searching.
- Do not modify Python code or any existing skill file (including
  `gmail-auto-poll`) -- this skill only reads Gmail and calls the existing
  `process_email` tool.
- Never run this skill on a schedule or timer -- it is on-demand only.
