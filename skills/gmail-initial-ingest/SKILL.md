---
name: gmail-initial-ingest
description: The one skill for every Gmail + cos-sales-agent request -- ingesting an exact count, processing one named email, checking for new mail, processing a whole thread, sweeping a date-range backlog, syncing already-created reply drafts to Gmail, or answering a plain-language question about the inbox/pipeline data. Always via the granular, LLM-free pipeline -- never process_email.
---

# Gmail Ingest & Processing (one consolidated skill)

This is the only skill for CoS Sales Agent / Gmail work. It replaces what used
to be several near-identical skills (gmail-initial-ingest, process-thread,
cos-new-email-check, process-one-email-granular, gmail-historical-backfill,
gmail-auto-poll, process-one-email, sales-inbox-assistant, sync-reply-drafts-
to-gmail, cos-agent-test) -- all of that is now one file, Step 0 below picks
which mode applies.

## Connector names -- check these before running

The exact connector names differ per Claude account. Before the first run,
confirm:

- **Gmail connector**: the standard, pre-built Gmail connector
  (`mcp__claude_ai_Gmail__*` tools, or `mcp__remote-devices__Gmail__*`
  depending on the account) connected to the inbox you want to work with.
- **cos-sales-agent MCP connector**: whatever this account named its
  connector to the cos-sales-agent MCP server. Use that connector's tools
  throughout -- if more than one similarly-named connector is visible,
  confirm which one points at the intended MongoDB/Render deployment before
  running, rather than guessing.

## Step 0: Determine the mode from the request

Resolve exactly one of these before doing anything else. If genuinely
ambiguous, ask rather than guess.

| Request sounds like | Mode |
|---|---|
| "ingest my last N emails", "do an initial backfill of N" | **A: Exact-count backfill** |
| Names or clearly identifies one specific email | **B: One named email** |
| "what's new", "check my inbox", "anything from X" | **C: What's new check** |
| Names one specific thread, "process this thread" | **D: One thread** |
| "the last 15 emails", "between Sept 17 and Sept 23" (a count or date range larger than a quick check) | **E: Backlog sweep** |
| "sync drafts to Gmail", "put pending drafts in my inbox" | **F: Sync reply drafts to Gmail** |
| A plain question about emails/deals/commitments/follow-ups/meetings, not a request to process anything | **G: Answer a question (read-only)** |

Modes A-E all run the exact same per-message procedure (below) -- they only
differ in how the target message(s) get selected and capped. Modes F and G
are their own separate procedures, further down.

## Hard rules (apply to every mode)

- Only ever use the one confirmed cos-sales-agent MCP connector's tools for
  CoS processing. **Never call `process_email`** -- always the granular
  tools, in this exact order, per message: `ingest_email` -> (you read and
  classify the message) -> `persist_email_analysis` -> `persist_context_delta`
  -> apply the Gmail label -> (`create_reply_draft` + Gmail `create_draft` +
  `set_reply_draft_gmail_id` if warranted, or `set_reply_withheld_reason` if
  a reply is needed but deliberately not drafted) -> `mark_email_completed`.
- Use the Gmail connector to *read*; to *label* a message (`create_label`/
  `label_message`); and — only immediately after `create_reply_draft`
  succeeds — to *create a draft* via `create_draft`. Never actually send,
  reply (dispatch), forward, delete, trash, or mark spam. Labeling and
  draft-creation are the only write actions ever permitted, and a draft must
  never be sent automatically by this skill.
