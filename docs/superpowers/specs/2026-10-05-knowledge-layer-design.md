# CoS Sales Agent — Organizational Knowledge Layer

Status: draft design, approved section-by-section in conversation, pending written review.

## 1. Purpose

Today, company knowledge is scattered and shallow: `Organization` only ever stores
`name`/`domain`/`aliases`, there is no explicit, queryable record of *why* a person
belongs to a company beyond a bare `org_id` foreign key, and nothing ever asks "what
do we actually know about this company" beyond its domain. This spec adds:

- A real **profile** on `Organization` (industry, description, size, HQ, website),
  populated by external web research the first time a genuinely new company is
  encountered.
- An explicit, auditable **Person → WORKS_AT → Organization** fact, recorded as a
  `KnowledgeItem` alongside every other extracted fact, not just a silent FK.
- A safe way to **merge duplicate organizations** that share no email domain
  (`"DataBeat"` / `"DataBeat Analytics"` / `databeat.com`), mirroring the existing
  Phase 18 person-merge tooling.
- A fix to `get_company_summary`, which today is **silently broken**: its primary
  query matches `Person.org`, a free-text field already confirmed elsewhere in this
  codebase to never be populated by any resolution path, and it also claims
  `Meeting`/`KnowledgeItem` have no org reference — both already exist and are
  already populated (`app/pipeline.py:690-698`, `app/knowledge/`), they're just never
  queried.

This is additive, following the same "Claude reasons, tools only persist" split as
every other part of this pipeline: no server-side LLM or search API call is
introduced anywhere. External research is performed by whichever Claude session is
processing the email, using its own `WebSearch` tool access, exactly the way that
session already performs classification and reply drafting.

## 2. Current State (confirmed by direct code read, not assumed)

- `Organization` (`app/entities/models.py`): `id`, `name`, `domain`, `aliases`,
  `source` — no profile fields, no `status`/`merged_into` lifecycle fields (unlike
  `Person`, which already has both).
