---
name: email-labelling
description: Stage 3 (final) of a 3-stage email-processing sequence (email-ingestion -> context-building -> email-labelling). Decides label_applied (criteria included below, self-contained), persists it, applies the matching REAL Gmail label via the Gmail connector, and marks the email completed. Requires context-building to have already run for the message_id. This skill changes your actual Gmail inbox -- a label becomes visible there.
---

# Email Labelling (stage 3 of 3, final)

Last of three deliberately separated stages: **email-ingestion** ->
**context-building** -> **email-labelling** (this skill). This skill's job
is deciding what a human should do about this email -- the `label_applied`
triage decision -- persisting it, applying the matching label in the real
Gmail inbox, and closing the email out. **Running this skill changes your
actual Gmail inbox** (a label becomes visible on the message there) -- this
is not a database-only test step.

## Precondition

Requires `context-building` (stage 2) to have already run for this
`message_id`. You also need the email's **real Gmail message id** (the one
`email-ingestion`, stage 1, originally read from Gmail and mapped as
`message_id` before `ingest_email` canonicalized it to an `EML-nnn` id) --
`label_message` needs the real Gmail id, never the canonical one.

## Hard rules

- **Decide `label_applied` using the criteria in "The six label values and
  criteria" below.**
- **Use the Gmail connector only to label** -- never send, reply, forward,
  delete, trash, or mark spam from this skill.
- **Never call `create_reply_draft`** -- deciding to reply is a separate
  concern from labelling.
- **Never call Claude, OpenAI, or any other LLM API.**

## The six label values and criteria

`label_applied` is exactly one of six values, re-evaluated fresh for every
email -- never inherited from how a prior message in the same thread was
labeled. Every value carries a `"1. "` prefix (a display-order marker, not
a priority ranking). Judge by whether the sender's intent is identifiable
from the message itself or its available thread context -- never by
message length alone. A short message with a clear topic is not
automatically `Undecided`; a long message with no identifiable ask is not
automatically `Needs reply`.

1. **`1. Needs reply: ASAP`** -- must respond, time-critical or a key
   relationship.
2. **`1. Needs reply`** -- a question or request has a reasonably
   identifiable purpose, from the message itself or its available thread
   context. A SHORT message is NOT automatically `Undecided` -- "Thoughts
   on the attached pricing proposal?" has a clear, identifiable purpose
   (feedback on that proposal) despite being one line, and is
   `Needs reply`.
3. **`1. Needs reply: mention`** -- a thread being read asks something by
   name.
4. **`1. Read only`** -- informational, nothing asked.
5. **`1. Delete`** -- meeting accept/decline notices, cold outreach with no
   prior relationship.
6. **`1. Undecided`** -- the message is too ambiguous to determine the
   appropriate action, even after checking thread context. This means
   genuinely CONTEXTLESS, not merely short: a standalone "Thoughts?" with
   no accessible prior message or stated topic (nothing to know what's
   being asked about) is `Undecided` -- but the same word attached to a
   clear subject ("Thoughts on the attached pricing proposal?") is
   `Needs reply`, not `Undecided`, despite being equally short.

## Step by step, per email, in order

1. **Decide `label_applied`** using the criteria above (one of the six
   values, judged by identifiable sender intent).
2. **Re-supply the same `EmailAnalysis` fields `context-building` already
   decided for this email** (the same `people_mentioned`,
   `projects_mentioned`, `commitments_mentioned`, `meetings_mentioned`,
   `personal_items_mentioned`, `person_facts_mentioned`, `facts`, and flat
   fields from stage 2), now with `label_applied` set to your stage-1
   decision. **This re-supply matters**: `persist_email_analysis` always
   overwrites the email's `entities_referenced`/`goal_pillar`/`label_applied`
   with whatever this specific call produces -- if you called it with an
   empty analysis just to set `label_applied`, it would silently erase the
   record of what stage 2 created (the underlying Person/Project/Commitment
   documents themselves are untouched, but the email's own metadata would
   lose track of them). If you don't have stage 2's exact `EmailAnalysis`
   object still in this conversation, re-classify the email for entities
   first (same as `context-building` stage 1) before adding `label_applied`,
   rather than calling `persist_email_analysis` with a thin object.
3. **Call `persist_email_analysis(message_id, analysis)`** with that
   complete, re-supplied object.
4. **Apply the matching Gmail label.** Once per run (not per message), call
   `list_labels`; for each of the six label names not already present, call
   `create_label` with that exact string as `displayName` (plain text, no
   color needed) and keep a name -> label id map in memory. Per message,
   call `label_message` with this message's **real Gmail id** (the one from
   stage 1, never the canonical `EML-nnn`) and the label id matching its own
   `label_applied`.
5. **Call `mark_email_completed(message_id)`** (using the canonical
   `message_id`, not the Gmail id). This verifies `entities_referenced` is
   actually populated before closing the email out -- the final step of the
   whole 3-stage sequence.

## Failure handling

If one email fails, record which message_id failed and why, then continue
to the next email.

## Reporting back

After the batch, report in plain English:

- how many emails were labelled, with the final `label_applied` value for
  each (message_id -> label)
- Gmail labels applied: count, and any newly created label names (or "None")
- how many were successfully marked completed
- any failures, by message_id and reason

Do not dump raw JSON/tool output. For example: "Labelled and completed 8
emails: 2 Needs reply: ASAP, 3 Needs reply, 1 Needs reply: mention, 1 Read
only, 1 Delete. Gmail labels applied: 8 (created 2 new labels: 'Needs reply:
mention', 'Delete'). No failures."