- Skip (don't reprocess) any message already `COMPLETED` -- `ingest_email`'s
  own `already_completed` flag is the authoritative check.
- Never call Claude, OpenAI, or any other LLM API for classification -- you
  are the classifier, using your own reading of each email. Do not expose
  your reasoning/chain-of-thought in any tool call or stored data -- only
  the resulting structured fields.
- Never create a real calendar event. `persist_email_analysis` only ever
  proposes a `calendar_action` (`awaiting_approval` / `needs_clarification`).
- Never invent people, projects, commitments, follow-ups, meetings, or reply
  content that isn't genuinely present in a message.
- **Before creating any Project or Opportunity, check for an existing one
  with the same name/entity first** (e.g. via `get_project_summary` /
  `list_projects` / `list_opportunities`). Reuse it rather than creating a
  duplicate just because one message's own mention omitted an `org`/entity
  value an earlier message already supplied.
- A failure on one message never stops a multi-message run (modes A/C/E) --
  record it under "failed" and continue. A failure *does* stop a single-
  thread run (mode D) immediately, since later messages in that thread
  depend on the same context.

## The per-message procedure (modes A-E)

For each message, in order (oldest to newest within a batch):

1. **Call `ingest_email`** (map Gmail id -> `message_id`, threadId ->
   `thread_id`, sender/recipients -> `from`/`to`/`cc`, date -> `timestamp`,
   include `in_reply_to`/`references` if available).
   - If it returns `already_completed: true`, record under "skipped" and
     move on -- never reprocess.
   - Otherwise, read `previous_context`/`thread_timeline` before reasoning.
2. **Read the message yourself and classify it**:
   - **Sales vs Not Sales**: Sales only for genuine activity tied to a
     specific prospect/customer (active negotiation, pricing/quote,
     proposal, contract discussion, pilot/POC, renewal, expansion, purchase
     discussion, or other concrete deal evidence). Not Sales for an
     unsolicited inbound vendor pitch, generic marketing, a newsletter,
     purely informational content, or generic sales language with no
     genuine deal evidence -- "Sales" means the user's own pipeline, never
     someone else's pitch to the user.
   - **The six labels** (`label_applied`, exactly one, re-evaluated per
     message, never inherited from the thread): `Needs reply: ASAP` (must
     respond, time-critical or a key relationship), `Needs reply` (must
     respond, not urgent), `Needs reply: mention` (a thread being read asks
     something by name), `Read only` (informational, nothing asked), `Delete`
     (meeting accept/decline notices, cold outreach with no prior
     relationship), `Undecided` (genuinely can't place it).
   - **Every commitment needs a `date_phrase` when one is stated or clearly
     implied.** `commitments_mentioned` entries must set `date_phrase` to
     the exact raw text phrase from the email (e.g. "Oct 6", "by Friday",
     "next week") whenever the email states or clearly implies a date --
     never leave it empty when a date is actually there. The deterministic
     code (`resolve_date_phrase`, called inside `persist_email_analysis`)
     converts this into the stored `committed_date` automatically; an empty
     `date_phrase` means that commitment will silently never show up as
     "due" on the dashboard, even though a real date was stated. Only omit
     `date_phrase` when the email genuinely states no date at all.
   - **Populate `people_mentioned` with real name/org/role evidence for
     EVERY person this email gives you a real name for** -- including the
     sender and recipients, not just people referenced in the body.
     Envelope-based resolution (automatic, from the raw From/To/CC headers)
     almost never has a real display name to work with, so a person who is
     never named in `people_mentioned` is permanently stuck showing just
     their email address on the dashboard. Whenever a signature block,
     greeting, self-introduction, or body text reveals someone's real name
     (and org/role, if stated), add a `MentionedPerson` entry for them with
     that `name`/`org`/`role_hint` -- even if envelope resolution will
     already create a Person for that same address regardless; the two are
     deduplicated automatically by email, and the mention is what actually
     supplies the name. Only ever use a name/org/role genuinely stated or
     clearly signed in the email -- never guess one.
   - Produce the full `EmailAnalysis` reflecting only what's actually in the
     message, then call `persist_email_analysis`. **If the result's
     `possible_missed_commitment` field is non-null**, re-read the email
     before moving on -- it means the body contains commitment-shaped
     language ("I'll...", "could you...") but `commitments_mentioned` came
     back empty. If there really is a commitment you missed, call
     `persist_email_analysis` again with the same analysis plus the missing
     commitment included -- commitments are deduped by normalized text +
     class + date within the thread, so people/projects/commitments already
     resolved the first time are reused, not duplicated, and only the new
     commitment gets created. If there genuinely isn't one, ignore the
     warning and continue; it's a heuristic, not proof.
   - **Research newly-discovered organizations.** If the same result's
     `new_organizations_needing_research` list is non-empty, for each entry
     use WebSearch (1-2 targeted queries, e.g. `"<name>" company industry`
     or `"<domain>" about`) to find out what the company does, then call
     `persist_organization_research` with whatever you found (industry,
     description, products_services, size_estimate, headquarters, website).
     If search turns up nothing useful, still call
     `persist_organization_research` with no fields set -- this marks the
     company "looked, found nothing" so it isn't re-surfaced on every future
     email about it. Do this before calling `mark_email_completed`.
   - **Update each referenced person's Customer Intelligence profile.** For
     each entry in the same result's `people_profile_context`, read what this
     email actually says about that person alongside their existing
     `profile_summary`/`recent_context`/`key_topics` (already included in the
     entry) and their organization's `org_description`/`org_industry` (also
     included). If this email adds anything substantive -- a new role, a new
     topic of discussion, a meaningful update to what you're working on
     together -- compose the FULL updated text (incorporating what was
     already there, never discarding it) and call `persist_person_profile`
     with whichever fields changed. A trivial email ("thanks, got it") with
     nothing new doesn't need a call at all -- this is judgment, not a rule.
     Do this before calling `mark_email_completed`.
   - Produce a bounded `ContextDelta` (only what this message changes -- a
     real object matching `app.context.models.ContextDelta`'s shape, never
     an empty/placeholder one) and call `persist_context_delta`.
3. **Apply the matching Gmail label.** Once per run (not per message), call
   `list_labels`; for each of the six label names not already present, call
   `create_label` with that exact string as `displayName` (plain text, no
   color needed) and keep a name -> label id map in memory. Per message,
   call `label_message` with this message's real Gmail id (never the
   canonical `EML-nnn`) and the label id matching its own `label_applied`.
