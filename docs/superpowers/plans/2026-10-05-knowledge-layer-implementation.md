# Organizational Knowledge Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `Organization` a real profile (populated by external research the first time a genuinely new company is encountered), an explicit auditable Person→WORKS_AT→Organization fact, safe organization-merge tooling for domain-less duplicates, and a working `get_company_summary` that actually joins on `org_id` instead of dead free-text fields.

**Architecture:** Additive changes to the existing deterministic MCP tool layer (`app/mcp/tools.py`) and entity-resolution layer (`app/entities/`), following the project's established "Claude reasons, tools only persist" split — no server-side LLM or search API call anywhere. Organization dedup/merge mirrors the existing Phase 18 Person-merge pattern (`app/duplicate_consolidation.py`) exactly, including a new merged-record lifecycle redirect mirroring `app/entities/lifecycle.py`.

**Tech Stack:** Python 3.11, Pydantic, MongoDB (pymongo + mongomock for tests), FastMCP.

**Spec:** `docs/superpowers/specs/2026-10-05-knowledge-layer-design.md`

## Global Constraints

- No server-side LLM or search API call anywhere in this plan — all reasoning (research queries, dedup judgment calls) happens in the calling Claude session, never inside a tool function.
- `resolve_organization`'s automatic domain-only dedup path is never changed to merge on name similarity — confirmed deliberately out of scope by its own existing docstring and by explicit user decision during brainstorming.
- A duplicate/retired `Organization` is never physically deleted — only marked `status="merged"`, `merged_into=<canonical_id>`, mirroring `Person`'s exact convention.
- Every new MCP tool function takes `db: Database` as its first parameter and is a thin, synchronous, deterministic function — no exceptions to the existing file's style.
- All tests use `mongomock` only — no live MongoDB Atlas, no live LLM API, no live web search, anywhere in this test suite.

## Review Focus

