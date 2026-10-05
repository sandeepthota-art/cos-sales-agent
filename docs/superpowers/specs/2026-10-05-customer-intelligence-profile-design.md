# CoS Sales Agent — Customer/Contact Intelligence Profile

Status: draft design, approved section-by-section in conversation, pending written review.

## 1. Purpose

Today, the People detail page (`frontend/src/pages/PersonDetailPage.tsx`) is a raw
database-record viewer: a "Profile" card of a few fields, followed by seven
unfiltered `RecordList` dumps (Commitments, Meetings, Follow-ups, Projects,
Knowledge, Reply Drafts, Calendar Actions), each showing internal ids, raw
timestamps, and backend fields like `basis`/`confidence`/`canonical_person_id`.
There is no answer, in a few seconds, to "who is this person, what do they do,
where do they work, why do they matter to us, what have we been discussing."

This spec replaces that page's content with a short, human-readable,
progressively-built profile — a Customer/Contact Intelligence layer, not a
database viewer. It is additive at the data layer (new fields on `Person`,
one new tool) and a rendering change at the UI layer; nothing existing is
deleted from the backend, since the operational data (commitments, meetings,
etc.) still needs to exist for the agent and for other pages.

This follows the exact "Claude reasons, tools only persist" split already
established by the Organizational Knowledge Layer
(`docs/superpowers/specs/2026-10-05-knowledge-layer-design.md`): no server-side
LLM call is introduced anywhere. The profile's prose is authored by whichever
Claude session is processing the email, using the email content plus the
person's and organization's already-known context, and handed to a new
persistence tool exactly like `persist_organization_research`.

## 2. Current State (confirmed by direct code read)

- `Person` (`app/entities/models.py`): `id`, `name`, `email`, `aliases`, `org`,
  `org_id`, `role`, `goal_pillar`, `last_inbound`, `last_outbound`,
  `open_threads`, `status`, `merged_into` — no narrative/profile field of any
  kind.
- `PersonContextSnapshot` (`app/entities/person_context.py`) is a *different*
  thing: a per-(person, email) incremental record of *structural* facts
  (commitments/meetings/follow-ups/etc. this email produced), consumed by
  `get_bounded_person_context_for_llm` to feed an LLM classification prompt.
  It is not a narrative profile and this spec does not change it.
- `get_person_context` (`app/entities/context.py`) is the full, unbounded
  360-degree view — already returns every category (commitments, meetings,
  follow_ups, projects, knowledge, reply_drafts, calendar_actions, emails,
  threads, related_people, other_people_at_org) wrapped with a `basis` tag.
  `GET /api/v1/people/{id}` (`app/api/routers/people.py`) passes this through
  unchanged. This function is **not modified** by this spec — only the one
  page that renders its output changes what it shows.
- `PersonDetailPage.tsx` renders the "Profile" card (email, org via `IdLink`,
  last_inbound/outbound, status/merged_into) plus seven raw `RecordList`
  dumps of the categories above.
- `Organization` (already shipped) has `description`/`industry`/other profile
  fields, populated once via external research — this is the existing source
  a person's profile draws company context from, requiring no new research
  step.

## 3. What's Missing

1. No narrative profile field on `Person` at all — nothing to render instead
   of the raw dumps.
2. No signal, anywhere in the pipeline, telling Claude "here is this person's
   current profile plus their org's profile, in case this email adds
   something worth recording."
3. No tool to persist a composed profile update.
4. The People detail page shows seven categories of raw backend data with no
   synthesized view.

## 4. Schema Changes

Only `Person` changes:

```python
# app/entities/models.py — Person, new fields
class Person(BaseModel):
    # ... existing fields unchanged ...

    # Customer Intelligence profile -- Claude-authored prose, composed from
    # this person's existing profile + their Organization's already-researched
    # profile + the triggering email, and persisted via persist_person_profile.
    # None until the first email mentioning this person is processed.
    profile_summary: str | None = None
    recent_context: str | None = None
    key_topics: list[str] = Field(default_factory=list)
    profile_updated_at: datetime | None = None
```