4. **Decide on a reply.** If the message genuinely needs one, first check
   whether drafting is actually appropriate: phishing/spoofing red flags
   (sender domain mismatch, implausible contact details, unfilled template
   placeholders), a request for sensitive data (bank details, credentials,
   passwords, government IDs), or any other reason a drafted reply would be
   unsafe or premature.
   - If any apply: do not draft a committal reply -- skip the draft (or keep
     it strictly non-committal) and call `set_reply_withheld_reason` with
     the message_id and a concise, specific reason. Flag the suspicion in
     the final report.
   - Otherwise, draft a reasonable reply and call `create_reply_draft`.
     **Immediately after it succeeds**, create a matching draft directly in
     the user's own Gmail mailbox via `create_draft` (same subject/body,
     addressed as a reply within the original thread using the real Gmail
     message/thread ids so it nests correctly), then call
     `set_reply_draft_gmail_id` with the reply's `reply_id` and the Gmail
     draft id just returned, so the link is recorded.
   - Skip this entire step if no reply is warranted at all.
5. **Call `mark_email_completed`.**
6. **On failure** (modes A/C/E): record message id, timestamp, error under
   "failed"; continue to the next message. (Mode D: stop the whole run
   immediately instead.)

## Mode A: Exact-count backfill

Process exactly the requested count (default 50 if unstated) of the most
recent inbox messages (`in:inbox -in:drafts -in:sent`), enumerating
individual messages out of any threads. Cap and sort strictly oldest to
newest before the per-message loop. One-time, on-request -- never scheduled.

