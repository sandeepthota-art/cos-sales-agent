# Customer/Contact Intelligence Profile Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the People detail page's raw database-record dump with a short, progressively-built, Claude-authored profile (role, company, relevance, recent discussion context), while leaving the backend's full data access completely intact for the agent and other consumers.

**Architecture:** Additive fields on `Person` + one new deterministic persistence tool (`persist_person_profile`), fed by a new signal (`people_profile_context`) on the existing `persist_email_analysis` tool -- the exact same "Claude reasons, tool persists" split already proven by `persist_organization_research`. The React `PersonDetailPage`/`PeoplePage` change what they render; nothing on the backend's existing `get_person_context`/`GET /api/v1/people/{id}` contract changes.

**Tech Stack:** Python 3.11, Pydantic, MongoDB (pymongo + mongomock for tests), FastMCP, React + TypeScript, Vitest + Testing Library.

**Spec:** `docs/superpowers/specs/2026-10-05-customer-intelligence-profile-design.md`

## Global Constraints

- No server-side LLM or search API call anywhere in this plan — all reasoning (reading the email, composing the profile) happens in the calling Claude session, never inside a tool function.
- `persist_person_profile` has no "manual" overwrite-protection concept — every field it writes is always Claude-composed; there is nothing to protect against.
- `get_person_context`, `get_bounded_person_context_for_llm`, and `PersonContextSnapshot` are not modified by this plan.
- No operational data (commitments/meetings/follow-ups/etc.) is removed from the backend or from any other page — only `PersonDetailPage.tsx`'s own rendering changes.
- All Python tests use `mongomock` only. All React tests use Vitest + Testing Library + the existing `installMockFetch` helper — no real network calls.

## Review Focus

- **A profile update must never drop fields the caller didn't touch**: calling `persist_person_profile` with only `recent_context` supplied must leave `role`/`profile_summary`/`key_topics` exactly as they were — including for a pre-existing Person document that predates these fields entirely (the exact bug the Organization-research tool had on its first pass in the prior plan). Task 2's test (`test_persist_person_profile_leaves_unsupplied_fields_unchanged_even_for_a_pre_existing_person`) pins this.
- **`people_profile_context` must reflect the organization's profile, not just the person's**: a brand-new person at an already-researched company must see that company's `description`/`industry` in the same payload, with no second lookup required. Task 3's test (`test_persist_email_analysis_people_profile_context_includes_organization_profile`) pins this.
- **A person with no `org_id` must not crash the signal**: `people_profile_context` must still include that person (with `org_name`/`org_description`/`org_industry` all `None`), not raise or silently omit them. Task 3's test (`test_persist_email_analysis_people_profile_context_handles_a_person_with_no_org`) pins this.
- **The React page must not silently render "undefined" when a profile hasn't been composed yet**: a person who has never had `persist_person_profile` called for them (profile fields all `None`/empty) must show a sensible placeholder, not blank/undefined text. Task 6's test (`renders a placeholder when no profile has been composed yet`) pins this.
- **The removed operational data must not vanish from the test fixture silently**: the existing `PersonDetailPage.test.tsx` fixture already includes `commitments`/`meetings`/etc. — the rewritten test must positively assert those are no longer rendered (not just stop asserting on them), so a future regression that re-adds them is caught. Task 6's test (`does not render raw commitments/meetings/follow-up data`) pins this.

---

## Task 1: `Person` profile fields

**Files:**
- Modify: `app/entities/models.py` (the `Person` class)
- Test: `tests/test_entities_models.py`

**Interfaces:**
- Produces: `Person` gains `profile_summary: str | None`, `recent_context: str | None`, `key_topics: list[str]`, `profile_updated_at: datetime | None`. `role` (already exists) is unchanged.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_entities_models.py` (uses the existing `from app.entities.models import ...` import line, already includes `Person`):

```python
def test_person_profile_fields_default_to_empty():
    person = Person(id="PER-001", name="Jane Doe", email="jane@example.com")
    assert person.profile_summary is None
    assert person.recent_context is None
    assert person.key_topics == []
    assert person.profile_updated_at is None


