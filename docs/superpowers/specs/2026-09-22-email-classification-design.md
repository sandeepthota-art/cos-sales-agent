# CoS Sales Agent — Deterministic Email Classification (Phase 5)

Status: draft design, pending user review.

## 1. Purpose

Classify every incoming email against a fixed taxonomy — business category, action
type, priority, and optional labels — using **only deterministic, local Python
rules**. No external classification API, no new SaaS dependency, no new API key, and
no LLM call of any kind. This spec supersedes the "optional LLM-assist" idea raised
during brainstorming: that path is explicitly out of scope here.

This is additive: one new pipeline stage, a new `app/classification/` package, and
one new embedded field on the `emails` document. It does not change thread
resolution, context building, entity resolution, commitment/follow-up/meeting
extraction, reply-draft generation, or calendar-action detection.

## 1.1 End-to-End Flow

```text
Gmail / folder source (unchanged)
  |
  v
Ingestion (app.pipeline.ingest_raw_email) -- unchanged
  |
  v
Normalization (app.email.normalizer) -- unchanged
  |
  v
Thread resolution (app.email.threading, app.pipeline.resolve_and_persist_thread) -- unchanged
  |
  v
Existing analysis (real LLM in run_pipeline, or an externally-supplied
EmailAnalysis in persist_email_analysis) -- unchanged
  |
  v
Entity/commitment/follow-up/meeting resolution (app.pipeline._process_entities) -- unchanged
  |
  v
*** NEW: Deterministic Classification (app.classification.classifier) ***
  - pure function, no I/O, no network call
  - reads: envelope (sender/domain), subject, body, thread metadata,
    already-resolved entities/commitments/meetings, existing MongoDB history
    for the sender
  - writes: nothing itself -- returns an EmailClassification value
  |
  v
Persist classification onto the email document (EmailRepository.set_classification)
  |
  v
(existing, unchanged) Reply draft generation -> Meeting/calendar detection
```

Both call sites (`run_pipeline` in `app/pipeline.py` and `persist_email_analysis` in
`app/mcp/tools.py`) call the exact same `classify_email()` function, immediately
after `_process_entities()` returns — mirroring how those two call sites already
share `_process_entities()` itself.

## 2. Taxonomy (fixed, closed sets — binding)

```python
# app/classification/schemas.py
from typing import Literal

BusinessCategory = Literal[
    "customer", "prospect", "sales", "project", "internal", "finance",
    "vendor", "recruiting", "personal", "operations", "informational", "other",
]

ActionType = Literal[
    "action_required", "reply_required", "follow_up_required", "meeting",
    "approval_required", "information_only", "no_action",
]

Priority = Literal["P1", "P2", "P3", "P4"]

OptionalLabel = Literal[
    "customer", "prospect", "sales", "project", "implementation", "pricing",
    "contract", "meeting", "follow_up", "approval", "internal", "urgent", "fyi",
]
```

These are the exact 12 / 7 / 4 / 13 values the user specified. Nothing in this
design ever produces a value outside these sets — see §7 for the enforcement
mechanism (it's structural, not a validation step that can be skipped).

## 3. `EmailClassification` model

```python
# app/classification/schemas.py (continued)
from pydantic import BaseModel, Field


class EmailClassification(BaseModel):
    business_category: BusinessCategory
    action_type: ActionType
    priority: Priority
    labels: list[OptionalLabel] = Field(default_factory=list)
    confidence: float
    needs_review: bool
    rationale: list[str] = Field(default_factory=list)
    classified_at: str  # ISO 8601, UTC -- set by the caller, not by classify_email itself
```

`rationale` is a short list of which rule(s) fired (e.g.
`["sender_domain_matches_agent_domain", "no_open_commitments"]`) — this is the
explainability requirement: any classification can be explained by reading this
list, with no need to re-derive it from the raw email.

`needs_review` is a plain boolean, not folded into `confidence` — see §6.

## 4. Deterministic rules

All rules live in `app/classification/rules.py`, one small pure function per rule
group, each taking already-normalized inputs (never raw strings) — mirroring
`app/calendar/detector.py`'s existing shape (module-level compiled regex constants,
pure functions, no I/O). `app/classification/classifier.py` orchestrates them into
one `EmailClassification`.

### 4.1 Business category

Evaluated in this fixed priority order — first match wins:

| Order | Rule | Signal | Category |
|---|---|---|---|
| 1 | `_is_internal` | sender domain == agent's own domain (`settings.agent_email` domain, e.g. `databeat.io`) | `internal` |
| 2 | `_is_finance` | sender local-part or domain matches a configured finance allowlist (e.g. `finance@`, `billing@`), or subject/body hits `\b(invoice|payment|billing|receipt|outstanding balance)\b` | `finance` |
| 3 | `_is_recruiting` | subject/body hits `\b(candidate|resume|r[ée]sum[ée]|interview process|job opening|open role)\b` **and** no existing open commitment/project tied to this thread | `recruiting` |
| 4 | `_is_vendor` | sender domain appears in a configured vendor-domain allowlist (e.g. known SaaS senders), or subject/body hits `\b(your (subscription|plan|account)|api key|trial (ending|expiring)|renewal notice)\b` | `vendor` |
| 5 | `_is_existing_customer` | sender's resolved `Person` (via existing `resolve_person`/`PersonRepository`) has ≥1 prior thread with a `commitment` whose `class` is `"mine"` (i.e. we owe them something — a live engagement, not just a lead) | `customer` |
| 6 | `_is_prospect` | sender's resolved `Person` has prior/current threads but no `class="mine"` commitment yet, **and** `goal_pillar`/analysis signals (`buying_signals`, `pricing_mentions`) are non-empty | `prospect` |
| 7 | `_is_sales_generic` | any commitment/meeting/pricing signal present but rules 5-6 didn't resolve a specific relationship stage | `sales` |
| 8 | `_is_project` | subject/body mentions an existing resolved `Project` (`resolve_project`'s existing normalized-name match against `ProjectRepository`) | `project` |
| 9 | `_is_operations` | subject/body hits `\b(PTO|holiday schedule|onboarding|payroll|policy update)\b` | `operations` |
| 10 | `_is_informational` | no commitments, no meetings, no questions (`"?" not in body`), sender is a no-reply/bulk address (`no-?reply@`, `newsletter@`, `hello@`) | `informational` |
| 11 | fallback | none of the above matched | `other` |

`personal` is intentionally **not** a fully automatic rule (see §5 — it's the one
category this design flags as needing a human-maintained allowlist, since there is
no deterministic body signal that reliably distinguishes "personal" from "internal"
or "informational").

### 4.2 Action type

Reuses two already-existing, already-tested functions instead of re-implementing
their logic:

| Rule | Reused from | Maps to |
|---|---|---|
| Has an unresolved calendar meeting (`detect_meeting().meeting_detected` or `needs_clarification`) | `app.calendar.detector.detect_meeting` | `meeting` |
| Has ≥1 open commitment where `owed_by == agent` (`class in ("mine", "owed_to_me")`) from this email's own `entities_referenced.commitments` | `app.pipeline._process_entities`'s already-computed output | `action_required` |
| Has ≥1 `follow_up` newly derived from this email | same `entities_referenced.follow_ups` | `follow_up_required` |
| Subject/body hits `\b(please approve|awaiting your approval|sign[- ]off|needs your approval)\b` | new regex, `rules.py` | `approval_required` |
| `needs_reply(analysis, email)` is true and none of the above fired | `app.replies.drafter.needs_reply` (unchanged, reused as-is) | `reply_required` |
| Sender is a no-reply/bulk address, or body has no question and no commitments | new regex + existing `entities_referenced` emptiness check | `information_only` |
| None of the above | — | `no_action` |

Evaluated top-to-bottom, first match wins — an email can only have one
`action_type`, matching the taxonomy's design (it's a single-select field, unlike
`labels`).

### 4.3 Priority