Report:
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
Needs reply but withheld (message id + reason, or "None"):
Gmail labels applied: <count, and any newly created label names, or "None">
```

## Mode B: One named email

Identify the one email the user means (search Gmail if given a sender/
subject/"latest"). If ambiguous (matches more than one, or none), ask rather
than guess. Run the per-message procedure on exactly that message.

Report in plain English: who it's from, what it's about, the label applied
and the matching Gmail label, what was created (people, commitments,
follow-ups, meeting, project), whether a reply draft was generated (Mongo +
Gmail) or withheld (and why). If already `COMPLETED`, say so plainly and
stop -- don't re-describe stale results as new.

## Mode C: What's new check

Search Gmail for the message(s) matching the request (recent inbox, a
sender, a subject). If the search surfaces a thread, open it (`get_thread`)
and enumerate its actual `messages[]` -- never treat a thread's own `id`
field as a message id. Check each candidate against `search_emails`/
`list_processed_emails` to see what's actually unprocessed before deciding
what's "new." Run the per-message procedure on each new candidate (normally
one at a time unless explicitly asked for more).

Report in plain English per email: what came in, who it's from, whether it
was Sales, what was created, the Gmail label applied, whether a reply was
drafted/withheld (and why).

## Mode D: One thread

Resolve the thread (a `THR-*` id, or enough context to resolve to exactly
one -- ask if ambiguous). Get every message (`get_thread`). Determine which
are already processed (`search_emails`/`list_processed_emails`, matching by
sender/subject/timestamp). Sort unprocessed messages strictly oldest to
newest. Run the per-message procedure on each, **stopping immediately on the
first failure** (later messages depend on the same context, unlike A/C/E).

Report:
```
Thread <THR-id> processed.
Processed (newly completed): <message ids, one-line summary each>
Skipped (already completed): <message ids, or "None">
Failed: <message id + error, if stopped early -- "None" otherwise>
Remaining unprocessed (if stopped early): <count, or "None">
Gmail drafts created (awaiting your review/send): <message ids, or "None">
Needs reply but withheld (message id + reason, or "None"):
Gmail labels applied: <count, and any newly created label names, or "None">
```

## Mode E: Backlog sweep (date range or larger count)

Resolve exactly one of: a count (most recent N) or a date range, from the
request -- never guess; ask if neither is clear. Search Gmail matching that
scope. **Cap at 20 messages per run**: if more candidates exist, process the
oldest 20 within scope and tell the user how many remain, offering to
continue. Run the per-message procedure on the capped batch, oldest to
newest.

Report:
```
Backlog sweep @ <current time>
Scope: <count or date range as resolved>
Candidates found: <N>
Processed this run (capped at 20): <count>
Completed (newly processed): <count>
Skipped (already completed): <count>
Failed: <count, with message_id + error if any>
Gmail drafts created (awaiting your review/send): <message ids, or "None">
Needs reply but withheld (message id + reason, or "None"):
Gmail labels applied: <count, and any newly created label names, or "None">
Remaining in scope (if any): <count> -- say the word to continue
```

## Mode F: Sync existing reply drafts to Gmail

For a reply draft that already exists in MongoDB but was created *without*
the immediate Gmail-draft-creation step (e.g. from before that was wired
in). Never invents a recipient/subject/body, never changes a draft's
approval status, never sends anything.

1. Call `list_reply_drafts` with `status="awaiting_approval"`.
2. Filter out any draft that already has a non-null `gmail_draft_id`. If
   nothing remains, report "Nothing to sync" and stop.
3. Cap the remainder at 10 per run (oldest first by `created_at`); note how
   many are left over, if any.
4. For each draft in the capped batch:
   - Call `get_thread` with the draft's `thread_id`; find the message whose
     `message_id` equals the draft's `source_email_id`. No match -> skip,
     record why, never guess a recipient.
   - Recipient = that message's `from.email`. `replyToMessageId` = that
     message's `source_message_id` (create without it, noting so, if it's
     `None`).
   - Call `create_draft` with `to=[recipient]`, the draft's exact stored
     `subject`/`body`, and `replyToMessageId` when available. Plain text
     only.
   - Call `set_reply_draft_gmail_id` with this `reply_id` and the returned
     Gmail draft id.
5. Report: how many synced, how many skipped and why, whether each was
   threaded, how many remain beyond the cap.

## Mode G: Answer a plain-language question (read-only)

For a question about the inbox/deals/commitments/follow-ups/meetings that
isn't a request to process anything -- e.g. "what do I need to follow up
on?", "what's outstanding with Acme?", "any meetings coming up?". Never
calls `process_email` or any write tool in this mode.

- Use the cos-sales-agent connector's read tools only: `list_processed_emails`,
  `search_emails`, `get_thread`, `list_people`, `list_projects`,
  `list_opportunities`, `list_commitments`, `list_follow_ups`,
  `list_meetings`, `list_organizations`, `get_project_summary`,
  `get_company_summary`, `lookup_knowledge`, or `ask_question` directly for a
  natural executive question. `get_company_summary` takes a canonical org_id
  (ORG-xxx), not a company name -- if a question names a company by name
  ("what's outstanding with Acme?"), call `list_organizations(name_contains=
  "Acme")` first to get its org_id. Use multiple tools when the question
  needs cross-entity reasoning (e.g. "what have we promised this customer?"
  -> `list_organizations` -> `get_company_summary` -> `list_commitments`).
- Ground every claim in what a tool actually returned. Never invent a fact,
  date, meeting confirmation, commitment, or relationship. When evidence is
  incomplete, say so plainly ("I couldn't confirm...", "the available data
  doesn't establish...") rather than guessing.
- Distinguish meeting/commitment *states* honestly: a meeting mentioned in
  an email is not a confirmed calendar event; something inferred is not the
  same as something explicitly stated; don't convert a proposal into a
  confirmation.
- Answer in plain business language -- no tool names, no "MongoDB," no raw
  JSON, no collection names. A short summary, key points, and open items
  only when relevant.
- If something actionable surfaces (a reply that could be sent, a meeting
  that could be scheduled), present it as a proposal and wait for explicit
  approval -- never act on a question alone.

## Organization research catch-up and dedup (on-demand only)

These are never run automatically as part of ingesting an email -- only when
explicitly asked to catch up on research or check for duplicate organizations.

- **`list_unresearched_organizations`**: call when asked to "catch up on
  company research" -- for each organization returned, research and persist
  exactly as in the per-message step above.
- **`preview_duplicate_organization_candidates`**: call when asked to check
  for duplicate companies. Each candidate names two organization ids and why
  they were flagged (`name_match` or `domain_cross_match`) -- never merge
  automatically from this list alone. Review each pair (WebSearch if
  genuinely ambiguous -- e.g. confirming two similarly-named companies are
  actually the same legal entity), then call `merge_organization_records`
  only for pairs you're genuinely confident about.

## What this skill never does, in any mode

- Never sends, replies (dispatches), or forwards an email.
- Never deletes, trashes, or marks anything spam.
- Never creates a real calendar event.
- Never modifies a reply draft's approval status outside the normal
  approve/reject/edit flow this app already provides elsewhere.
- Never calls an LLM API for classification -- you are always the
  classifier.