- **Merged organization resurrected via stale domain match**: after `merge_organization_records` retires a source org, a brand-new person whose email domain matches the *source* org's domain must resolve to the canonical (target) org, not recreate/reattach to the retired one. Task 7's test (`test_resolve_organization_redirects_through_a_merged_organization`) pins this.
- **Multiple brand-new companies in one email**: an email that introduces people from two different never-seen-before companies must surface both in `new_organizations_needing_research`, not just the first. Task 3's test (`test_persist_email_analysis_surfaces_every_new_organization_in_one_email`) pins this.
- **Research must never clobber a hand-verified field**: `persist_organization_research` must leave a field with `research_source="manual"` untouched even when new auto-research data is supplied for it. Task 2's test (`test_persist_organization_research_never_overwrites_a_manual_field`) pins this.
- **`get_company_summary` for a real org with zero related records**: must return an org with all-empty lists (a real, findable company that just has no data yet), clearly distinct from `None` (an org_id that doesn't exist at all). Task 5's test (`test_get_company_summary_returns_empty_lists_for_a_real_org_with_no_related_records`) pins this.
- **WORKS_AT fact must not duplicate across repeated ingestion**: a second email from a person whose `org_id` was already recorded as a WORKS_AT fact must not create a second `KnowledgeItem` for the same relationship. Task 4's test (`test_persist_email_analysis_does_not_duplicate_works_at_fact_on_a_later_email`) pins this.

---

## Task 1: Organization profile, provenance, and lifecycle fields

**Files:**
- Modify: `app/entities/models.py:7-12` (the `Organization` class)
- Test: `tests/test_entities_models.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `Organization` gains fields every later task reads/writes — `industry: str | None`, `description: str | None`, `products_services: list[str]`, `size_estimate: str | None`, `headquarters: str | None`, `website: str | None`, `research_source: str | None`, `researched_at: datetime | None`, `status: str` (default `"active"`), `merged_into: str | None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_entities_models.py` (add `Organization` to the existing `from app.entities.models import ...` line at the top of the file):

```python
def test_organization_defaults():
    org = Organization(id="ORG-001", name="DataBeat", domain="databeat.io")
    assert org.industry is None
    assert org.description is None
    assert org.products_services == []
    assert org.size_estimate is None
    assert org.headquarters is None
    assert org.website is None
    assert org.research_source is None
    assert org.researched_at is None
    assert org.status == "active"
    assert org.merged_into is None


def test_organization_accepts_research_profile_and_lifecycle_fields():
    org = Organization(
        id="ORG-001", name="DataBeat", domain="databeat.io",
        industry="Data Analytics", description="A BI platform vendor.",
        products_services=["dashboards", "reporting"], size_estimate="11-50 employees",
        headquarters="Bengaluru, India", website="https://databeat.io",
        research_source="web_research", researched_at=datetime(2026, 10, 5, 12, 0, 0),
        status="merged", merged_into="ORG-002",
    )
    assert org.industry == "Data Analytics"
    assert org.products_services == ["dashboards", "reporting"]
    assert org.research_source == "web_research"
    assert org.status == "merged"
    assert org.merged_into == "ORG-002"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entities_models.py -k organization -v`
Expected: FAIL with `TypeError: Organization() got unexpected keyword arguments` (or a Pydantic `ValidationError` for the extra fields).

- [ ] **Step 3: Implement**

Replace the `Organization` class in `app/entities/models.py`:

```python
class Organization(BaseModel):
    id: str
    name: str
    domain: str | None = None
    aliases: list[str] = Field(default_factory=list)
    source: str = "gmail"
    # Profile, filled by external research (Claude's own WebSearch, triggered by
    # persist_email_analysis's new_organizations_needing_research signal -- see
    # app.mcp.tools.persist_organization_research). None until researched.
    industry: str | None = None
    description: str | None = None
    products_services: list[str] = Field(default_factory=list)
    size_estimate: str | None = None
    headquarters: str | None = None
    website: str | None = None
    # Provenance: "web_research" (auto) vs "manual" (hand-edited, never
    # overwritten by a later auto-research pass) vs None (never researched).
    research_source: str | None = None
    researched_at: datetime | None = None
    # Lifecycle, mirroring Person exactly -- see app.entities.organization_lifecycle
    # and app.mcp.tools.merge_organization_records (Tasks 7-8).
    status: str = "active"
    merged_into: str | None = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entities_models.py -k organization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/entities/models.py tests/test_entities_models.py
git commit -m "feat: add profile, provenance, and lifecycle fields to Organization"
```

---

## Task 2: Organization research persistence tools

**Files:**
- Modify: `app/mcp/tools.py` (add `OrganizationRepository` to the existing `from app.database.repositories import (...)` block; add two new functions, placed directly above `get_company_summary`)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `Organization` fields from Task 1.
- Produces: `persist_organization_research(db, org_id, industry=None, description=None, products_services=None, size_estimate=None, headquarters=None, website=None, research_source="web_research") -> dict[str, Any]` (raises `ValueError` for an unknown `org_id`) and `list_unresearched_organizations(db) -> list[dict[str, Any]]`. Task 3 calls both.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_deterministic_tools.py`. First add `OrganizationRepository` to the existing `from app.database.repositories import (...)` block, and add `from datetime import datetime, timezone` to the top imports. Then add:

```python
def test_persist_organization_research_sets_profile_fields_and_researched_at(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "databeat.io", "domain": "databeat.io", "aliases": [], "source": "gmail"},
    )

    result = tools.persist_organization_research(
        db, "ORG-001", industry="Data Analytics", description="A BI platform vendor.",
        products_services=["dashboards"], size_estimate="11-50 employees",
        headquarters="Bengaluru, India", website="https://databeat.io",
    )

    assert result["industry"] == "Data Analytics"
    assert result["research_source"] == "web_research"
    assert result["researched_at"] is not None
    stored = OrganizationRepository(db).find_one({"id": "ORG-001"})
    assert stored["industry"] == "Data Analytics"


def test_persist_organization_research_sets_researched_at_even_when_inconclusive(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "databeat.io", "domain": "databeat.io", "aliases": [], "source": "gmail"},
    )

    result = tools.persist_organization_research(db, "ORG-001")

    assert result["industry"] is None
    assert result["researched_at"] is not None
    assert result["research_source"] == "web_research"


def test_persist_organization_research_never_overwrites_a_manual_field(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {
            "id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail",
            "industry": "Hand-verified Industry", "research_source": "manual",
        },
    )

    result = tools.persist_organization_research(db, "ORG-001", industry="Auto-researched Industry")

    assert result["industry"] == "Hand-verified Industry"
    assert result["research_source"] == "manual"


def test_persist_organization_research_raises_for_unknown_org(db):
    with pytest.raises(ValueError, match="ORG-999"):
        tools.persist_organization_research(db, "ORG-999")


def test_list_unresearched_organizations_returns_only_orgs_with_no_researched_at(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "New Co", "domain": "newco.com", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"},
        {
            "id": "ORG-002", "name": "Researched Co", "domain": "researched.com", "aliases": [], "source": "gmail",
            "researched_at": datetime.now(timezone.utc).isoformat(), "research_source": "web_research",
        },
    )

    result = tools.list_unresearched_organizations(db)

    assert [o["id"] for o in result] == ["ORG-001"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k organization_research -v`
Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'persist_organization_research'`

- [ ] **Step 3: Implement**

In `app/mcp/tools.py`, add `OrganizationRepository` to the existing repository import block (alphabetical, between `OpportunityRepository` and `PersonalItemRepository`):

```python
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    ContextSnapshotRepository,
    EmailRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OpportunityRepository,
    OrganizationRepository,
    PersonalItemRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
```

Then add, directly above `def get_company_summary(db: Database, org: str) -> dict[str, Any]:`:

```python
def persist_organization_research(
    db: Database,
    org_id: str,
    industry: str | None = None,
    description: str | None = None,
    products_services: list[str] | None = None,
    size_estimate: str | None = None,
    headquarters: str | None = None,
    website: str | None = None,
    research_source: str = "web_research",
) -> dict[str, Any]:
    """Persists external research a reasoning caller (Claude, via its own WebSearch
    tool) already performed about a company -- this function never searches or
    calls an LLM itself, purely persistence, exactly like persist_email_analysis's
    own split between reasoning and storage.

    Only overwrites a profile field if the existing Organization's
    research_source != "manual" -- a hand-corrected field can never be silently
    clobbered by a later automated research pass. researched_at is always set to
    now, regardless of whether any field was actually supplied, so a company that
    genuinely has no useful public information is still marked "looked, found
    nothing" rather than being re-surfaced by persist_email_analysis's
    new_organizations_needing_research signal on every future email.

    Raises ValueError if org_id doesn't exist.
    """
    repo = OrganizationRepository(db)
    org = repo.find_one({"id": org_id})
    if org is None:
        raise ValueError(f"no organization found for org_id={org_id!r}")

    protect_existing = org.get("research_source") == "manual"
    supplied = {
        "industry": industry, "description": description,
        "products_services": products_services, "size_estimate": size_estimate,
        "headquarters": headquarters, "website": website,
    }
    update: dict[str, Any] = {
        field: value for field, value in supplied.items() if value is not None and not protect_existing
    }
    update["research_source"] = org.get("research_source") if protect_existing else research_source
    update["researched_at"] = datetime.now(timezone.utc)

    repo.upsert_by_key({"id": org_id}, {**org, **update})
    return {**org, **update}


def list_unresearched_organizations(db: Database) -> list[dict[str, Any]]:
    """Read-only catch-up list: every active Organization that has never been
    researched (researched_at is None), independent of
    persist_email_analysis's per-email new_organizations_needing_research
    signal -- for a company created but never mentioned again in a later email,
    or a session that lacked WebSearch access when the signal first fired.
    """
    return [
        org for org in OrganizationRepository(db).find_many({"researched_at": None})
        if org.get("status", "active") == "active"
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k organization_research -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: add persist_organization_research and list_unresearched_organizations tools"
```

---

## Task 3: `new_organizations_needing_research` signal in `persist_email_analysis`

**Files:**
- Modify: `app/mcp/tools.py:271-284` (inside `persist_email_analysis`, between `_process_entities` and the final `return`)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `entities_referenced["people"]` (list of `person_id` strings, already produced by `_process_entities`), `PersonRepository`/`OrganizationRepository` (already imported after Task 2).
- Produces: `persist_email_analysis`'s return dict gains `"new_organizations_needing_research": list[{"org_id": str, "name": str, "domain": str | None}]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_deterministic_tools.py` (uses the existing `_analysis`/`_raw_email` helpers and `db`/`settings` fixtures already in this file; add `MentionedPerson` to the existing `from app.analysis.schemas import ...` line if not already present — it already is, per the file's current imports):

```python
def test_persist_email_analysis_surfaces_a_new_organization_needing_research(db, settings):
    raw = _raw_email("msg_001")
    email = parse_email(raw)
    ingest_result = tools.ingest_email(db, email)

    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    assert len(result["new_organizations_needing_research"]) == 1
    new_org = result["new_organizations_needing_research"][0]
    assert new_org["name"] == "NewCo"
    assert new_org["domain"] == "newco.com"


def test_persist_email_analysis_surfaces_every_new_organization_in_one_email(db, settings):
    raw = _raw_email("msg_001")
    email = parse_email(raw)
    ingest_result = tools.ingest_email(db, email)

    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[
            MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo"),
            MentionedPerson(name="Raj Patel", email="raj@othernewco.com", org="OtherNewCo"),
        ],
    )

    result = tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    surfaced_domains = {o["domain"] for o in result["new_organizations_needing_research"]}
    assert surfaced_domains == {"newco.com", "othernewco.com"}


def test_persist_email_analysis_resurfaces_an_unresearched_org_on_a_later_email(db, settings):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis1 = _analysis(
        ingest1["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    tools.persist_email_analysis(db, ingest1["message_id"], analysis1, settings)

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    assert len(result2["new_organizations_needing_research"]) == 1


def test_persist_email_analysis_stops_surfacing_an_org_once_researched(db, settings):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis1 = _analysis(
        ingest1["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    result1 = tools.persist_email_analysis(db, ingest1["message_id"], analysis1, settings)
    org_id = result1["new_organizations_needing_research"][0]["org_id"]
    tools.persist_organization_research(db, org_id, industry="Software")

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    result2 = tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    assert result2["new_organizations_needing_research"] == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k new_organization -v`
Expected: FAIL with `KeyError: 'new_organizations_needing_research'`

- [ ] **Step 3: Implement**

In `app/mcp/tools.py`, inside `persist_email_analysis`, directly after the existing `_link_knowledge_to_entities(db, thread_id, message_id)` / `_link_thread_to_entities(db, thread_id)` pair (i.e. right before the `sender_person = resolve_canonical_person_for_email(...)` line), add:

```python
    # New organizations needing research (Knowledge Layer): collect every org_id
    # this email's resolved people actually belong to, then surface whichever of
    # those organizations has never been researched. Computed fresh from
    # researched_at on every call, not a one-shot "just created" flag, so a
    # session that lacks WebSearch access doesn't permanently lose the chance --
    # the next email mentioning the same org surfaces it again.
    person_repo = PersonRepository(db)
    org_repo = OrganizationRepository(db)
    referenced_org_ids = {
        person["org_id"]
        for person_id in entities_referenced.get("people", [])
        if (person := person_repo.find_one({"id": person_id})) and person.get("org_id")
    }
    new_organizations_needing_research = [
        {"org_id": org["id"], "name": org["name"], "domain": org.get("domain")}
        for org_id in referenced_org_ids
        if (org := org_repo.find_one({"id": org_id})) and org.get("researched_at") is None
    ]
```

Then add the new key to the final `return` dict (right after `"entities_referenced": entities_referenced,`):

```python
    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "entities_referenced": entities_referenced,
        "new_organizations_needing_research": new_organizations_needing_research,
        "possible_missed_commitment": possible_missed_commitment,
        ...
```

(keep every other existing key unchanged).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k new_organization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: surface new_organizations_needing_research from persist_email_analysis"
```

---

## Task 4: Explicit `WORKS_AT` knowledge fact on first `org_id` attribution

**Files:**
- Modify: `app/mcp/tools.py` (inside `persist_email_analysis`, add `KnowledgeItem`/`HistoryEntry` import; add a loop after Task 3's block)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `entities_referenced["people"]`, `person_repo` (from Task 3, same function scope), `knowledge_repo` (already defined earlier in `persist_email_analysis` as `KnowledgeRepository(db)`).
- Produces: a `KnowledgeItem` row with `predicate="works_at"` per person, found via `KnowledgeRepository(db).find_one({"person_id": ..., "predicate": "works_at"})`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_deterministic_tools.py` (add `from app.knowledge.models import KnowledgeItem` is not needed for the test itself, only `KnowledgeRepository`, already imported in this file):

```python
def test_persist_email_analysis_creates_a_works_at_knowledge_fact(db, settings):
    ingest_result = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis = _analysis(
        ingest_result["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )

    tools.persist_email_analysis(db, ingest_result["message_id"], analysis, settings)

    person = PersonRepository(db).find_one({"email": "jane@newco.com"})
    fact = KnowledgeRepository(db).find_one({"person_id": person["id"], "predicate": "works_at"})
    assert fact is not None
    assert fact["current_value"] == person["org_id"]
    assert fact["org_id"] == person["org_id"]
    assert fact["basis"] == "stated"


def test_persist_email_analysis_does_not_duplicate_works_at_fact_on_a_later_email(db, settings):
    ingest1 = tools.ingest_email(db, parse_email(_raw_email("msg_001")))
    analysis1 = _analysis(
        ingest1["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    tools.persist_email_analysis(db, ingest1["message_id"], analysis1, settings)

    ingest2 = tools.ingest_email(db, parse_email(_raw_email("msg_002", subject="Follow-up")))
    analysis2 = _analysis(
        ingest2["message_id"],
        people_mentioned=[MentionedPerson(name="Jane Smith", email="jane@newco.com", org="NewCo")],
    )
    tools.persist_email_analysis(db, ingest2["message_id"], analysis2, settings)

    person = PersonRepository(db).find_one({"email": "jane@newco.com"})
    facts = KnowledgeRepository(db).find_many({"person_id": person["id"], "predicate": "works_at"})
    assert len(facts) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k works_at -v`
Expected: FAIL with `AssertionError: assert None is not None`

- [ ] **Step 3: Implement**

In `app/mcp/tools.py`, add to the existing `from app.replies.models import ReplyDraft, ReplyDraftContent` import area a new line:

```python
from app.knowledge.models import HistoryEntry, KnowledgeItem
```

Then, directly after Task 3's `new_organizations_needing_research` block (same function, same scope — reuses the `person_repo` variable Task 3 already defined), add:

```python
    # Explicit Person -> WORKS_AT -> Organization knowledge fact (Knowledge
    # Layer): Person.org_id already IS this relationship as a silent FK; this
    # records it as an auditable KnowledgeItem too, with history/confidence/
    # source_emails like every other extracted fact. Looked up directly by
    # (person_id, predicate) -- not process_new_fact's fuzzy text-similarity
    # dedup, since a WORKS_AT fact's identity is always exactly one person and
    # one org_id, never free text requiring fuzzy matching.
    for person_id in entities_referenced.get("people", []):
        person = person_repo.find_one({"id": person_id})
        if not person or not person.get("org_id"):
            continue
        if knowledge_repo.find_one({"person_id": person_id, "predicate": "works_at"}) is not None:
            continue
        now = datetime.now(timezone.utc)
        works_at_fact = KnowledgeItem(
            knowledge_id=f"knowledge_{thread_id}_{person_id}_works_at",
            thread_id=thread_id,
            subject_key=person_id,
            predicate="works_at",
            fact_key="works_at",
            current_value=person["org_id"],
            person_id=person_id,
            org_id=person["org_id"],
            history=[HistoryEntry(value=person["org_id"], source_email_id=message_id, recorded_at=now)],
            source_emails=[message_id],
            basis="stated",
            first_seen_at=now,
            last_confirmed_at=now,
            confidence=0.95,
        )
        knowledge_repo.upsert_by_key(
            {"knowledge_id": works_at_fact.knowledge_id}, works_at_fact.model_dump(mode="json")
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k works_at -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: record an explicit WORKS_AT knowledge fact when a person's org_id is set"
```

---

## Task 5: Fix `get_company_summary` to join on `org_id`

**Files:**
- Modify: `app/mcp/tools.py:1052-1100` (replace `get_company_summary` entirely)
- Modify: `app/mcp/server.py:423-432` (the `get_company_summary` tool wrapper)
- Modify: `app/api/routers/organizations.py` (the `get_organization_route` handler)
- Modify: `frontend/src/api/types.ts:122-134` (`OrganizationSummary.summary` — drop the dead `org: string` field)
- Test: `tests/test_mcp_query_tools.py`

**Interfaces:**
- Consumes: `OrganizationRepository`, `PersonRepository`, `ProjectRepository`, `CommitmentRepository`, `FollowUpRepository`, `MeetingRepository`, `KnowledgeRepository` (all already imported in `app/mcp/tools.py`).
- Produces: `get_company_summary(db, org_id) -> dict[str, Any] | None` — `None` for an unknown `org_id` (matching `get_project_summary`'s own established convention for this file), otherwise `{"people", "projects", "related_commitments", "related_follow_ups", "related_meetings", "related_knowledge_items", "relationship_notes"}`.

- [ ] **Step 1: Write the failing tests**

Replace the three existing tests in `tests/test_mcp_query_tools.py` (`test_get_company_summary_returns_people_and_projects_for_org`, `test_get_company_summary_finds_commitments_indirectly_through_matching_projects`, `test_get_company_summary_returns_empty_lists_for_unknown_org_never_erroring`) with:

```python
def test_get_company_summary_returns_people_and_projects_by_org_id(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "Speedvision", "domain": "speedvision.com", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"}, {"id": "ORG-002", "name": "Other Co", "domain": "otherco.com", "aliases": [], "source": "gmail"}
    )
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person("PER-001", org_id="ORG-001"))
    PersonRepository(db).upsert_by_key({"id": "PER-002"}, _person("PER-002", org_id="ORG-002"))
    ProjectRepository(db).upsert_by_key({"id": "PRJ-001"}, _project("PRJ-001", org_id="ORG-001"))
    ProjectRepository(db).upsert_by_key({"id": "PRJ-002"}, _project("PRJ-002", org_id="ORG-002"))

    result = tools.get_company_summary(db, "ORG-001")

    assert [p["id"] for p in result["people"]] == ["PER-001"]
    assert [p["id"] for p in result["projects"]] == ["PRJ-001"]


def test_get_company_summary_finds_commitments_meetings_and_knowledge_directly_by_org_id(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "Speedvision", "domain": "speedvision.com", "aliases": [], "source": "gmail"}
    )
    CommitmentRepository(db).upsert_by_key({"id": "COM-001"}, _commitment("COM-001", "t1", org_id="ORG-001"))
    FollowUpRepository(db).upsert_by_key({"id": "FUP-001"}, _follow_up("FUP-001", org_id="ORG-001", thread_id="t1"))
    MeetingRepository(db).upsert_by_key({"id": "MTG-001"}, _meeting("MTG-001", "t1", org_id="ORG-001"))
    KnowledgeRepository(db).upsert_by_key(
        {"knowledge_id": "KNOW-001"},
        {
            "knowledge_id": "KNOW-001", "thread_id": "t1", "subject_key": "speedvision", "predicate": "has",
            "fact_key": "employee_count", "current_value": "500", "person_id": None, "org_id": "ORG-001",
            "history": [], "source_emails": ["m1"], "basis": "stated",
            "first_seen_at": "2026-09-10T09:00:00", "last_confirmed_at": "2026-09-10T09:00:00",
            "confidence": 0.9, "status": "active",
        },
    )

    result = tools.get_company_summary(db, "ORG-001")

    assert [c["id"] for c in result["related_commitments"]] == ["COM-001"]
    assert [f["id"] for f in result["related_follow_ups"]] == ["FUP-001"]
    assert [m["id"] for m in result["related_meetings"]] == ["MTG-001"]
    assert [k["knowledge_id"] for k in result["related_knowledge_items"]] == ["KNOW-001"]


def test_get_company_summary_returns_empty_lists_for_a_real_org_with_no_related_records(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "Nobody Inc", "domain": "nobody.example", "aliases": [], "source": "gmail"}
    )

    result = tools.get_company_summary(db, "ORG-001")

    assert result is not None
    assert result["people"] == []
    assert result["projects"] == []
    assert result["related_commitments"] == []
    assert result["related_meetings"] == []
    assert result["related_knowledge_items"] == []


def test_get_company_summary_returns_none_for_unknown_org_id(db):
    assert tools.get_company_summary(db, "ORG-999") is None
```

Add `KnowledgeRepository` and `OrganizationRepository` to the existing `from app.database.repositories import (...)` block at the top of this test file. Also update the existing cross-cutting parametrized test: change `lambda db: tools.get_company_summary(db, "Nobody"),` (around line 1074) to `lambda db: tools.get_company_summary(db, "ORG-999"),`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_query_tools.py -k get_company_summary -v`
Expected: FAIL (old tests removed/changed; new ones fail against the old string-matching implementation — `result["people"]` empty when it should have one entry, etc.)

- [ ] **Step 3: Implement**

Replace `get_company_summary` in `app/mcp/tools.py` entirely:

```python
def get_company_summary(db: Database, org_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary for an organization, assembled with
    plain Mongo queries in application code -- no LLM involved. Returns None
    for an org_id that doesn't exist, matching get_project_summary's own
    convention for an unknown id.

    Takes a canonical org_id (not a free-text name): every related collection
    (Person, Project, Commitment, FollowUp, Meeting, KnowledgeItem) already
    carries a real org_id reference, populated by the pipeline's own entity
    resolution -- this function joins on that FK directly. An earlier version
    of this tool matched Person.org/Project.entity by free-text string
    equality; Person.org is confirmed never populated by any resolution path,
    so that version silently returned an empty people list for every real
    organization, and hardcoded related_meetings/related_knowledge_items to []
    with a docstring claiming the schema couldn't support them -- both
    Meeting.org_id and KnowledgeItem.org_id already exist and are populated.
    """
    org = OrganizationRepository(db).find_one({"id": org_id})
    if org is None:
        return None

    return {
        "people": PersonRepository(db).find_many({"org_id": org_id}),
        "projects": ProjectRepository(db).find_many({"org_id": org_id}),
        "related_commitments": CommitmentRepository(db).find_many({"org_id": org_id}),
        "related_follow_ups": FollowUpRepository(db).find_many({"org_id": org_id}),
        "related_meetings": MeetingRepository(db).find_many({"org_id": org_id}),
        "related_knowledge_items": KnowledgeRepository(db).find_many({"org_id": org_id}),
        "relationship_notes": {
            "people": "direct: Person.org_id == org_id",
            "projects": "direct: Project.org_id == org_id",
            "related_commitments": "direct: Commitment.org_id == org_id",
            "related_follow_ups": "direct: FollowUp.org_id == org_id",
            "related_meetings": "direct: Meeting.org_id == org_id",
            "related_knowledge_items": "direct: KnowledgeItem.org_id == org_id",
        },
    }
```

In `app/mcp/server.py`, replace the `get_company_summary` tool (lines 423-432):

```python
@mcp.tool()
def get_company_summary(org_id: str) -> dict[str, Any] | None:
    """Read-only cross-collection summary for one organization (ORG-xxx): people,
    projects, commitments, follow-ups, meetings, and knowledge items with a direct
    org_id reference. See `relationship_notes` in the response for exactly which
    relationship each list represents. Returns None if the organization doesn't
    exist. No LLM involved -- deterministic MongoDB queries only.
    """
    return tools.get_company_summary(_get_db(), org_id)
```

In `app/api/routers/organizations.py`, replace `get_organization_route`:

```python
@router.get("/{org_id}")
def get_organization_route(org_id: str, db=Depends(get_db)) -> dict[str, Any]:
    """360 view -- reuses app.mcp.tools.get_company_summary directly by org_id,
    the same id this route itself takes (get_company_summary used to require
    the organization's free-text name instead; that round-trip is gone now
    that the tool joins on org_id directly)."""
    org = OrganizationRepository(db).find_one({"id": org_id})
    if org is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no organization found for id={org_id!r}")
    return {"organization": org, "summary": get_company_summary(db, org_id)}
```

In `frontend/src/api/types.ts`, remove the dead `org: string` line from `OrganizationSummary.summary` (nothing in the frontend reads it — confirmed `OrganizationDetailPage.tsx` only reads `related.people/projects/related_commitments/related_follow_ups`):

```typescript
export interface OrganizationSummary {
  organization: OrganizationRow
  summary: {
    people: PersonRow[]
    projects: ProjectRow[]
    related_commitments: Record<string, unknown>[]
    related_follow_ups: Record<string, unknown>[]
    related_meetings: Record<string, unknown>[]
    related_knowledge_items: Record<string, unknown>[]
    relationship_notes: Record<string, string>
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_query_tools.py -v`
Expected: PASS (full file, since the shared parametrized test was also edited)

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py app/mcp/server.py app/api/routers/organizations.py frontend/src/api/types.ts tests/test_mcp_query_tools.py
git commit -m "fix: get_company_summary now joins on org_id instead of dead free-text fields"
```

---

## Task 6: `preview_duplicate_organization_candidates`

**Files:**
- Modify: `app/mcp/tools.py` (add `import re` is already present at the top of the file; add the new function above `get_company_summary`)
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: `OrganizationRepository`.
- Produces: `preview_duplicate_organization_candidates(db) -> list[dict[str, Any]]`, each entry `{"org_a_id", "org_a_name", "org_b_id", "org_b_name", "evidence"}`. Read-only; never merges anything. Task 8's `merge_organization_records` is the separate, explicit execution step.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_deterministic_tools.py`:

```python
def test_preview_duplicate_organization_candidates_flags_a_name_variant_pair(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"},
        {"id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [], "source": "gmail"},
    )

    result = tools.preview_duplicate_organization_candidates(db)

    assert len(result) == 1
    assert {result[0]["org_a_id"], result[0]["org_b_id"]} == {"ORG-001", "ORG-002"}
    assert result[0]["evidence"] == "name_match"


def test_preview_duplicate_organization_candidates_flags_a_website_domain_cross_match(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "Acme Corp", "domain": "acme-internal.example", "aliases": [], "source": "gmail", "website": "https://acme.com"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"}, {"id": "ORG-002", "name": "Totally Different Name", "domain": "acme.com", "aliases": [], "source": "gmail"}
    )

    result = tools.preview_duplicate_organization_candidates(db)

    assert len(result) == 1
    assert result[0]["evidence"] == "domain_cross_match"


def test_preview_duplicate_organization_candidates_does_not_flag_unrelated_orgs(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"}, {"id": "ORG-002", "name": "Totally Unrelated Co", "domain": "unrelated.com", "aliases": [], "source": "gmail"}
    )

    assert tools.preview_duplicate_organization_candidates(db) == []


def test_preview_duplicate_organization_candidates_ignores_already_merged_orgs(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {
            "id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [],
            "source": "gmail", "status": "merged", "merged_into": "ORG-001",
        },
        {
            "id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [],
            "source": "gmail", "status": "merged", "merged_into": "ORG-001",
        },
    )

    assert tools.preview_duplicate_organization_candidates(db) == []
```

(Note the last test passes the same dict as both the key and the document to `upsert_by_key` -- fine, since `upsert_by_key`'s key dict only needs to contain `id`; any extra keys in it are simply ignored by the key filter and the full document arg is what's actually stored. If that double-argument style looks off during implementation, the simpler equivalent `OrganizationRepository(db).upsert_by_key({"id": "ORG-002"}, {...})` works identically.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k preview_duplicate_organization -v`
Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'preview_duplicate_organization_candidates'`

- [ ] **Step 3: Implement**

Add to `app/mcp/tools.py`, above `get_company_summary`:

```python
_ORG_SUFFIX_TOKENS = {
    "inc", "llc", "ltd", "corp", "corporation", "group",
    "technologies", "analytics", "solutions", "co",
}


def _org_name_tokens(name: str) -> set[str]:
    lowered = re.sub(r"[^\w\s]", "", name.lower())
    return {t for t in lowered.split() if t not in _ORG_SUFFIX_TOKENS}


def _org_website_domain(org: dict[str, Any]) -> str | None:
    website = org.get("website")
    if not website:
        return None
    stripped = re.sub(r"^https?://", "", website).split("/")[0]
    return stripped[4:] if stripped.startswith("www.") else stripped


def preview_duplicate_organization_candidates(db: Database) -> list[dict[str, Any]]:
    """Read-only heuristic preview of organizations that may be the same real
    company under different names/domains (e.g. "DataBeat" / "DataBeat
    Analytics" / databeat.com) -- resolve_organization's own automatic path
    stays domain-only and never merges these; this is the human/Claude-
    reviewed catch for everything domain-matching can't see. Never merges
    anything itself -- see merge_organization_records for the explicit,
    caller-confirmed execution step. Already-merged organizations are never
    candidates (status != "active" is skipped entirely).

    A pair is flagged when, after normalizing both names (lowercase, strip
    punctuation, drop common suffix words: Inc/LLC/Ltd/Corp/Corporation/
    Group/Technologies/Analytics/Solutions/Co), their non-empty token sets
    are equal or one is a subset of the other ("name_match"), OR one
    organization's website domain matches the other's stored domain or an
    alias ("domain_cross_match").
    """
    orgs = [o for o in OrganizationRepository(db).find_many({}) if o.get("status", "active") == "active"]
    candidates: list[dict[str, Any]] = []

    for i, a in enumerate(orgs):
        a_tokens = _org_name_tokens(a["name"])
        a_website_domain = _org_website_domain(a)
        for b in orgs[i + 1:]:
            b_tokens = _org_name_tokens(b["name"])
            b_website_domain = _org_website_domain(b)

            name_match = bool(a_tokens) and bool(b_tokens) and (
                a_tokens == b_tokens or a_tokens <= b_tokens or b_tokens <= a_tokens
            )
            domain_cross_match = (
                (a_website_domain is not None and a_website_domain == b.get("domain"))
                or (b_website_domain is not None and b_website_domain == a.get("domain"))
                or (a.get("domain") and a["domain"] in (b.get("aliases") or []))
                or (b.get("domain") and b["domain"] in (a.get("aliases") or []))
            )

            if not (name_match or domain_cross_match):
                continue

            candidates.append(
                {
                    "org_a_id": a["id"], "org_a_name": a["name"],
                    "org_b_id": b["id"], "org_b_name": b["name"],
                    "evidence": "name_match" if name_match else "domain_cross_match",
                }
            )

    return candidates
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_deterministic_tools.py -k preview_duplicate_organization -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: add preview_duplicate_organization_candidates heuristic"
```

---

## Task 7: Organization lifecycle module + merged-record redirect in `resolve_organization`

**Files:**
- Create: `app/entities/organization_lifecycle.py`
- Modify: `app/entities/resolution.py:79-109` (`resolve_organization`)
- Test: `tests/test_entities_organization_lifecycle.py` (new)
- Test: `tests/test_entities_resolution.py`

**Interfaces:**
- Produces: `is_organization_merged(org) -> bool`, `is_organization_active(org) -> bool`, `resolve_canonical_organization_id(db, org_id) -> str` (raises `OrganizationCanonicalResolutionError`), all mirroring `app/entities/lifecycle.py`'s Person equivalents exactly. `resolve_organization` gains the same merged-record redirect behavior Task 8's `merge_organization_records` depends on for safety.
- Consumes: nothing new beyond what's already imported in `resolution.py` (`OrganizationRepository`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_entities_organization_lifecycle.py`:

```python
# tests/test_entities_organization_lifecycle.py
"""The centralized Organization lifecycle contract, mirroring
tests/test_entities_lifecycle.py's Person-lifecycle tests exactly. All
mongomock only.
"""
import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import OrganizationRepository
from app.entities.organization_lifecycle import (
    OrganizationCanonicalResolutionError,
    is_organization_active,
    is_organization_merged,
    resolve_canonical_organization_id,
)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _org(**overrides):
    org_id = overrides.get("id", "ORG-1")
    doc = {"id": org_id, "name": "Someone Co", "domain": f"{org_id.lower()}.example", "aliases": [], "source": "gmail"}
    doc.update(overrides)
    return doc


def test_missing_status_means_active():
    org = _org()
    org.pop("status", None)
    assert is_organization_active(org) is True
    assert is_organization_merged(org) is False


def test_status_merged_means_retired():
    org = _org(status="merged", merged_into="ORG-2")
    assert is_organization_active(org) is False
    assert is_organization_merged(org) is True


def test_resolve_canonical_organization_id_returns_self_when_active(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-1"}, _org(status="active"))
    assert resolve_canonical_organization_id(db, "ORG-1") == "ORG-1"


def test_resolve_canonical_organization_id_follows_merged_into(db):
    OrganizationRepository(db).upsert_by_key({"id": "ORG-2"}, _org(id="ORG-2", status="active"))
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-1"}, _org(id="ORG-1", status="merged", merged_into="ORG-2")
    )
    assert resolve_canonical_organization_id(db, "ORG-1") == "ORG-2"


def test_resolve_canonical_organization_id_raises_on_broken_chain(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-1"}, _org(id="ORG-1", status="merged", merged_into="ORG-404-DOES-NOT-EXIST")
    )
    with pytest.raises(OrganizationCanonicalResolutionError):
        resolve_canonical_organization_id(db, "ORG-1")
```

Add to `tests/test_entities_resolution.py`, directly after the existing `test_resolve_organization_returns_none_without_a_domain` test:

```python
def test_resolve_organization_redirects_through_a_merged_organization(db):
    # Pin: after an org merge (Task 8), a brand-new person at the SOURCE org's
    # domain must resolve to the canonical (target) org, never resurrect the
    # retired source -- this is the exact bug a domain-only lookup with no
    # lifecycle awareness would otherwise reintroduce.
    source_org_id = resolve_organization(db, "first@databeat.io", name_hint="DataBeat")
    target_org_id = resolve_organization(db, "first@differentcompany.example", name_hint="Different Co")
    OrganizationRepository(db).upsert_by_key(
        {"id": source_org_id},
        {**OrganizationRepository(db).find_one({"id": source_org_id}), "status": "merged", "merged_into": target_org_id},
    )

    resolved = resolve_organization(db, "second@databeat.io", name_hint="DataBeat")

    assert resolved == target_org_id
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_entities_organization_lifecycle.py tests/test_entities_resolution.py -k "organization_lifecycle or redirects_through_a_merged" -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.entities.organization_lifecycle'`

- [ ] **Step 3: Implement**

Create `app/entities/organization_lifecycle.py` (mirrors `app/entities/lifecycle.py` exactly, substituting Organization for Person):

```python
"""The single, centralized Organization lifecycle contract -- mirrors
app.entities.lifecycle's Person contract exactly. Nothing else in the codebase
should inline an `org.get("status") == "merged"` check.

Contract (see app.entities.models.Organization.status/merged_into):
  - status missing entirely (every historical document predating this field) -> ACTIVE
  - status == "active"                                                        -> ACTIVE
  - status == "merged"                                                        -> MERGED, with
    merged_into pointing at the canonical replacement

This module never writes to MongoDB and never repairs a broken merged_into chain --
it only ever reports (as an explicit exception) that one is broken.
"""

from typing import Any

from pymongo.database import Database

from app.database.repositories import OrganizationRepository

ACTIVE = "active"
MERGED = "merged"

_MAX_CHAIN_DEPTH = 10


class OrganizationCanonicalResolutionError(Exception):
    """A merged_into chain could not be safely followed to an active
    Organization -- missing target, self-reference, a cycle, or a chain deeper
    than _MAX_CHAIN_DEPTH. Callers must handle this explicitly; nothing in
    this module ever repairs the chain or invents a replacement Organization.
    """


def is_organization_merged(org: dict[str, Any]) -> bool:
    return org.get("status") == MERGED


def is_organization_active(org: dict[str, Any]) -> bool:
    return not is_organization_merged(org)


def resolve_canonical_organization_id(db: Database, org_id: str) -> str:
    """Returns the id of the ACTIVE Organization that `org_id` should be
    treated as today: itself, if already active; otherwise the result of
    following `merged_into` repeatedly until an active Organization is
    reached. Raises OrganizationCanonicalResolutionError (never returns a
    guess) on a missing target, self-reference, cycle, or a chain exceeding
    _MAX_CHAIN_DEPTH.
    """
    repo = OrganizationRepository(db)
    seen: set[str] = set()
    current_id = org_id

    for _ in range(_MAX_CHAIN_DEPTH):
        if current_id in seen:
            raise OrganizationCanonicalResolutionError(
                f"circular merged_into chain detected starting at {org_id!r} (revisited {current_id!r})"
            )
        seen.add(current_id)

        org = repo.find_one({"id": current_id})
        if org is None:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} does not exist (broken merged_into chain starting at {org_id!r})"
            )
        if is_organization_active(org):
            return current_id

        target = org.get("merged_into")
        if not target:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} is status='merged' but has no merged_into target"
            )
        if target == current_id:
            raise OrganizationCanonicalResolutionError(
                f"organization {current_id!r} has merged_into pointing at itself"
            )
        current_id = target

    raise OrganizationCanonicalResolutionError(
        f"merged_into chain from {org_id!r} exceeded {_MAX_CHAIN_DEPTH} hops without reaching an active "
        "organization -- possible undetected cycle or pathological chain"
    )
```

Replace `resolve_organization` in `app/entities/resolution.py`:

```python
def resolve_organization(db: Database, email: str | None, name_hint: str | None = None) -> str | None:
    """Canonical Organization resolution (Part 4 of the global-identity design) --
    mirrors resolve_person's own hierarchy: the email DOMAIN is the strongest, safest
    signal, exactly analogous to a full email address for a Person. Returns None
    whenever no domain is available -- a company NAME alone is never sufficient to
    establish or look up a canonical organization; text-similarity-only merging
    ("DataBeat" vs "DataBeat Inc." vs "databeat") is deliberately out of scope here,
    since two different real companies can share a very similar display name --
    see app.mcp.tools.preview_duplicate_organization_candidates/
    merge_organization_records for that, as an explicit, caller-confirmed step.

    If the domain-matched Organization has itself been merged into another one
    (app.entities.organization_lifecycle), this transparently redirects to the
    canonical replacement instead -- a domain match must never resurrect a
    retired, merged organization record.
    """
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].strip().lower()
    if not domain:
        return None

    repo = OrganizationRepository(db)
    existing = repo.find_one({"domain": domain})
    if existing:
        target = existing
        if is_organization_merged(existing):
            canonical_id = resolve_canonical_organization_id(db, existing["id"])
            target = repo.find_one({"id": canonical_id})
        # Enrichment, not migration: the org may have been created earlier with no
        # real display name -- a later call that DOES have one fills it in, but
        # only while the stored name is still just the domain itself; never
        # overwrites a real name that's already been recorded. Applied to
        # whichever record is actually canonical, never to a retired one.
        if name_hint and target.get("name") == target.get("domain"):
            repo.upsert_by_key({"id": target["id"]}, {**target, "name": name_hint})
        return target["id"]

    org_id = next_id(db, "ORG-")
    org = Organization(id=org_id, name=name_hint or domain, domain=domain)
    repo.upsert_by_key({"id": org_id}, org.model_dump(mode="json"))
    return org_id
```

Add the import to the top of `app/entities/resolution.py`:

```python
from app.entities.organization_lifecycle import is_organization_merged, resolve_canonical_organization_id
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_entities_organization_lifecycle.py tests/test_entities_resolution.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/entities/organization_lifecycle.py app/entities/resolution.py tests/test_entities_organization_lifecycle.py tests/test_entities_resolution.py
git commit -m "feat: add Organization lifecycle contract and merged-record redirect in resolve_organization"
```

---

## Task 8: `merge_organization_records`

**Files:**
- Modify: `app/duplicate_consolidation.py` (add `OpportunityRepository` to the existing repository import block; add `_ORG_COLLECTION_SPECS` and `_execute_one_org_mapping`)
- Modify: `app/mcp/tools.py` (import `_execute_one_org_mapping`; add `merge_organization_records`)
- Test: `tests/test_duplicate_consolidation.py`
- Test: `tests/test_mcp_deterministic_tools.py`

**Interfaces:**
- Consumes: Task 1's `Organization` lifecycle fields, Task 7's redirect (so a merge's effects are immediately safe against future domain lookups).
- Produces: `merge_organization_records(db, source_org_id, target_org_id) -> dict[str, Any]` (raises `ValueError` for an unknown id, same id, or an already-merged source).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_duplicate_consolidation.py` (add `OpportunityRepository` to its existing `from app.database.repositories import (...)` block):

```python
def test_execute_one_org_mapping_repoints_every_referencing_collection(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"}, {"id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [], "source": "gmail"}
    )
    PersonRepository(db).upsert_by_key({"id": "PER-001"}, _person(id="PER-001", org_id="ORG-002"))
    ProjectRepository(db).upsert_by_key(
        {"id": "PRJ-001"}, {"id": "PRJ-001", "project": "Renewal", "org_id": "ORG-002"}
    )
    CommitmentRepository(db).upsert_by_key(
        {"id": "COM-001"},
        {
            "id": "COM-001", "what": "send pricing", "class": "mine", "source_record": "msg_001",
            "made_on": "2026-09-13T10:30:00Z", "status": "open", "org_id": "ORG-002", "thread_id": "t1",
        },
    )

    result = _execute_one_org_mapping(db, "ORG-002", "ORG-001")

    assert result["status"] == "COMPLETED"
    assert result["updated_counts"]["people"] == 1
    assert result["updated_counts"]["projects"] == 1
    assert result["updated_counts"]["commitments"] == 1
    assert PersonRepository(db).find_one({"id": "PER-001"})["org_id"] == "ORG-001"
    assert ProjectRepository(db).find_one({"id": "PRJ-001"})["org_id"] == "ORG-001"
    assert CommitmentRepository(db).find_one({"id": "COM-001"})["org_id"] == "ORG-001"

    source = OrganizationRepository(db).find_one({"id": "ORG-002"})
    assert source["status"] == "merged"
    assert source["merged_into"] == "ORG-001"


def test_execute_one_org_mapping_unions_aliases_and_backfills_blank_profile_fields(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"},
        {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": ["DB"], "source": "gmail"},
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"},
        {
            "id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com",
            "aliases": [], "source": "gmail", "industry": "Data Analytics",
        },
    )

    _execute_one_org_mapping(db, "ORG-002", "ORG-001")

    target = OrganizationRepository(db).find_one({"id": "ORG-001"})
    assert set(target["aliases"]) == {"DB", "DataBeat Analytics"}
    assert target["industry"] == "Data Analytics"
```

Add to `tests/test_mcp_deterministic_tools.py`:

```python
def test_merge_organization_records_repoints_and_retires_the_source(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"}, {"id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [], "source": "gmail"}
    )
    PersonRepository(db).upsert_by_key(
        {"id": "PER-001"},
        {"id": "PER-001", "name": "Jane", "email": "jane@databeat-analytics.com", "aliases": [], "org_id": "ORG-002", "open_threads": []},
    )

    result = tools.merge_organization_records(db, "ORG-002", "ORG-001")

    assert result["status"] == "COMPLETED"
    assert PersonRepository(db).find_one({"id": "PER-001"})["org_id"] == "ORG-001"


def test_merge_organization_records_raises_for_unknown_ids(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    with pytest.raises(ValueError, match="ORG-999"):
        tools.merge_organization_records(db, "ORG-999", "ORG-001")
    with pytest.raises(ValueError, match="ORG-999"):
        tools.merge_organization_records(db, "ORG-001", "ORG-999")


def test_merge_organization_records_raises_for_same_id(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    with pytest.raises(ValueError, match="must be different"):
        tools.merge_organization_records(db, "ORG-001", "ORG-001")


def test_merge_organization_records_raises_if_source_already_merged(db):
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-001"}, {"id": "ORG-001", "name": "DataBeat", "domain": "databeat.io", "aliases": [], "source": "gmail"}
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-002"},
        {
            "id": "ORG-002", "name": "DataBeat Analytics", "domain": "databeat-analytics.com", "aliases": [],
            "source": "gmail", "status": "merged", "merged_into": "ORG-001",
        },
    )
    OrganizationRepository(db).upsert_by_key(
        {"id": "ORG-003"}, {"id": "ORG-003", "name": "Third Co", "domain": "thirdco.com", "aliases": [], "source": "gmail"}
    )

    with pytest.raises(ValueError, match="already merged"):
        tools.merge_organization_records(db, "ORG-002", "ORG-003")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_duplicate_consolidation.py tests/test_mcp_deterministic_tools.py -k "org_mapping or merge_organization" -v`
Expected: FAIL with `ImportError`/`AttributeError` (functions don't exist yet)

- [ ] **Step 3: Implement**

In `app/duplicate_consolidation.py`, add `OpportunityRepository` to the existing repository import block:

```python
from app.database.repositories import (
    CalendarActionRepository,
    CommitmentRepository,
    FollowUpRepository,
    KnowledgeRepository,
    MeetingRepository,
    OpportunityRepository,
    OrganizationRepository,
    PersonRepository,
    ProjectRepository,
    ReplyDraftRepository,
    ThreadRepository,
)
```

Then add, at the end of the file:

```python
# --- Organization merge (mirrors Person merge above, but simpler: org_id is ----
# --- always a scalar field on every referencing collection -- there is no ----
# --- list-valued org_id anywhere in this schema, so no _repoint_list_field ----
# --- equivalent is needed here). -----------------------------------------------


_ORG_COLLECTION_SPECS = [
    ("people", PersonRepository, lambda d: {"id": d["id"]}),
    ("projects", ProjectRepository, lambda d: {"id": d["id"]}),
    ("opportunities", OpportunityRepository, lambda d: {"id": d["id"]}),
    ("commitments", CommitmentRepository, lambda d: {"id": d["id"]}),
    ("follow_ups", FollowUpRepository, lambda d: {"id": d["id"]}),
    ("meetings", MeetingRepository, lambda d: {"id": d["id"]}),
    ("knowledge_items", KnowledgeRepository, lambda d: {"knowledge_id": d["knowledge_id"]}),
    ("reply_drafts", ReplyDraftRepository, lambda d: {"source_email_id": d["source_email_id"]}),
    (
        "calendar_actions", CalendarActionRepository,
        lambda d: {"thread_id": d["thread_id"], "meeting_fingerprint": d["meeting_fingerprint"]},
    ),
]

_ORG_PROFILE_FIELDS = (
    "industry", "description", "products_services", "size_estimate",
    "headquarters", "website", "research_source", "researched_at",
)


def _execute_one_org_mapping(db: Database, source_org_id: str, target_org_id: str) -> dict[str, Any]:
    """Repoints org_id -- always scalar on every referencing collection, unlike
    Person's person_id/person_ids split -- from source_org_id to
    target_org_id. Mirrors _execute_one_mapping's structure (same repo-list
    pattern, same retire-last ordering).
    """
    updated_counts: dict[str, int] = defaultdict(int)

    for label, repo_cls, key_fn in _ORG_COLLECTION_SPECS:
        repo = repo_cls(db)
        for doc in repo.find_many({"org_id": source_org_id}):
            repo.upsert_by_key(key_fn(doc), {**doc, "org_id": target_org_id})
            updated_counts[label] += 1

    org_repo = OrganizationRepository(db)
    source = org_repo.find_one({"id": source_org_id})
    target = org_repo.find_one({"id": target_org_id})

    merged_aliases = list(
        dict.fromkeys([*(target.get("aliases") or []), *(source.get("aliases") or []), source["name"]])
    )
    target_update: dict[str, Any] = {"aliases": merged_aliases}
    for field in _ORG_PROFILE_FIELDS:
        if not target.get(field) and source.get(field):
            target_update[field] = source[field]
    org_repo.upsert_by_key({"id": target_org_id}, {**target, **target_update})

    org_repo.upsert_by_key(
        {"id": source_org_id}, {**source, "status": "merged", "merged_into": target_org_id}
    )

    return {"status": "COMPLETED", "updated_counts": dict(updated_counts)}
```

In `app/mcp/tools.py`, add `_execute_one_org_mapping` to the existing import line:

```python
from app.duplicate_consolidation import _execute_one_mapping, _execute_one_org_mapping, generate_merge_plan
```

Then add, directly below `preview_duplicate_organization_candidates` (Task 6):

```python
def merge_organization_records(db: Database, source_org_id: str, target_org_id: str) -> dict[str, Any]:
    """Merges one Organization record into another, for a caller (Claude,
    having reviewed a preview_duplicate_organization_candidates pair, with
    WebSearch confirmation if genuinely ambiguous) who is confident both
    records are the same real company. Unlike resolve_organization's
    automatic domain-only path, this always requires an explicit, named
    pair -- there is no automatic name-similarity merge anywhere in this
    codebase.

    Repoints org_id across every referencing collection (people, projects,
    opportunities, commitments, follow_ups, meetings, knowledge_items,
    reply_drafts, calendar_actions), unions aliases (adding the source's own
    name as an alias on the target, so a lookup by the old name still
    resolves), and backfills any blank profile/research field on the target
    from the source without overwriting a populated target field. The source
    organization is never deleted -- only retired (status="merged",
    merged_into=target_org_id), mirroring merge_person_records exactly. A
    later resolve_organization domain lookup against the retired source
    transparently redirects to the target (app.entities.organization_lifecycle).

    Raises ValueError if either id doesn't exist, if they're the same id, or
    if source_org_id is already merged into someone else.
    """
    repo = OrganizationRepository(db)
    source = repo.find_one({"id": source_org_id})
    target = repo.find_one({"id": target_org_id})
    if source is None:
        raise ValueError(f"no organization found for source_org_id={source_org_id!r}")
    if target is None:
        raise ValueError(f"no organization found for target_org_id={target_org_id!r}")
    if source_org_id == target_org_id:
        raise ValueError("source_org_id and target_org_id must be different")
    if source.get("status") == "merged":
        raise ValueError(f"org_id={source_org_id!r} is already merged into {source.get('merged_into')!r}")

    return _execute_one_org_mapping(db, source_org_id, target_org_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_duplicate_consolidation.py tests/test_mcp_deterministic_tools.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/duplicate_consolidation.py app/mcp/tools.py tests/test_duplicate_consolidation.py tests/test_mcp_deterministic_tools.py
git commit -m "feat: add merge_organization_records for domain-less organization duplicates"
```

---

## Task 9: MCP server registrations

**Files:**
- Modify: `app/mcp/server.py` (add 4 new `@mcp.tool()` wrappers, directly below `merge_person_records`)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `tools.persist_organization_research`, `tools.list_unresearched_organizations`, `tools.preview_duplicate_organization_candidates`, `tools.merge_organization_records` (Tasks 2, 6, 8).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_server.py`:

```python
def test_persist_organization_research_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "persist_organization_research" in names


def test_list_unresearched_organizations_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "list_unresearched_organizations" in names


def test_preview_duplicate_organization_candidates_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "preview_duplicate_organization_candidates" in names


def test_merge_organization_records_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "merge_organization_records" in names
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_server.py -k "persist_organization_research or unresearched_organizations or preview_duplicate_organization or merge_organization_records" -v`
Expected: FAIL with `assert "persist_organization_research" in names` (not registered yet)

- [ ] **Step 3: Implement**

In `app/mcp/server.py`, directly below the existing `merge_person_records` tool, add:

```python
@mcp.tool()
def persist_organization_research(
    org_id: str,
    industry: str | None = None,
    description: str | None = None,
    products_services: list[str] | None = None,
    size_estimate: str | None = None,
    headquarters: str | None = None,
    website: str | None = None,
) -> dict[str, Any]:
    """Persists external research you (Claude, via your own WebSearch tool)
    already performed about a company -- call this after seeing
    new_organizations_needing_research in a persist_email_analysis result, or
    after reviewing list_unresearched_organizations. Only overwrites a field
    if it wasn't already hand-corrected (research_source != "manual" on the
    existing record). Sets researched_at regardless of whether you found
    anything, so the same org isn't re-surfaced on every future email. Raises
    ValueError if org_id doesn't exist.
    """
    return tools.persist_organization_research(
        _get_db(), org_id, industry=industry, description=description,
        products_services=products_services, size_estimate=size_estimate,
        headquarters=headquarters, website=website,
    )


@mcp.tool()
def list_unresearched_organizations() -> list[dict[str, Any]]:
    """Read-only catch-up list of every active Organization that has never
    been researched (researched_at is None) -- for a company created but
    never mentioned again in a later email, independent of
    persist_email_analysis's per-email new_organizations_needing_research
    signal. Never writes anything.
    """
    return tools.list_unresearched_organizations(_get_db())


@mcp.tool()
def preview_duplicate_organization_candidates() -> list[dict[str, Any]]:
    """Read-only heuristic preview of organizations that may be the same real
    company under a different name or domain (e.g. "DataBeat" / "DataBeat
    Analytics" / databeat.com) -- resolve_organization's own automatic path
    only merges on exact email domain and never catches these. Never merges
    anything -- review each candidate yourself (WebSearch if genuinely
    ambiguous) and call merge_organization_records only when confident.
    """
    return tools.preview_duplicate_organization_candidates(_get_db())


@mcp.tool()
def merge_organization_records(source_org_id: str, target_org_id: str) -> dict[str, Any]:
    """Merges one Organization record into another -- use only when you
    (Claude, having reviewed a preview_duplicate_organization_candidates pair,
    with WebSearch if genuinely ambiguous) are confident both records are the
    same real company. Repoints org_id across every downstream collection
    (people, projects, opportunities, commitments, follow_ups, meetings,
    knowledge_items, reply_drafts, calendar_actions), unions aliases, and
    backfills blank profile fields on the target from the source. The source
    is never deleted -- only retired (status="merged", merged_into=
    target_org_id). Raises ValueError if either id doesn't exist, they're the
    same id, or source_org_id is already merged into someone else.
    """
    return tools.merge_organization_records(_get_db(), source_org_id, target_org_id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp/server.py tests/test_mcp_server.py
git commit -m "feat: register organization research and merge tools on the MCP server"
```

---

## Task 10: Update the `gmail-initial-ingest` skill

**Files:**
- Modify: `skills/gmail-initial-ingest/SKILL.md`

**Interfaces:**
- Consumes: `new_organizations_needing_research` (Task 3), `persist_organization_research` (Task 2), `list_unresearched_organizations` (Task 2), `preview_duplicate_organization_candidates`/`merge_organization_records` (Tasks 6, 8) -- all now registered (Task 9).

- [ ] **Step 1: Read the current skill file**

Run: `cat skills/gmail-initial-ingest/SKILL.md` (or open it in an editor) to find the shared per-message procedure's existing step that already reacts to `persist_email_analysis`'s `possible_missed_commitment` field -- the new step goes immediately after it, following the same "read this field from the tool result, act on it before mark_email_completed" pattern already established there for that field.

- [ ] **Step 2: Add the new-organization-research step**

Add a new step to the shared per-message procedure, directly after the existing `possible_missed_commitment` handling step:

```markdown
- **Research newly-discovered organizations.** If `persist_email_analysis`'s
  result includes a non-empty `new_organizations_needing_research` list, for
  each entry use WebSearch (1-2 targeted queries, e.g. `"<name>" company
  industry` or `"<domain>" about`) to find out what the company does, then
  call `persist_organization_research` with whatever you found (industry,
  description, products_services, size_estimate, headquarters, website). If
  search turns up nothing useful, still call `persist_organization_research`
  with no fields set -- this marks the company "looked, found nothing" so it
  isn't re-surfaced on every future email about it. Do this before calling
  `mark_email_completed` for the message.
```

Add a new, separate section (not part of the per-message procedure, since it's not run on every message) documenting the catch-up and dedup tools:

```markdown
## Organization research catch-up and dedup (on-demand only)

These are never run automatically as part of ingesting an email -- only when
explicitly asked to catch up on research or check for duplicate organizations,
mirroring how person-dedup (`preview_duplicate_person_candidates`/
`merge_person_records`) already works in this skill.

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
```

- [ ] **Step 3: Commit**

```bash
git add skills/gmail-initial-ingest/SKILL.md
git commit -m "docs: update gmail-initial-ingest skill for organization research and dedup tools"
```

---

## Final Verification

1. `python -m pytest -q` -- full suite green, no regressions.
2. `python -c "from app.mcp import server"` -- confirms the module still imports cleanly (catches any missed import in Tasks 2-9).
3. Manually sanity-check `get_company_summary`'s new contract change doesn't break the FastAPI route: `python -m pytest tests/ -k organizations_router -v` if such a test file exists, otherwise a quick manual check that `app/api/routers/organizations.py` still imports and type-checks cleanly.