def test_person_accepts_a_composed_profile():
    person = Person(
        id="PER-001", name="Vijender", email="vijender@alumnx.com",
        profile_summary="Vijender is an AI Training and AI Consulting professional at Alumnx AI Labs.",
        recent_context="Vijender has discussed AI Engineer requirements with Databeat.",
        key_topics=["AI Engineer hiring", "AI training programs"],
        profile_updated_at=datetime(2026, 10, 5, 12, 0, 0),
    )
    assert person.profile_summary.startswith("Vijender is an AI Training")
    assert person.key_topics == ["AI Engineer hiring", "AI training programs"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entities_models.py -k profile -v`
Expected: FAIL with `AttributeError: 'Person' object has no attribute 'profile_summary'`

- [ ] **Step 3: Implement**

In `app/entities/models.py`, add to the `Person` class (after `merged_into: str | None = None`):

```python
    # Customer Intelligence profile -- Claude-authored prose, composed from
    # this person's existing profile + their Organization's already-researched
    # profile + the triggering email, and persisted via persist_person_profile
    # (app.mcp.tools). None until the first email mentioning this person is
    # processed.
    profile_summary: str | None = None
    recent_context: str | None = None
    key_topics: list[str] = Field(default_factory=list)
    profile_updated_at: datetime | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entities_models.py -k profile -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/entities/models.py tests/test_entities_models.py
git commit -m "feat: add Customer Intelligence profile fields to Person"
```

---

## Task 2: `persist_person_profile` tool

**Files:**
- Modify: `app/mcp/tools.py` (add the new function, placed directly above `persist_organization_research`)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `Person` fields from Task 1 (already-imported `PersonRepository`).
- Produces: `persist_person_profile(db, person_id, role=None, profile_summary=None, recent_context=None, key_topics=None) -> dict[str, Any]` (raises `ValueError` for an unknown `person_id`). Task 3's signal and the MCP registration (Task 4) both depend on this exact name/signature.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_deterministic_tools.py`:

```python
def test_persist_person_profile_sets_supplied_fields_and_stamps_updated_at(db):
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"},
        {"id": "PER-001", "name": "Vijender", "email": "vijender@alumnx.com", "aliases": [], "open_threads": []},
    )

    result = tools.persist_person_profile(
        db, "PER-001",
        role="AI Training and AI Consulting professional",
        profile_summary="Vijender is an AI Training and AI Consulting professional at Alumnx AI Labs.",
        recent_context="Discussing AI Engineer requirements with Databeat.",
        key_topics=["AI Engineer hiring"],
    )

    assert result["role"] == "AI Training and AI Consulting professional"
    assert result["profile_summary"].startswith("Vijender is an AI Training")
    assert result["key_topics"] == ["AI Engineer hiring"]
    assert result["profile_updated_at"] is not None
    stored = PersonRepository(db).find_one({"id": "PER-001"})
    assert stored["recent_context"] == "Discussing AI Engineer requirements with Databeat."


def test_persist_person_profile_leaves_unsupplied_fields_unchanged_even_for_a_pre_existing_person(db):
    # Review Focus: a document created before these fields existed has none of
    # them in Mongo at all -- the result must still expose the full field set
    # (existing values where nothing was supplied), not KeyError/omit them.
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"},
        {"id": "PER-001", "name": "Vijender", "email": "vijender@alumnx.com", "aliases": [], "open_threads": []},
    )
    tools.persist_person_profile(db, "PER-001", role="AI Consultant", profile_summary="Initial summary.")

    result = tools.persist_person_profile(db, "PER-001", recent_context="New discussion context.")

    assert result["role"] == "AI Consultant"
    assert result["profile_summary"] == "Initial summary."
    assert result["recent_context"] == "New discussion context."
    assert result["key_topics"] == []


def test_persist_person_profile_raises_for_unknown_person(db):
    with pytest.raises(ValueError, match="PER-999"):
        tools.persist_person_profile(db, "PER-999", role="Someone")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k persist_person_profile -v`
Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'persist_person_profile'`

- [ ] **Step 3: Implement**

Add to `app/mcp/tools.py`, directly above `persist_organization_research`:

```python
def persist_person_profile(
    db: Database,
    person_id: str,
    role: str | None = None,
    profile_summary: str | None = None,
    recent_context: str | None = None,
    key_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Persists a Customer/Contact Intelligence profile update a reasoning
    caller (Claude, having read the triggering email plus this person's
    existing profile from persist_email_analysis's people_profile_context)
    already composed -- this function never synthesizes text itself, purely
    persistence, exactly like persist_organization_research's own split.

    Each field is independently optional: None leaves that field exactly as
    it already was. There is no "manual" overwrite-protection like
    Organization's research has -- every field here is always Claude-
    composed, so there is nothing to protect against; the instruction to
    incorporate the existing value (already handed back via
    people_profile_context) is what prevents regression, not a tool guard.

    Raises ValueError if person_id doesn't exist.
    """
    repo = PersonRepository(db)
    person = repo.find_one({"id": person_id})
    if person is None:
        raise ValueError(f"no person found for person_id={person_id!r}")

    # Every field is written explicitly (even when left unchanged), not just
    # the ones that got a new value -- so the returned/stored document always
    # exposes the full field set, matching Person's own defaults, rather than
    # silently omitting a key a pre-existing document never had.
    update: dict[str, Any] = {
        "role": role if role is not None else person.get("role"),
        "profile_summary": profile_summary if profile_summary is not None else person.get("profile_summary"),
        "recent_context": recent_context if recent_context is not None else person.get("recent_context"),
        "key_topics": key_topics if key_topics is not None else person.get("key_topics", []),
        "profile_updated_at": datetime.now(timezone.utc),
    }

    repo.upsert_by_key({"id": person_id}, {**person, **update})
    return {**person, **update}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k persist_person_profile -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: add persist_person_profile tool"
```

---

## Task 3: `people_profile_context` signal in `persist_email_analysis`

**Files:**
- Modify: `app/mcp/tools.py:377` (inside `persist_email_analysis`, between the WORKS_AT loop and the `sender_person = resolve_canonical_person_for_email(...)` line)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `entities_referenced["people"]`, `person_repo`/`org_repo` (already defined earlier in the same function scope, from the Knowledge Layer work), `persist_person_profile` (Task 2, used only by the tests here).
- Produces: `persist_email_analysis`'s return dict gains `"people_profile_context": list[{"person_id", "name", "role", "profile_summary", "recent_context", "key_topics", "org_name", "org_description", "org_industry"}]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_deterministic_tools.py`:

```python
def test_persist_email_analysis_includes_people_profile_context_for_referenced_people(db, settings):
    ingest_result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    person = PersonRepository(db).find_one({"email": "jane@newco.com"})
    entry = next(e for e in result["people_profile_context"] if e["person_id"] == person["id"])
    assert entry["name"] == "Jane Smith"
    assert entry["profile_summary"] is None
    assert entry["recent_context"] is None
    assert entry["key_topics"] == []


def test_persist_email_analysis_people_profile_context_includes_organization_profile(db, settings):
    ingest_result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    result1 = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)
    person = PersonRepository(db).find_one({"email": "jane@newco.com"})
    tools.persist_organization_research(
        db, person["org_id"], industry="Widget Manufacturing", description="Makes widgets."
    )

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    entry = next(e for e in result2["people_profile_context"] if e["person_id"] == person["id"])
    assert entry["org_name"] == "NewCo"
    assert entry["org_industry"] == "Widget Manufacturing"
    assert entry["org_description"] == "Makes widgets."


def test_persist_email_analysis_people_profile_context_reflects_an_existing_profile(db, settings):
    ingest_result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)
    person = PersonRepository(db).find_one({"email": "jane@newco.com"})
    tools.persist_person_profile(
        db, person["id"], profile_summary="Existing summary.", recent_context="Existing context."
    )

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    entry = next(e for e in result2["people_profile_context"] if e["person_id"] == person["id"])
    assert entry["profile_summary"] == "Existing summary."
    assert entry["recent_context"] == "Existing context."


def test_persist_email_analysis_people_profile_context_handles_a_person_with_no_org(db, settings):
    # Review Focus: a person with no org_id (e.g. name-only, no email domain
    # ever resolved) must still appear in people_profile_context, not crash
    # or be silently skipped.
    ingest_result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Someone No Org", email=None, org=None)],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    no_org_entries = [e for e in result["people_profile_context"] if e["name"] == "Someone No Org"]
    assert len(no_org_entries) == 1
    assert no_org_entries[0]["org_name"] is None
    assert no_org_entries[0]["org_description"] is None
    assert no_org_entries[0]["org_industry"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k people_profile_context -v`
Expected: FAIL with `KeyError: 'people_profile_context'`

- [ ] **Step 3: Implement**

In `app/mcp/tools.py`, inside `persist_email_analysis`, directly after the WORKS_AT loop's closing (right before the `# Phase 20.1: lifecycle-aware...` comment), add:

```python
    # People profile context (Customer/Contact Intelligence): for every
    # person this email references, bundle their current profile state plus
    # their organization's already-researched profile into one payload, so
    # Claude can compose an updated profile (if this email adds anything
    # substantive) without a second lookup.
    people_profile_context = []
    for person_id in entities_referenced.get("people", []):
        profile_person = person_repo.find_one({"id": person_id})
        if profile_person is None:
            continue
        person_org = (
            org_repo.find_one({"id": profile_person["org_id"]}) if profile_person.get("org_id") else None
        )
        people_profile_context.append(
            {
                "person_id": person_id,
                "name": profile_person["name"],
                "role": profile_person.get("role"),
                "profile_summary": profile_person.get("profile_summary"),
                "recent_context": profile_person.get("recent_context"),
                "key_topics": profile_person.get("key_topics", []),
                "org_name": person_org["name"] if person_org else None,
                "org_description": person_org.get("description") if person_org else None,
                "org_industry": person_org.get("industry") if person_org else None,
            }
        )
```

Then add the new key to the final `return` dict (directly after `"new_organizations_needing_research": new_organizations_needing_research,`):

```python
        "people_profile_context": people_profile_context,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k people_profile_context -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: surface people_profile_context from persist_email_analysis"
```

---

## Task 4: MCP server registration

**Files:**
- Modify: `app/mcp/server.py` (add one `@mcp.tool()` wrapper, directly below `merge_organization_records`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `tools.persist_person_profile` (Task 2).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_persist_person_profile_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "persist_person_profile" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py -k persist_person_profile -v`
Expected: FAIL with `assert "persist_person_profile" in names`

- [ ] **Step 3: Implement**

Add to `app/mcp/server.py`, directly below `merge_organization_records`:

```python
@mcp.tool()
def persist_person_profile(
    person_id: str,
    role: str | None = None,
    profile_summary: str | None = None,
    recent_context: str | None = None,
    key_topics: list[str] | None = None,
) -> dict[str, Any]:
    """Persists a Customer/Contact Intelligence profile update you (Claude)
    already composed -- call this after seeing people_profile_context in a
    persist_email_analysis result, when the email adds something substantive
    about who this person is, their role, or what you've been discussing.
    Each field is optional -- omit one to leave it unchanged. Raises
    ValueError if person_id doesn't exist.
    """
    return tools.persist_person_profile(
        _get_db(), person_id, role=role, profile_summary=profile_summary,
        recent_context=recent_context, key_topics=key_topics,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add app/mcp/server.py tests/test_mcp_server.py
git commit -m "feat: register persist_person_profile on the MCP server"
```

---

## Task 5: `gmail-initial-ingest` skill update

**Files:**
- Modify: `skills/gmail-initial-ingest/SKILL.md`

**Interfaces:**
- Consumes: `people_profile_context` (Task 3), `persist_person_profile` (Tasks 2, 4) -- now registered.

- [ ] **Step 1: Add the new step**

In `skills/gmail-initial-ingest/SKILL.md`, directly after the existing "Research newly-discovered organizations" step (which ends with "Do this before calling `mark_email_completed`."), add:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add skills/gmail-initial-ingest/SKILL.md
git commit -m "docs: update gmail-initial-ingest skill for person profile updates"
```

---

## Task 6: React `PersonDetailPage` redesign

**Files:**
- Modify: `frontend/src/api/types.ts` (`PersonRow` interface)
- Modify: `frontend/src/pages/PersonDetailPage.tsx`
- Test: `frontend/src/pages/__tests__/PersonDetailPage.test.tsx`

**Interfaces:**
- Consumes: the `Person` fields from Task 1, already returned unchanged by `get_person_context`/`GET /api/v1/people/{id}`.
- Produces: `PersonRow` gains `profile_summary?: string | null`, `recent_context?: string | null`, `key_topics?: string[]`, `profile_updated_at?: string | null`. Task 7 (`PeoplePage`) consumes these same fields.

- [ ] **Step 1: Write the failing test**

Replace the single test in `frontend/src/pages/__tests__/PersonDetailPage.test.tsx` with:

```typescript
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { PersonDetailPage } from '../PersonDetailPage'

const PERSON_CONTEXT_BODY = {
  person: {
    id: 'PER-001',
    name: 'Vijender',
    email: 'vijender@alumnx.com',
    org: 'Alumnx AI Labs',
    role: 'AI Training and AI Consulting professional',
    status: 'active',
    profile_summary:
      'Vijender is an AI Training and AI Consulting professional at Alumnx AI Labs, where he works primarily across AI HR consulting, AI training, and AI software solutions.',
    recent_context:
      'Vijender has discussed AI Engineer requirements with Databeat, including candidate profiles and engagement duration.',
    key_topics: ['AI Engineer hiring', 'AI training programs'],
  },
  canonical_person_id: 'PER-001',
  canonical_resolution_error: null,
  organization: { basis: 'canonical_id', data: { id: 'ORG-001', name: 'Alumnx AI Labs' } },
  emails: { basis: 'canonical_id', data: [] },
  threads: { basis: 'canonical_id', data: [] },
  related_people: { basis: 'canonical_id_shared_thread', data: [] },
  other_people_at_org: { basis: 'canonical_id', data: [] },
  commitments: [{ id: 'CMT-001', what: 'send pricing' }],
  meetings: [{ id: 'MTG-001' }],
  follow_ups: [{ id: 'FUP-001' }],
  projects: [],
  knowledge: [],
  reply_drafts: [],
  calendar_actions: [],
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/people/PER-001']}>
      <AuthProvider>
        <Routes>
          <Route path="/people/:personId" element={<PersonDetailPage />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>,
  )
}

describe('PersonDetailPage', () => {
  it('renders the composed profile and recent context', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-001')) return { status: 200, body: PERSON_CONTEXT_BODY }
      return { status: 404 }
    })

    renderPage()

    expect(await screen.findByRole('heading', { name: 'Vijender' })).toBeInTheDocument()
    expect(screen.getByText('Alumnx AI Labs', { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/AI Training and AI Consulting professional at Alumnx AI Labs/)).toBeInTheDocument()
    expect(screen.getByText(/discussed AI Engineer requirements with Databeat/)).toBeInTheDocument()
    expect(screen.getByText(/AI Engineer hiring/)).toBeInTheDocument()
  })

  it('does not render raw commitments/meetings/follow-up data', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-001')) return { status: 200, body: PERSON_CONTEXT_BODY }
      return { status: 404 }
    })

    renderPage()
    await screen.findByRole('heading', { name: 'Vijender' })

    expect(screen.queryByText('CMT-001')).not.toBeInTheDocument()
    expect(screen.queryByText('MTG-001')).not.toBeInTheDocument()
    expect(screen.queryByText('FUP-001')).not.toBeInTheDocument()
    expect(screen.queryByText('send pricing')).not.toBeInTheDocument()
  })

  it('renders a placeholder when no profile has been composed yet', async () => {
    installMockFetch((url) => {
      if (url.includes('/people/PER-002')) {
        return {
          status: 200,
          body: {
            ...PERSON_CONTEXT_BODY,
            person: {
              id: 'PER-002', name: 'New Contact', email: 'new@newco.com', org: null,
              role: null, status: 'active', profile_summary: null, recent_context: null, key_topics: [],
            },
          },
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter initialEntries={['/people/PER-002']}>
        <AuthProvider>
          <Routes>
            <Route path="/people/:personId" element={<PersonDetailPage />} />
          </Routes>
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByRole('heading', { name: 'New Contact' })).toBeInTheDocument()
    expect(screen.getByText(/No profile yet/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd frontend && npx vitest run src/pages/__tests__/PersonDetailPage.test.tsx`
Expected: FAIL — the new assertions (`profile_summary`/`recent_context`/`key_topics` text, "No profile yet" placeholder) don't match the current page's output; the first test's `heading` name also no longer matches the old fixture's "Ashok Kumar".

- [ ] **Step 3: Implement**

In `frontend/src/api/types.ts`, add to the `PersonRow` interface (after `merged_into?: string | null`):

```typescript
  profile_summary?: string | null
  recent_context?: string | null
  key_topics?: string[]
  profile_updated_at?: string | null
```

Replace `frontend/src/pages/PersonDetailPage.tsx` entirely:

```typescript
import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'
import { ApiError } from '../api/client'
import { getPerson } from '../api/people'
import type { PersonContext } from '../api/types'
import { ErrorState } from '../components/ErrorState'
import { IdLink } from '../components/IdLink'
import { LoadingSkeleton } from '../components/LoadingSkeleton'
import { PageHeader } from '../components/PageHeader'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

export function PersonDetailPage() {
  const { personId = '' } = useParams<{ personId: string }>()
  const [context, setContext] = useState<PersonContext | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError(null)
    getPerson(personId)
      .then((result) => {
        if (!cancelled) setContext(result)
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof ApiError ? err.message : 'Failed to load person')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [personId])

  if (loading) return <LoadingSkeleton rows={6} />
  if (error) return <ErrorState message={error} />
  if (!context) return <ErrorState message="Person not found" />

  const { person } = context
  const orgData = context.organization.data
  const companyName = (orgData?.name as string | undefined) ?? person.org ?? undefined

  return (
    <div>
      <PageHeader
        title={person.name || person.id}
        subtitle={person.role ?? undefined}
        breadcrumbs={[{ label: 'People', to: '/people' }, { label: person.id }]}
      />

      <div className="card" style={{ marginBottom: 16 }}>
        {companyName && (
          <p className="card__title">
            {orgData ? <IdLink id={(orgData.id as string) ?? ''} label={companyName} /> : companyName}
          </p>
        )}

        {person.profile_summary ? (
          <p>{person.profile_summary}</p>
        ) : (
          <p style={{ color: 'var(--color-text-muted)' }}>
            No profile yet -- this builds up as emails from this person are processed.
          </p>
        )}

        {person.recent_context && (
          <>
            <p className="card__title" style={{ marginTop: 12 }}>
              Recent context
            </p>
            <p>{person.recent_context}</p>
          </>
        )}

        {(person.key_topics?.length ?? 0) > 0 && (
          <p style={{ marginTop: 12, color: 'var(--color-text-muted)' }}>{person.key_topics!.join(' · ')}</p>
        )}

        <p style={{ marginTop: 12 }}>
          Email: {person.email ?? '—'}
          {' · '}
          Last heard from: {formatDate(person.last_inbound)}
        </p>

        {context.other_people_at_org.data.length > 0 && (
          <p>
            Also at {companyName ?? 'this organization'}:{' '}
            {context.other_people_at_org.data.map((p, index) => (
              <span key={p.id}>
                <IdLink id={p.id} label={p.name ?? p.id} />
                {index < context.other_people_at_org.data.length - 1 && ', '}
              </span>
            ))}
          </p>
        )}

        {person.merged_into && (
          <p>
            <StatusBadge status={person.status} /> → merged into <IdLink id={person.merged_into} />
          </p>
        )}
      </div>
    </div>
  )
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/pages/__tests__/PersonDetailPage.test.tsx`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/pages/PersonDetailPage.tsx frontend/src/pages/__tests__/PersonDetailPage.test.tsx
git commit -m "feat: redesign PersonDetailPage as a Customer Intelligence profile"
```

---

## Task 7: `PeoplePage` profile teaser column

**Files:**
- Modify: `frontend/src/pages/PeoplePage.tsx`

**Interfaces:**
- Consumes: `PersonRow.profile_summary`/`key_topics` (Task 6).

- [ ] **Step 1: Write the failing test**

Create `frontend/src/pages/__tests__/PeoplePage.test.tsx`:

```typescript
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it } from 'vitest'
import { AuthProvider } from '../../auth/AuthContext'
import { installMockFetch } from '../../test-utils/mockFetch'
import { PeoplePage } from '../PeoplePage'

describe('PeoplePage', () => {
  it('shows a one-line profile teaser per person', async () => {
    installMockFetch((url) => {
      if (url.includes('/people')) {
        return {
          status: 200,
          body: [
            {
              id: 'PER-001', name: 'Vijender', email: 'vijender@alumnx.com', company: 'Alumnx AI Labs',
              role: 'AI Consultant', status: 'active',
              profile_summary: 'Vijender is an AI Training and AI Consulting professional.',
              key_topics: ['AI Engineer hiring'],
            },
          ],
        }
      }
      return { status: 404 }
    })

    render(
      <MemoryRouter>
        <AuthProvider>
          <PeoplePage />
        </AuthProvider>
      </MemoryRouter>,
    )

    expect(await screen.findByText('Vijender')).toBeInTheDocument()
    expect(screen.getByText(/AI Training and AI Consulting professional/)).toBeInTheDocument()
  })
})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/pages/__tests__/PeoplePage.test.tsx`
Expected: FAIL — no element contains "AI Training and AI Consulting professional" (no teaser column exists yet)

- [ ] **Step 3: Implement**

Replace `frontend/src/pages/PeoplePage.tsx`'s `columns` array:

```typescript
import { listPeople } from '../api/people'
import type { PersonRow } from '../api/types'
import { EntityListPage } from '../components/EntityListPage'
import { StatusBadge } from '../components/StatusBadge'
import { formatDate } from '../utils/format'

export function PeoplePage() {
  return (
    <EntityListPage<PersonRow>
      title="People"
      fetcher={(limit, offset) => listPeople({ limit, offset })}
      rowKey={(row) => row.id}
      detailRoute={(row) => `/people/${encodeURIComponent(row.id)}`}
      searchableText={(row) => `${row.name ?? ''} ${row.email ?? ''} ${row.company ?? ''}`}
      emptyTitle="No people yet"
      columns={[
        { key: 'name', label: 'Name' },
        { key: 'email', label: 'Email' },
        { key: 'company', label: 'Organization' },
        { key: 'role', label: 'Role' },
        {
          key: 'profile_summary',
          label: 'Profile',
          render: (row) =>
            row.profile_summary ?? (row.key_topics?.length ? row.key_topics.join(', ') : '—'),
        },
        { key: 'last_inbound', label: 'Last Inbound', render: (row) => formatDate(row.last_inbound) },
        { key: 'status', label: 'Status', render: (row) => <StatusBadge status={row.status} /> },
      ]}
    />
  )
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd frontend && npx vitest run src/pages/__tests__/PeoplePage.test.tsx`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add frontend/src/pages/PeoplePage.tsx frontend/src/pages/__tests__/PeoplePage.test.tsx
git commit -m "feat: add profile teaser column to PeoplePage"
```

---

## Final Verification

1. `python -m pytest -q --ignore=tests/test_api.py` — full backend suite green, no regressions.
2. `cd frontend && npx vitest run` — full frontend suite green, no regressions.
3. `python -c "from app.mcp import server"` — confirms the module still imports cleanly.