A deterministic weighted score, not a single rule — this is the one dimension the
brainstorming pass already flagged as needing tuning once real distribution data is
visible (§9 covers how you'll measure that).

```python
def _priority_score(email, analysis, entities_referenced, classification_so_far) -> int:
    score = 0
    if re.search(r"\b(urgent|asap|immediately|critical)\b", email.body, re.I):
        score += 3
    if classification_so_far.action_type in ("approval_required", "action_required"):
        score += 2
    if _earliest_committed_date_within(entities_referenced, days=2):
        score += 3
    elif _earliest_committed_date_within(entities_referenced, days=7):
        score += 1
    if classification_so_far.business_category in ("customer", "sales"):
        score += 1
    return score

# score >= 6 -> P1, >= 4 -> P2, >= 2 -> P3, else P4
```

`_earliest_committed_date_within` reads `entities_referenced.commitments`'
`committed_date` (already resolved by the existing `resolve_commitment`/
`resolve_date_phrase` machinery — no new date parsing here).

### 4.4 Optional labels

Independent, order-doesn't-matter, each its own small boolean rule; multiple can
fire. All reuse already-extracted signals — no new extraction:

| Label | Rule |
|---|---|
| `customer` / `prospect` / `sales` / `project` / `internal` | mirrors whichever `business_category` rule fired (§4.1) — not independently re-derived |
| `pricing` | `analysis.pricing_mentions` non-empty |
| `contract` | subject/body hits `\b(contract|agreement|MSA|SOW|order form)\b` |
| `implementation` | subject/body hits `\b(implementation|onboarding|rollout|go-live)\b` |
| `meeting` | mirrors the `action_type == "meeting"` rule, or `entities_referenced.meetings` non-empty |
| `follow_up` | `entities_referenced.follow_ups` non-empty |
| `approval` | mirrors the `action_type == "approval_required"` rule |
| `urgent` | the same urgency regex from §4.3 |
| `fyi` | `action_type == "information_only"` |

After assembly: `labels = sorted(set(labels))` — dedupes structurally (§8), and a
label whose triggering condition contradicts `action_type == "no_action"` (e.g.
`"urgent"` or `"approval"` on a `no_action` email) is dropped and the drop is
recorded in `rationale` (e.g. `"dropped label 'urgent': contradicts action_type=no_action"`).

## 5. What's genuinely ambiguous (no LLM assist — explicit local-only fallback)

Per your instruction, there is no LLM-assist path at all. For the cases flagged as
ambiguous during brainstorming (`customer` vs. `prospect` vs. `sales`, `personal`,
close P2/P3 priority calls), the local-only fallback is:

1. Run every rule in priority order as normal (§4.1–§4.4).
2. If the winning `business_category` rule is a "weak" one (rules 6, 7, 9, 10, or
   the `other` fallback — configured as a `_WEAK_CATEGORY_RULES` set in
   `rules.py`, not a magic number scattered around), or the priority score sits at
   an exact tie boundary, set `needs_review = True` and lower `confidence`
   accordingly (§6).
3. `personal` is reachable **only** via a small, explicit, human-maintained
   allowlist of personal sender addresses/domains in `Settings`
   (`personal_sender_allowlist: list[str] = []`, empty by default) — it is never
   inferred from body content. This keeps `personal` fully deterministic and fully
   explainable, at the cost of requiring the operator to opt senders in.

Nothing here calls an LLM, makes a network request, or requires an API key. A
`needs_review = True` email is not blocked or hidden — it's classified and
persisted like any other, just flagged for a human to glance at (see §12 for what
consumes that flag).

## 6. Confidence scoring

Purely deterministic, computed from how many rules agreed and how strong the
matched rule was — no learned model, no external call:

```python
def _confidence(category_rule_strength: float, action_rule_strength: float,
                 priority_score: int, label_count: int) -> float:
    base = (category_rule_strength + action_rule_strength) / 2
    # A very high or very low label count is itself a weak signal that the
    # deterministic rules didn't cleanly separate this email.
    if label_count == 0 or label_count > 4:
        base -= 0.1
    return round(max(0.0, min(1.0, base)), 2)
```

Each rule in §4.1/§4.2 is tagged with a strength (`1.0` for an unambiguous match
like `_is_internal`'s exact domain comparison, `0.6` for a keyword-only match like
`_is_vendor`'s regex, `0.4` for the `other`/fallback case). This is a small,
explicit table in `rules.py` (`_RULE_STRENGTH: dict[str, float]`), not a formula
that needs re-deriving per rule.

`needs_review` is set `True` whenever `confidence < settings.classification_confidence_threshold`
(new setting, default `0.6`, following the exact pattern of every other tunable in
`app/config/settings.py`) **or** whenever §5's weak-rule/tie-boundary condition
fires — whichever triggers first. A low-confidence email's `action_type` is never
silently downgraded to `no_action`; §4.2's rule order already means `no_action`
only wins when nothing else matched, so a low-confidence email keeps whatever
`action_type` it earned and simply carries the review flag alongside it.

## 7. Taxonomy enforcement (why an invalid value can't happen)

This is structural, not a runtime check to remember: `EmailClassification`'s fields
are typed as the `Literal[...]` unions from §2. `classify_email()` can only
construct that Pydantic model by assigning one of those literal strings — Python's
own type system (backed by Pydantic's validation on construction) rejects
anything else at the moment the object is built, inside the classifier itself,
before it ever reaches MongoDB. There is no separate "validate the taxonomy" step
to skip, because there's no code path that can produce an `EmailClassification`
with an out-of-taxonomy value in the first place. (This is the same mechanism
`EmailAnalysis.label_applied`'s existing `Literal[...]` already uses today — see
`app/analysis/schemas.py`.)

## 8. Preventing duplicate/conflicting labels

- **Duplicates**: `labels = sorted(set(labels))` after assembly — a `list[str]`
  built from a `set` cannot contain a repeat, structurally.
- **Conflicts**: one explicit post-assembly check,
  `_drop_contradictory_labels(action_type, labels) -> tuple[list[str], list[str]]`
  (returns the filtered labels plus the rationale strings for anything dropped),
  called once at the end of `classify_email()`. The only contradiction rule for
  now: a label from `{"urgent", "approval", "meeting", "follow_up"}` present
  alongside `action_type == "no_action"` or `"information_only"` is dropped (§4.4).
  New contradiction rules are added to this one function as they're discovered —
  not scattered across the individual label rules.

## 9. MongoDB schema

No new collection — embedded directly on the existing `emails` document, exactly
matching how `goal_pillar`/`label_applied`/`confidence` are already stored (see
`app/database/repositories.py: EmailRepository.set_entity_metadata`):

```python
# app/database/repositories.py -- new method on EmailRepository
def set_classification(self, message_id: str, classification: dict) -> None:
    self._collection.update_one(
        {"message_id": message_id},
        {"$set": {"classification": classification}},
    )
```

Resulting document shape (added field only; every other field on `emails` is
unchanged):

```json
{
  "message_id": "...",
  "...": "... (unchanged existing fields) ...",
  "classification": {
    "business_category": "customer",
    "action_type": "action_required",
    "priority": "P1",
    "labels": ["pricing", "contract", "meeting"],
    "confidence": 0.82,
    "needs_review": false,
    "rationale": ["sender_has_open_mine_commitment", "urgent_keyword_hit"],
    "classified_at": "2026-09-22T21:30:00Z"
  }
}
```

New indexes (`app/database/indexes.py`), added only because §12 names concrete
queries that need them:

```python
db.emails.create_index("classification.business_category")
db.emails.create_index("classification.action_type")
db.emails.create_index("classification.priority")
db.emails.create_index("classification.needs_review")
```

## 10. New `Settings` fields

```python
# app/config/settings.py
classification_confidence_threshold: float = 0.6
personal_sender_allowlist: list[str] = []
finance_sender_allowlist: list[str] = ["finance@", "billing@", "ap@"]
vendor_sender_domains: list[str] = []  # operator-populated; empty list means the
                                        # vendor rule falls back to keyword-only matching
```

Follows the existing plain-field pattern used by every other tunable in this file
(e.g. `email_limit`, `ingestion_interval_minutes`).

## 11. Files/classes/functions touched

| File | Change |
|---|---|
| `app/classification/__init__.py` (new) | empty, matches sibling packages |
| `app/classification/schemas.py` (new) | `BusinessCategory`, `ActionType`, `Priority`, `OptionalLabel`, `EmailClassification` |
| `app/classification/rules.py` (new) | one pure function per rule group (§4.1–§4.4), `_RULE_STRENGTH`, `_WEAK_CATEGORY_RULES`, compiled regex constants |
| `app/classification/classifier.py` (new) | `classify_email(email, analysis, entities_referenced, settings, person_history) -> EmailClassification` — orchestrates §4–§8 |
| `app/database/repositories.py` | `EmailRepository.set_classification()` |
| `app/database/indexes.py` | 4 new indexes (§9) |
| `app/config/settings.py` | 4 new fields (§10) |
| `app/pipeline.py` | one new call in `run_pipeline`, immediately after `_process_entities()` |
| `app/mcp/tools.py` | identical call in `persist_email_analysis`, same relative position |
| `tests/test_classification.py` (new) | see §13 |

Explicitly **not** touched: `app/interfaces/llm_provider.py`, any file under
`app/providers/llm/`, `app/analysis/`, `app/context/`, `app/replies/`,
`app/calendar/` (only read from, via `detect_meeting`, never modified),
`app/entities/` (only read from, never modified).

## 12. Interaction with the existing pipeline (explicit non-effects)

- `needs_reply()`'s gate for reply-draft creation is **unchanged** — classification
  does not influence whether a draft gets created in this design. (A future
  enhancement could skip drafting for `action_type == "information_only"`, but
  that's a separate decision, out of scope here, flagged so it isn't silently
  assumed.)
- Commitment/follow-up/meeting resolution is read-only input to classification,
  never written to by it.
- A `needs_review == true` email is not blocked from completing the pipeline — it
  reaches `COMPLETED` exactly like any other email. The flag exists so a future
  "what needs my attention" query (or an MCP `list_*` tool, following the existing
  `list_processed_emails`/`search_emails` pattern) can filter on it — no such tool
  is being built in this spec, just the field it would read.

## 13. Test cases (`tests/test_classification.py`)

Following the repo's existing convention exactly: `mongomock.MongoClient()` +
`initialize_indexes(db)` fixture, `Settings(...)` constructed directly, local
`Email`/`EmailAnalysis` fixtures — no live LLM, no live API, matching every other
test file in this codebase.

**Business category:**
- `test_sender_from_agent_domain_classifies_as_internal`
- `test_sender_matching_finance_allowlist_classifies_as_finance`
- `test_invoice_keyword_in_subject_classifies_as_finance`
- `test_recruiting_keywords_with_no_open_commitment_classifies_as_recruiting`
- `test_recruiting_keywords_with_open_commitment_does_not_override_customer`
- `test_known_vendor_domain_classifies_as_vendor`
- `test_sender_with_prior_mine_commitment_classifies_as_customer`
- `test_sender_with_buying_signals_and_no_mine_commitment_classifies_as_prospect`
- `test_unresolved_sales_signal_falls_back_to_sales_category`
- `test_subject_matching_existing_project_classifies_as_project`
- `test_no_reply_bulk_sender_with_no_commitments_classifies_as_informational`
- `test_no_rule_matches_falls_back_to_other`
- `test_personal_allowlisted_sender_classifies_as_personal`
- `test_personal_never_inferred_from_body_content_alone`

**Action type:**
- `test_unresolved_meeting_detection_classifies_action_type_meeting`
- `test_open_mine_commitment_classifies_action_required`
- `test_new_follow_up_classifies_follow_up_required`
- `test_approval_keywords_classify_approval_required`
- `test_question_mark_with_no_other_signal_classifies_reply_required`
- `test_bulk_sender_no_signals_classifies_information_only`
- `test_nothing_matches_classifies_no_action`
- `test_action_type_rule_order_meeting_wins_over_reply_required`

**Priority:**
- `test_urgent_keyword_raises_priority_score`
- `test_commitment_due_within_two_days_scores_p1`
- `test_commitment_due_within_seven_days_scores_at_most_p2`
- `test_no_urgency_signals_scores_p4`
- `test_priority_score_boundary_values_map_to_correct_tier`

**Labels:**
- `test_pricing_mention_adds_pricing_label`
- `test_contract_keyword_adds_contract_label`
- `test_duplicate_label_sources_collapse_to_one_entry`
- `test_urgent_label_dropped_when_action_type_is_no_action`
- `test_dropped_label_recorded_in_rationale`

**Confidence / review:**
- `test_strong_rule_match_yields_high_confidence`
- `test_weak_category_rule_sets_needs_review_true`
- `test_confidence_below_threshold_sets_needs_review_true`
- `test_low_confidence_email_keeps_its_action_type_not_downgraded_to_no_action`
- `test_confidence_score_clamped_to_zero_one_range`

**Taxonomy safety:**
- `test_classify_email_never_returns_a_business_category_outside_the_literal_set` (exhaustive fixture sweep)
- `test_classify_email_never_returns_an_action_type_outside_the_literal_set`
- `test_classify_email_never_returns_a_label_outside_the_literal_set`

**Integration (pipeline wiring):**
- `test_run_pipeline_persists_classification_on_the_email_document`
- `test_persist_email_analysis_persists_classification_identically_to_run_pipeline`
- `test_classification_does_not_alter_reply_draft_or_calendar_action_behavior`

**Real-data regression (read-only, against the historical baseline — see §14):**
- `test_classifier_runs_without_error_over_every_historical_email_fixture` — a
  handful of the real bodies pulled in §14, frozen as fixtures (not a live DB read
  in the test suite itself), asserting no exception and a valid `EmailClassification`
  for each.

## 14. Worked examples — real historical emails

Pulled read-only from the protected historical dataset (excluding this session's
live-test senders). Shown here to validate the rules against real content before
implementation; not fabricated.

**1. `199480e4ccd0fd5d` — jacob.uzana@ex.co → ashok@databeat.io, "Restarting Our Conversation"**
> "We would like to restart our conversation regarding the cooperation between DataBeat and EX.CO... let us know when you are available for a quick call."

`business_category: prospect` (external domain, restarting a stalled relationship,
no `class="mine"` commitment yet) · `action_type: reply_required` (question,
scheduling ask) · `priority: P3` · `labels: ["prospect", "sales", "meeting"]` ·
`confidence: 0.7`.

**2. `1a0c6926f640479e` — hello@render.com → sandeep.thota@databeat.io, "The easiest way to launch scalable web services"**
> "Deploy secure, scalable web services with a delightful developer experience... you can..."

`business_category: vendor` (bulk marketing sender, `hello@` pattern, unrelated
product pitch) · `action_type: information_only` · `priority: P4` ·
`labels: ["fyi"]` · `confidence: 0.75`.

**3. `1a0c7ed96942cc9b` — support@groq.com → sandeep.thota@databeat.io, "Notice: Your Groq API Key will expire in 7 days"**
> "Your API key ... will expire in 7 days ... This key has not been used..."

`business_category: vendor` (existing configured LLM provider's own domain,
account-notice keyword match) · `action_type: action_required` (a key rotation is
a real, if low-stakes, required action — not purely informational) ·
`priority: P3` (7-day runway, not urgent) · `labels: []` · `confidence: 0.65`.

**4. `195c80523332ffb1` — finance@databeat.io → john@doyouremember.com, "...Pending Invoices"**
> "I'm reaching out to follow up on my earlier email regarding the outstanding invoices FY24-0173... Can you please provide an update on the payment status?"

`business_category: internal` (sender domain matches agent's own domain — rule 1
fires before the finance-keyword rule ever runs, since the outbound sender here is
`databeat.io` itself, not the recipient). This is a deliberate, correctly-ordered
outcome: rule 1 (`_is_internal`) checks the **email's own sender**, so DataBeat's
own outbound finance-collections email is `internal` from DataBeat's mailbox
perspective, not `finance`-category-as-a-customer-invoice. `action_type:
follow_up_required` · `priority: P2` (multiple aged invoices) · `labels:
["internal", "follow_up"]` · `confidence: 0.8`. *(This example is why §4.1's rule
order matters and is worth re-confirming with you before implementation — see the
open question below.)*

**5. `19b9e87bcee3adfb` — alex@cashew.ai → ashok@databeat.io, "Updated invitation: Ashok ... & (Alex Vardon)"**
> "Sorry to be a pain but could we meet near Hall G at the Venetian instead?"

`business_category: sales` (external, event/meeting logistics, no clear
customer/prospect signal on its own) · `action_type: meeting` (unresolved
meeting-detail change — `detect_meeting` would likely flag `needs_clarification`
given the calendar-invite-subject pattern) · `priority: P2` (same-day logistics) ·
`labels: ["sales", "meeting"]` · `confidence: 0.6`, `needs_review: true` (weak
category rule 7 fired, not a strong customer/prospect signal).

**Open question worth flagging before you approve this spec**: example 4 shows
that a `finance@databeat.io` sender is currently classified `internal`, not
`finance` — is that the outcome you want, or should the finance allowlist rule
(§4.1, rule 2) take priority over the internal-domain rule (rule 1) specifically
for known finance sub-addresses? Both are defensible; the rule order in §4.1 is
what decides it, and it's a one-line change either way once you tell me which you
prefer.

## 15. Explicitly out of scope (per your constraints)

- No LLM classification method, no `classify_email_ambiguous`, no new
  `LLMProvider` interface method — fully removed from this spec versus the earlier
  brainstorm.
- No external classification API or SaaS dependency of any kind.
- No new API key of any kind.
- No changes to `app/interfaces/llm_provider.py` or any file under
  `app/providers/llm/`.
- No application code has been modified to produce this document — this is a
  design spec only.