- `resolve_organization` (`app/entities/resolution.py:79`): domain-only dedup,
  explicitly and deliberately out of scope for name-similarity merging (own
  docstring: `"DataBeat" vs "DataBeat Inc." vs "databeat"` merging "is deliberately
  out of scope here, since two different real companies can share a very similar
  display name"). Called only from inside `_resolve_person_impl`
  (`app/entities/resolution.py:305,341`) — there is no direct call site in
  `app/pipeline.py`, which matters for how the "new organization" signal below has
  to be computed.
- `Person.org_id` already carries the real company link; `Person.org` (free text)
  is confirmed dead — never populated by any resolution path.
- `Meeting.org_id` (`app/entities/models.py:208-228`) **is** populated today:
  `app/pipeline.py:690` derives `meeting_org_id` from attendees' already-resolved
  `Person.org_id` and passes it into `resolve_meeting_with_operation`
  (`app/entities/resolution.py:874`), which also backfills it on reuse
  (`resolution.py:910-911`).
- `KnowledgeItem.org_id` (`app/knowledge/models.py`) **is** populated today via
  `_link_knowledge_to_entities`.
- `get_company_summary` (`app/mcp/tools.py:1052`) queries `Person.org` and
  `Project.entity` by string equality, and hardcodes `related_meetings: []` and
  `related_knowledge_items: []` with a docstring claiming the schema can't support
  them. Both claims are stale; the underlying bug is that this tool was never
  updated after `org_id` became the canonical link.
- Phase 18 (`app/duplicate_consolidation.py`) already has the merge pattern this
  spec reuses for organizations: `preview_duplicate_person_candidates` (read-only
  classifier) + `merge_person_records` (repoints a person's id across every
  referencing collection via `_execute_one_mapping`).

## 3. What's Missing

1. No `Organization` profile fields, no provenance of where profile data came
   from, no way to tell "never researched" from "researched, found nothing."
2. No `status`/`merged_into` on `Organization` — no way to merge two org records
   without deleting one and breaking every reference to it.
3. No signal, anywhere in the pipeline, that a newly-created `Organization` might
   warrant external research.
4. No tool to persist research results, and no tool to find organizations that
   still need research.
5. No way to detect or merge organization duplicates that don't share a domain.
6. `get_company_summary` returns incomplete/empty data for every real company,
   including its primary `people` list.

## 4. Schema Changes

Only `Organization` changes — `Person.org_id`, `Meeting.org_id`, and
`KnowledgeItem.org_id` already exist and are already populated, so no schema work
is needed there.

```python
# app/entities/models.py — Organization, new fields
class Organization(BaseModel):
    # ... existing: id, name, domain, aliases, source ...

    # Profile, filled by external research (Section 5). Backfill-protected:
    # see "overwrite rule" below.
    industry: str | None = None
    description: str | None = None
    products_services: list[str] = Field(default_factory=list)
    size_estimate: str | None = None
    headquarters: str | None = None
    website: str | None = None

    # Provenance: lets code distinguish "never researched" from "researched,
    # found nothing" from "a human edited this by hand."
    research_source: str | None = None  # "web_research" | "manual" | None
    researched_at: datetime | None = None

    # Lifecycle, mirroring Person exactly, needed for merge_organization_records.
    status: Literal["active", "merged"] = "active"
    merged_into: str | None = None
```

No new collection for the `WORKS_AT` relationship. `Person.org_id` already *is*
that edge; what's added is a `KnowledgeItem` row recorded whenever `org_id` is set
on a person (`subject_key=person_id`, `predicate="works_at"`,
`current_value=org_id`), so the relationship carries history, confidence, and
`source_emails` like every other extracted fact — "what do we know about how X
relates to DataBeat" becomes answerable from the knowledge store itself, not just
an opaque FK.

## 5. Ingestion / Enrichment Flow

```text
persist_email_analysis (app/mcp/tools.py)
  |
  v
people/org resolution runs exactly as today (resolve_person -> resolve_organization)
  |
  v
*** NEW: collect the distinct org_id values touched by this email's resolved
    people; query Organization for any with researched_at is None ***
  |
  v
return payload gains: new_organizations_needing_research: [{org_id, name, domain}, ...]
  |
  v
(Claude, in conversation, per updated skill) sees the signal, runs WebSearch
(1-2 targeted queries: "<name>" company industry / "<domain>" about)
  |
  v
Claude calls persist_organization_research(db, org_id, industry=..., ...,
  research_source="web_research")  -- pure persistence, no LLM/search call inside it
  |
  v
mark_email_completed (unchanged)
```

Key properties:

- **No separate background job.** Research happens synchronously, in the same
  conversation turn that's already processing the email, before
  `mark_email_completed` — same timing discipline as reply drafting today.
- **Self-healing, not one-shot.** The signal is computed from `researched_at`,
  not from a "was this org just created" flag, so a session with no `WebSearch`
  access (or one that ignores the signal) doesn't permanently lose the chance —
  the *next* email mentioning that org surfaces it again.
- **Inconclusive research still closes the loop.** If search turns up nothing
  usable, Claude still calls `persist_organization_research` with all profile
  fields `None`; `researched_at` is set regardless, so the org isn't re-surfaced
  on every single future email, only reconsidered if someone explicitly runs a
  catch-up pass.
- **Second person at a known company inherits its profile for free.** When
  `resolve_organization` reuses an existing org (domain match), `researched_at`
  is already set, so no signal fires — the new person's `org_id` immediately
  makes the existing profile visible to them via the fixed `get_company_summary`.
- **Overwrite rule.** `persist_organization_research` only overwrites a field if
  the existing `research_source != "manual"` — a hand-corrected profile field can
  never be silently clobbered by a later auto-research pass.
- **Catch-up path.** `list_unresearched_organizations(db)` (`researched_at is
  None`) exists independently of the per-email signal, for orgs with no further
  email contact, or for a deliberate "research backlog" sweep.

## 6. Entity Resolution / Dedup

`resolve_organization`'s automatic path is **unchanged** — still domain-only, so
there is no new risk of an automatic false-positive merge.

For name variants that share no domain, add a read-only preview tool, mirroring
Phase 18's person-dedup pattern exactly:

```python
# app/duplicate_consolidation.py
def preview_duplicate_organization_candidates(db: Database) -> list[dict]:
    """Heuristic only -- never merges. Normalizes each org name (lowercase,
    strip common suffixes: Inc/LLC/Ltd/Analytics/Technologies/Corp/Group),
    then flags pairs where the normalized names are equal, one is a substring
    of the other, or an alias/website on one matches another's domain."""

def merge_organization_records(db: Database, source_org_id: str, target_org_id: str) -> dict:
    """Caller-confirmed merge (Claude decides after reviewing a candidate,
    WebSearch if genuinely ambiguous -- never automatic). Repoints org_id
    across every referencing collection: Person, Project, Opportunity,
    Commitment, FollowUp, Meeting, KnowledgeItem, ReplyDraft, CalendarAction
    -- reusing _execute_one_mapping's existing repointing logic. Sets
    source.status="merged", source.merged_into=target_org_id (never deletes
    the source row, matching Person's merge convention). Unions aliases;
    fills any blank profile field on the target from the source without
    overwriting a populated target field."""
```

**Explicit scope boundary:** this only resolves organizations already reachable
via a person's email domain. A company mentioned by name only in email body text,
with no participant at that domain, is still not resolved to an `Organization`
today — unchanged, existing behavior, out of scope here.

## 7. `get_company_summary` Fix

Replace the dead string-match queries with `org_id`-based joins:

```text
people              = Person.find({"org_id": org_id})
projects            = Project.find({"org_id": org_id})       # was: {"entity": org}
related_commitments = Commitment.find({"org_id": org_id})    # was: indirect via project_id, usually empty
related_follow_ups  = FollowUp.find({"org_id": org_id})      # was: indirect via commitment_id
related_meetings    = Meeting.find({"org_id": org_id})       # was: hardcoded []
related_knowledge   = KnowledgeItem.find({"org_id": org_id}) # was: hardcoded []
```

`relationship_notes` and the function's docstring are rewritten to describe these
as direct `org_id` joins, removing the stale "NOT SUPPORTED by the current schema"
claims.

## 8. Exact Files to Change

1. **`app/entities/models.py`** — `Organization`: add the 10 fields from Section 4.
2. **`app/mcp/tools.py`**:
   - `persist_email_analysis` — add `new_organizations_needing_research` to its
     return payload (Section 5).
   - New: `persist_organization_research(db, org_id, industry=None, description=None, products_services=None, size_estimate=None, headquarters=None, website=None, research_source="web_research")`.
   - New: `list_unresearched_organizations(db)`.
   - Fix `get_company_summary` (Section 7).
   - New thin wrappers: `preview_duplicate_organization_candidates`,
     `merge_organization_records`.
3. **`app/duplicate_consolidation.py`** — add the two functions from Section 6.
4. **`app/mcp/server.py`** — register the 4 new tools; fix docstrings that
   reference `get_company_summary`'s old limitations.
5. **`skills/gmail-initial-ingest/SKILL.md`** — per-message procedure gains: act
   on `new_organizations_needing_research` (WebSearch + `persist_organization_research`
   per org); mention `list_unresearched_organizations` for catch-up; mention the
   org-dedup tools as on-demand, not auto-run (matching person-dedup's existing
   convention).
6. **Tests**: `tests/test_entities_models.py` (new Organization fields),
   `tests/test_duplicate_consolidation.py` (new org preview/merge tests mirroring
   the existing person ones), `tests/test_mcp_deterministic_tools.py` /
   `tests/test_mcp_tools.py` (`persist_organization_research`,
   `list_unresearched_organizations`, `new_organizations_needing_research` signal,
   `get_company_summary` fix), `tests/test_mcp_server.py` (new registrations).
7. **Deferred, out of scope for this spec**: surfacing the new `Organization`
   profile fields in the React dashboard (`frontend/src/api/types.ts`'s
   `OrganizationRow`, an organization detail page). The originating request was
   about the ingestion pipeline, not the UI; flagged as a natural follow-up.

## 9. What's Explicitly NOT Done Here

- No automatic name-similarity merging of organizations — confirmed deliberately
  out of scope by the existing `resolve_organization` docstring, and kept that way
  per explicit user decision during brainstorming.
- No resolution of a company mentioned by name only in email body text with no
  participant at that domain (Section 6's scope boundary).
- No server-side LLM or search API call anywhere — all reasoning (research
  queries, dedup judgment calls) happens in the calling Claude session, consistent
  with every other part of this pipeline.
- No UI changes (Section 8, item 7) — deferred as a separate, smaller follow-up.