`role` (already exists) is reused for the short title line ("AI Training and
AI Consulting professional") — not duplicated. No new collection: every
other field a profile needs (`org_id` → `Organization.description`/
`industry`) already exists from the shipped Knowledge Layer work.

`get_person_context`/`GET /api/v1/people/{id}` need **no code change** —
they already return the raw `Person` document, so these fields are included
automatically once added to the model.

## 5. Ingestion / Update Flow

```text
persist_email_analysis (app/mcp/tools.py)
  |
  v
people/org resolution + new_organizations_needing_research + WORKS_AT fact
  (all unchanged, already shipped)
  |
  v
*** NEW: for every person_id in entities_referenced["people"], bundle their
    CURRENT profile state (role, profile_summary, recent_context, key_topics)
    + their Organization's already-researched profile (name, description,
    industry) into one payload -- no second lookup needed by the caller ***
  |
  v
return payload gains: people_profile_context: [{person_id, name, role,
  profile_summary, recent_context, key_topics, org_name, org_description,
  org_industry}, ...]
  |
  v
(Claude, in conversation) reads this + the actual email content, decides
whether anything substantive changed (a trivial "thanks, got it" shouldn't
overwrite a good profile with nothing new -- this is judgment, not a rule
the tool enforces)
  |
  v
if so: Claude composes the FULL updated role/profile_summary/recent_context/
  key_topics -- incorporating what was already there, never blind-appending --
  and calls persist_person_profile(db, person_id, role=None,
  profile_summary=None, recent_context=None, key_topics=None)
  |
  v
mark_email_completed (unchanged)
```

Key properties:

- **Each field is independently optional** on `persist_person_profile` —
  `None` means "leave this one as it is." There is no "manual" overwrite
  protection like `Organization`'s research has: every field here is always
  Claude-composed, so there is nothing to protect against — the instruction
  to incorporate prior context is what prevents regression, not a tool-level
  guard.
- **Company context is free for a new person at a known company.** Because
  `people_profile_context` already includes `org_description`/`org_industry`
  from the (already-shipped) Organization research, composing a brand-new
  person's `profile_summary` never requires a separate lookup or a new
  research step — "at Alumnx AI Labs, an AI training and consulting company"
  falls out of data that already exists.
- **No duplication across emails.** Because the tool always overwrites
  (never appends) and Claude is instructed to read the existing value first,
  a person's profile stays one concise, current synthesis — not a growing
  log. This is the same reason `persist_organization_research` overwrites
  rather than appends.
- **The skill decides when to call it**, not the tool. `persist_email_analysis`
  always includes the signal for every referenced person; whether a given
  email is substantive enough to warrant a profile update is the calling
  Claude session's judgment call, exactly like the existing
  `new_organizations_needing_research`/`possible_missed_commitment` signals.

## 6. React UI Redesign

`PersonDetailPage.tsx` replaces the "Profile card + 7 raw `RecordList` dumps"
with:

```text
[Name]                                    [Company name, linked]
[Position / role title, as subtitle]

  <profile_summary -- 2-4 sentences: who they are, their company, their
  role, their relevance to us>

Recent context
  <recent_context -- 1-3 sentences: what we've actually been discussing>

[Key topic tags, from key_topics]

Email: ...          Last heard from: <relative time, not a raw timestamp>
Also at <company>: <other people at the org, names only, linked -- not a
  raw record dump>
```

Concretely:

- `frontend/src/api/types.ts`'s `PersonRow` gains `profile_summary`,
  `recent_context`, `key_topics`, `profile_updated_at`.
- `PersonDetailPage.tsx` drops the `RecordList` calls for
  Commitments/Meetings/Follow-ups/Projects/Knowledge/Reply Drafts/Calendar
  Actions entirely. "Other People at Organization" stays, trimmed to names
  only (linked) — relationship context, not an operational dump.
- `status`/`merged_into` stays, but small and low-key — shown only when the
  person is actually merged, not as a prominent badge.
- **Nothing is removed from the backend.** `get_person_context` and
  `GET /api/v1/people/{id}` keep returning every category unchanged, for the
  agent and any other consumer. Only this one page's rendering changes.
- `PeoplePage.tsx` (the list view) gains one small addition: a truncated
  one-line teaser (from `profile_summary` or `key_topics`) so the list is
  scannable without opening each person.

## 7. Exact Files to Change

1. **`app/entities/models.py`** — `Person`: add the 4 fields from Section 4.
2. **`app/mcp/tools.py`**:
   - `persist_email_analysis` — add `people_profile_context` to its return
     payload (Section 5).
   - New: `persist_person_profile(db, person_id, role=None,
     profile_summary=None, recent_context=None, key_topics=None)`.
3. **`app/mcp/server.py`** — register `persist_person_profile`.
4. **`skills/gmail-initial-ingest/SKILL.md`** — new per-message step acting
   on `people_profile_context`, directly mirroring the existing
   organization-research step.
5. **`frontend/src/api/types.ts`** — `PersonRow` gains the four new fields.
6. **`frontend/src/pages/PersonDetailPage.tsx`** — redesigned per Section 6.
7. **`frontend/src/pages/PeoplePage.tsx`** — add the one-line profile teaser
   column.
8. **Tests**: `tests/test_entities_models.py`, `tests/test_mcp_deterministic_tools.py`,
   `tests/test_mcp_server.py`, and `frontend/src/pages/__tests__/PersonDetailPage.test.tsx`
   (already exists — updated for the new layout).

## 8. What's Explicitly NOT Done Here

- No server-side LLM or search API call anywhere — all reasoning (reading
  the email, composing the profile) happens in the calling Claude session,
  consistent with every other part of this pipeline.
- No changes to `get_person_context`, `get_bounded_person_context_for_llm`,
  or `PersonContextSnapshot` — these remain exactly as they are; this spec
  adds a narrative layer alongside them, not a replacement.
- No removal of operational data from the backend or from any other page —
  Commitments/Meetings/Follow-ups/etc. remain fully accessible through their
  own existing top-level pages; they are only removed from this one page's
  rendering.
- No new "manual"-style overwrite protection on the person profile fields —
  unlike `Organization`'s research, every field here is always
  Claude-composed, so there is nothing to protect.
