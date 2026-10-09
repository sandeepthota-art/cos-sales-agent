---
name: context-building
description: Stage 2 of a 3-stage email-processing sequence (email-ingestion -> context-building -> email-labelling). Resolves canonical entities (people/organizations/projects/opportunities/commitments/follow_ups/meetings/personal_items), extracts knowledge, and updates the thread's rolling ThreadContext -- via persist_email_analysis and persist_context_delta. Deliberately leaves label_applied at its schema default; deciding that is email-labelling's job, not this skill's. Requires email-ingestion to have already run for the message_id.
---

# Context Building (stage 2 of 3)

Second of three deliberately separated stages: **email-ingestion** ->
**context-building** (this skill) -> **email-labelling**. This skill's job
is building up everything the system now knows as a result of this email --
canonical entities, knowledge facts, and the thread's rolling narrative
context. It does not decide what a human should do about the email; that's
stage 3.

## Precondition

Requires `email-ingestion` (stage 1) to have already run for this
`message_id`. If `persist_email_analysis` raises because `ingest_email`
hasn't run yet, run the `email-ingestion` skill for this email first, then
retry.

## Hard rules

- **Never decide `label_applied`.** Leave it unset on the `EmailAnalysis`
  object you build -- its schema default (`"1. Undecided"`) applies
  automatically. Do not use the `six-label-classification` skill's criteria
  here; that's `email-labelling`'s job, in stage 3.
- **Never call `create_label`, `label_message`, `create_reply_draft`, or
  `mark_email_completed`** from this skill -- `mark_email_completed` belongs
  to the end of stage 3, once labelling has also run.
- **Never call Claude, OpenAI, or any other LLM API** to do the
  classification. You read the email and classify it yourself.
- Do not invent entities or relationships -- if nothing in a field applies,
  leave it empty. Evidence-gate `person_facts_mentioned` (real, resolvable
  names only, never guessed).
- **Keep the `EmailAnalysis` object you build.** `email-labelling` (stage 3)
  needs to re-supply these same fields when it adds `label_applied`, to
  avoid overwriting the `entities_referenced` this stage just recorded (see
  that skill's own notes for why).

## Step by step, per email, in order (oldest to newest)

1. **Classify the email for entities and knowledge.** Decide `goal_pillar`
   (Sales vs. Not-Sales: concrete negotiation/pricing/proposal/pilot/renewal/
   expansion/purchase activity tied to a specific org, never just a mention
   of a company or generic sales language), `people_mentioned`,
   `projects_mentioned` (only when genuinely Sales and evidence-backed --
   never invented), `commitments_mentioned`, `meetings_mentioned`,
   `personal_items_mentioned`, `person_facts_mentioned`, and the flat
   `facts`/`requirements`/`pain_points`/`buying_signals`/`objections`/
   `competitors`/`pricing_mentions` fields. Build the `EmailAnalysis` object
   -- leave `label_applied` unset.
2. **Call `persist_email_analysis(message_id, analysis)`** (no
   `skip_knowledge` -- this stage builds the full knowledge layer too).
   Read the result:
   - `entities_referenced` -- what got created/reused this email.
   - `possible_missed_commitment` -- non-authoritative heuristic; if it
     fires, re-read the email, add the missed commitment to `analysis`, and
     call `persist_email_analysis` again (idempotent, dedup is automatic).
   - `new_organizations_needing_research` -- WebSearch each one, then call
     `persist_organization_research`.
   - `people_profile_context` -- if an entry adds something substantive
     about who this person is, call `persist_person_profile`.
3. **Build a `ContextDelta`** for this email: a bounded change to the
   thread's `ThreadContext` -- only what this email adds or changes
   (`summary` if it changed, `participants_added`/`removed`,
   `company_updates`/`opportunity_updates`/`pricing_updates` as flat
   key/value changes, and one `ListFieldDelta` (`added`/`removed`) per list
   field: `requirements`, `pain_points`, `products_discussed`,
   `competitors`, `objections`, `buying_signals`, `decisions`,
   `commitments`, `open_questions`, `next_actions`, `meetings`). Never
   reconstruct or echo the full accumulated context -- only the delta.
4. **Call `persist_context_delta(thread_id, message_id, delta)`.**

## Failure handling

If one email fails, record which message_id failed and why, then continue
to the next email.

## Reporting back

After the batch, report in plain English:

- how many emails were processed
- total counts of each entity type created/reused across the batch (people,
  organizations, projects, opportunities, commitments, follow_ups, meetings,
  personal_items) and whether any knowledge_items were created
- how many context deltas were applied, and a one-line sense of what each
  thread's context now reflects
- any `possible_missed_commitment` flags raised, by message_id
- any failures, by message_id and reason

Do not dump raw JSON/tool output.
