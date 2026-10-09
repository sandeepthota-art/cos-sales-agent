---
name: six-label-classification
description: Criteria for choosing exactly one of the six label_applied values (Needs reply: ASAP / Needs reply / Needs reply: mention / Read only / Delete / Undecided) for an email, judged by whether the sender's intent is identifiable -- never by message length alone. Reference this from any skill that builds an EmailAnalysis object and has to decide label_applied for real inbox triage. Do not reference this from context-building, which deliberately skips triage labelling and leaves label_applied at its schema default.
---

# Six-Label Classification

`label_applied` on an `EmailAnalysis` object (`app/analysis/schemas.py`) is
exactly one of six values, re-evaluated fresh for every email -- never
inherited from how a prior message in the same thread was labeled. Every
value carries a `"1. "` prefix (a display-order marker, e.g.
`"1. Needs reply: ASAP"`), not a numbering of priority.

## The six labels

1. **`Needs reply: ASAP`** -- must respond, time-critical or a key
   relationship.
2. **`Needs reply`** -- a question or request has a reasonably identifiable
   purpose, from the message itself or its available thread context. A SHORT
   message is NOT automatically `Undecided` -- "Thoughts on the attached
   pricing proposal?" has a clear, identifiable purpose (feedback on that
   proposal) despite being one line, and is `Needs reply`.
3. **`Needs reply: mention`** -- a thread being read asks something by name.
4. **`Read only`** -- informational, nothing asked.
5. **`Delete`** -- meeting accept/decline notices, cold outreach with no
   prior relationship.
6. **`Undecided`** -- the message is too ambiguous to determine the
   appropriate action, even after checking thread context. This means
   genuinely CONTEXTLESS, not merely short: a standalone "Thoughts?" with no
   accessible prior message or stated topic (nothing to know what's being
   asked about) is `Undecided` -- but the same word attached to a clear
   subject ("Thoughts on the attached pricing proposal?") is `Needs reply`,
   not `Undecided`, despite being equally short.

## The one rule that governs all six

Judge by whether the sender's intent is identifiable from the message itself
or its available thread context -- never by message length alone. A short
message with a clear topic is not automatically `Undecided`; a long message
with no identifiable ask is not automatically `Needs reply`.

## Where this applies

Any skill that builds an `EmailAnalysis` object for real inbox triage should
use these criteria to set `label_applied`:

- **`gmail-initial-ingest`** -- applies the matching real Gmail label right
  after deciding this value.
- **`email-labelling`** (stage 3 of the email-ingestion -> context-building
  -> email-labelling sequence) -- persists this value to MongoDB via
  `persist_email_analysis`, then also applies the matching real Gmail label,
  same as `gmail-initial-ingest` does.

**`context-building`** (stage 2 of that same sequence) deliberately does
NOT use this skill -- it builds entities and knowledge, and leaves
`label_applied` at its schema default (`"1. Undecided"`), since deciding
the label is `email-labelling`'s job alone.
