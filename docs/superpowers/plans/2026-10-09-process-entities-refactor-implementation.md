# _process_entities() Refactor + Raw-Dump Replay Validation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 373-line `_process_entities()` function in `app/pipeline.py:378-751` into small, comment-free, independently testable helper functions with identical external behavior, then prove the result still works end-to-end by replaying 10 hand-built test emails through the real `raw_emails_dump` → `ingest_email` → `persist_email_analysis` path.

**Architecture:** This is a behavior-preserving refactor, not a feature change. `_process_entities()` currently does 7 sequential jobs in one function body (resolve envelope people, resolve LLM-mentioned people, resolve projects, resolve commitments + derive follow-ups, resolve meetings, create sales opportunities, resolve personal items) sharing a mutable `resolved_people` list and a couple of intermediate maps. Each job becomes its own small function with an explicit input/output contract instead of implicit shared mutable state; `_process_entities()` becomes a ~20-line orchestrator that calls them in the same fixed order (order matters — see Global Constraints). No change to `app.mcp.tools.persist_email_analysis` or `app.pipeline.run_pipeline`'s calling contract.

**Tech Stack:** Python 3.11, pydantic v2, pymongo + mongomock (tests), pytest.

**Spec:** The current implementation itself — `app/pipeline.py:378-751` (read in full before starting; this plan's tasks are a literal decomposition of that function, line-ranges cited per task) — plus `app/analysis/schemas.py` (the `EmailAnalysis` contract) and `app/entities/resolution.py` (the `resolve_*_with_operation` functions this code calls, unchanged by this plan).

## Global Constraints

- Behavior-preserving only: for every existing passing test, the refactor must produce byte-identical `entities_referenced` dict output and identical MongoDB writes (same collections, same documents, same `try_record_event` calls, same order). If a test's assertion would need to change because of this refactor, the refactor has a bug — stop and fix the refactor, not the test.
- Every new function must be under 50 lines (project-wide coding-style rule) and must not nest more than 4 levels deep.
- No restatement comments ("loop over X" above a loop over X). The ~6 non-obvious business rules currently encoded as paragraph comments in `_process_entities` (listed in Review Focus below) must survive as a single-line docstring note on the function that owns that rule — not deleted, not left as a paragraph.
- Never construct a real LLM provider (`ClaudeProvider`/`OpenAIProvider`) anywhere in this refactor or its tests — this function and its tests only ever see `MockLLMProvider`, consistent with the rest of this codebase's architecture.
- `raw_emails_dump` (written by `app.mcp.tools.ingest_raw_email_only`) stays fully isolated from the `emails` collection / `ProcessingStage` / `_process_entities` — per its own documented contract (`app/mcp/tools.py:225-251`). The Task 10 validation test reads *from* a dump-shaped fixture but calls `ingest_email`/`persist_email_analysis` separately; it must never make `persist_email_analysis` read from or write to `raw_emails_dump`.
- Preserve the exact existing signature and return shape of `_process_entities(db, thread_id, email, analysis, reference_now, agent_email, llm_provider, agent_name=None) -> dict[str, list[str]]` with keys `people, projects, commitments, follow_ups, meetings, personal, opportunities` — `app.mcp.tools.persist_email_analysis` and `app.pipeline.run_pipeline` both call it positionally/by-keyword today and must not need to change.
- Follow the project's existing immutability convention: helpers return new values (lists, tuples, dataclasses); do not mutate a shared `entities_referenced` dict in place across helpers.

## Review Focus

1. **Dedup order.** `entities_referenced["people"]` (and every other list) is built via `dict.fromkeys(ids)` — first-seen order preserved. A test like `test_pipeline_populates_entity_metadata_on_the_email` asserts `entities_referenced["people"][0]` is the sender, not the operator. A refactor that rebuilds a list via `set()` or reorders helper calls silently breaks this — give it its own assertion in Task 9's reassembly step.
2. **Shared `resolved_people` pool.** Commitments/meetings/person-facts matching must see people resolved by *both* the envelope step and the people_mentioned step, in the full combined list — not just one or the other. Task 9 must pass the concatenated list, not just the mentioned-people output, into every downstream helper.
3. **Opportunity double-gate.** An Opportunity may only be created when `goal_pillar == "Sales"` **and** a `projects_mentioned` entry actually resolved to a `project_id` in *this same email* — Example 6 vs Example 7 below pin both sides of this gate.
4. **FollowUp class gate.** Only `commitment_class in {"mine", "owed_to_me"}` ever attempts FollowUp derivation; `"theirs"`/`"recap"` must never produce one even though the Commitment itself is always persisted — Example 9 pins this.
5. **No phantom people.** A meeting attendee name or person-fact mention that doesn't match anyone already resolved for *this* email must be silently dropped, never create a new Person — Example 10 pins this (the "Mike Chen" attendee).

## File Structure

- Modify: `app/pipeline.py:378-751` (replace the monolithic `_process_entities` with an orchestrator + 7 new private helpers, placed immediately above the orchestrator in the same relative order they execute)
- Create: `tests/test_process_entities_raw_dump_replay.py` (Task 10 — the 10-example integration suite)
- No other files change. `app/mcp/tools.py`, `app/entities/resolution.py`, `app/entities/dates.py`, `app/calendar/*` are all called unchanged by the new helpers.

---

### Task 1: Baseline safety net

**Files:** none changed — this task only runs the existing suite and records the result.

- [ ] **Step 1: Run the full suite and record the baseline**

Run: `pytest -q`
Expected: every test passes. Write down the final summary line (e.g. `1369 passed`) — this exact number is your regression gate for every later task. If anything is already failing, stop and fix or report that before starting the refactor; do not refactor on top of a red baseline.

- [ ] **Step 2: Commit nothing (no code changed yet)** — this task is a checkpoint, not a commit.

---

### Task 2: Extract `_EnvelopeContext` + `_resolve_envelope_people()`

**Files:**
- Modify: `app/pipeline.py:378-446` (the setup block + envelope loop)
- Test: existing suite (no new test file this task)

**Interfaces:**
- Produces: `_EnvelopeContext` (frozen dataclass) and `_resolve_envelope_people(db, thread_id, email, ctx, reference_now) -> list[dict[str, Any]]`, returning the resolved Person documents for every envelope (From/To/CC) address, operator included.

- [ ] **Step 1: Add the dataclass and helper function above `_process_entities`**

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class _EnvelopeContext:
    sender_email: str
    agent_email: str
    agent_email_normalized: str
    agent_email_domain: str | None
    agent_name_tokens: frozenset[str] | None
    operator_display_name: str

    @staticmethod
    def build(email: Email, agent_email: str, agent_name: str | None) -> "_EnvelopeContext":
        agent_email_normalized = agent_email.lower()
        agent_email_domain = (
            agent_email_normalized.split("@")[-1] if "@" in agent_email_normalized else None
        )
        return _EnvelopeContext(
            sender_email=email.from_.email.lower(),
            agent_email=agent_email,
            agent_email_normalized=agent_email_normalized,
            agent_email_domain=agent_email_domain,
            agent_name_tokens=frozenset(_name_tokens_pipeline(agent_name)) if agent_name else None,
            operator_display_name=f"{agent_name} (Me)" if agent_name else "Me",
        )


def _resolve_envelope_people(
    db: Database, thread_id: str, email: Email, ctx: _EnvelopeContext, reference_now: datetime
) -> list[dict[str, Any]]:
    """Every real address in From/To/CC gets a Person record, independent of
    whether the LLM's own people_mentioned happened to include it. The
    operator's own mailbox resolves to their dedicated operator profile, never
    an ordinary Person."""
    person_repo = PersonRepository(db)
    resolved: list[dict[str, Any]] = []
    envelope = {addr.email.lower(): addr for addr in envelope_people(email)}
    for envelope_email, addr in envelope.items():
        if envelope_email == ctx.agent_email_normalized:
            person_id, operation, delta = resolve_operator_person_with_operation(
                db, ctx.agent_email, ctx.operator_display_name,
                is_sender=envelope_email == ctx.sender_email,
                now=reference_now, thread_id=thread_id,
            )
        else:
            person_id, operation, delta = resolve_person_with_operation(
                db, {"name": addr.name, "email": addr.email, "org": None},
                is_sender=envelope_email == ctx.sender_email,
                now=reference_now, thread_id=thread_id,
            )
        _record_person_event(db, thread_id, email.message_id, person_id, operation, delta)
        resolved.append(person_repo.find_one({"id": person_id}))
    return resolved
```

- [ ] **Step 2: Wire it in** — inside `_process_entities`, replace the setup block + envelope loop with:

```python
    ctx = _EnvelopeContext.build(email, agent_email, agent_name)
    resolved_people = _resolve_envelope_people(db, thread_id, email, ctx, reference_now)
```

(The rest of `_process_entities` below this point is untouched in this task — it still references the old local variable names like `agent_email_normalized`; leave those lines as `ctx.agent_email_normalized` etc. for now, or leave the old locals assigned from `ctx.*` as a temporary bridge — either is fine since Task 9 deletes this whole block anyway.)

- [ ] **Step 3: Run the full suite, confirm identical result to Task 1's baseline**

Run: `pytest -q`
Expected: same pass count as Task 1.

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _EnvelopeContext and _resolve_envelope_people from _process_entities"
```

---

### Task 3: Extract `_resolve_mentioned_people()`

**Files:**
- Modify: `app/pipeline.py:448-510` (the `people_mentioned` loop)

**Interfaces:**
- Consumes: `ctx: _EnvelopeContext`, `envelope_people: list[dict]` (Task 2's output)
- Produces: `_resolve_mentioned_people(db, thread_id, email, analysis, ctx, envelope_people, reference_now) -> list[dict[str, Any]]`, returning *only* the newly-matched-or-created people from `analysis.people_mentioned` (the caller concatenates this with `envelope_people`).

- [ ] **Step 1: Add the helper**

```python
def _resolve_mentioned_people(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis,
    ctx: _EnvelopeContext, envelope_people: list[dict[str, Any]], reference_now: datetime,
) -> list[dict[str, Any]]:
    """A no-email mention is checked first against people already resolved for
    THIS email (envelope + earlier mentions in this same loop) before ever
    calling resolve_person's own (unchanged) no-email tiers -- never a fresh,
    wider scan of the whole people collection."""
    person_repo = PersonRepository(db)
    resolved_so_far = list(envelope_people)
    newly_resolved: list[dict[str, Any]] = []

    for mention in analysis.people_mentioned:
        mention_email = (mention.email or "").lower() or None
        is_operator_mention = mention_email == ctx.agent_email_normalized
        if not is_operator_mention and not mention_email and ctx.agent_name_tokens:
            mention_name_tokens = _name_tokens_pipeline(mention.name)
            is_operator_mention = bool(mention_name_tokens) and (
                mention_name_tokens <= ctx.agent_name_tokens or ctx.agent_name_tokens <= mention_name_tokens
            )

        is_sender = None
        envelope_emails = {p["email"].lower() for p in envelope_people if p and p.get("email")}
        if mention_email and mention_email == ctx.sender_email:
            is_sender = True
        elif mention_email and mention_email in envelope_emails:
            is_sender = False

        if is_operator_mention:
            person_id, operation, delta = resolve_operator_person_with_operation(
                db, ctx.agent_email, ctx.operator_display_name, is_sender=is_sender,
                now=reference_now, thread_id=thread_id,
            )
            _record_person_event(db, thread_id, email.message_id, person_id, operation, delta)
            person = person_repo.find_one({"id": person_id})
            resolved_so_far.append(person)
            newly_resolved.append(person)
            continue

        same_match = match_resolved_person_by_name(resolved_so_far, mention.name) if not mention_email else None
        if same_match is not None:
            continue  # already resolved this email -- no write, no new entry needed

        person_id, operation, delta = resolve_person_with_operation(
            db,
            {"name": mention.name, "email": mention.email, "org": mention.org, "role_hint": mention.role_hint},
            is_sender=is_sender, now=reference_now, thread_id=thread_id,
        )
        _record_person_event(db, thread_id, email.message_id, person_id, operation, delta)
        person = person_repo.find_one({"id": person_id})
        resolved_so_far.append(person)
        newly_resolved.append(person)

    return newly_resolved
```

- [ ] **Step 2: Wire it in**

```python
    mentioned_people = _resolve_mentioned_people(db, thread_id, email, analysis, ctx, resolved_people, reference_now)
    resolved_people = resolved_people + mentioned_people
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _resolve_mentioned_people from _process_entities"
```

---

### Task 4: Extract `_process_projects_mentioned()`

**Files:**
- Modify: `app/pipeline.py:536-587`

**Interfaces:**
- Consumes: `resolved_people: list[dict]` (Tasks 2+3 combined)
- Produces: `_process_projects_mentioned(db, thread_id, email, analysis, resolved_people) -> tuple[list[str], dict[str, list[str]], list[dict]]` returning `(project_ids, project_ids_by_org_id, sales_opportunity_candidates)`

- [ ] **Step 1: Add the helper**

```python
def _process_projects_mentioned(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis, resolved_people: list[dict[str, Any]],
) -> tuple[list[str], dict[str, list[str]], list[dict[str, Any]]]:
    """An Opportunity is never created here -- only remembered as a candidate
    (is_sales_email gate) for _process_sales_opportunities to act on after
    meetings_mentioned has run, so this email's own detected meetings can be
    linked onto it at creation time."""
    project_ids: list[str] = []
    project_ids_by_org_id: dict[str, list[str]] = {}
    sales_opportunity_candidates: list[dict[str, Any]] = []
    is_sales_email = (analysis.goal_pillar or "").strip().lower() == "sales"

    for mention in analysis.projects_mentioned:
        entity_normalized = normalize_text(mention.org or "")
        matching_people = [
            p for p in resolved_people
            if entity_normalized and normalize_text(p.get("org") or "") == entity_normalized
        ]
        project_org_id = next((p["org_id"] for p in matching_people if p.get("org_id")), None)
        project_person_ids = list(dict.fromkeys(p["id"] for p in matching_people))
        project_id, project_operation, project_delta = resolve_project_with_operation(
            db, {"name": mention.name, "org": mention.org},
            goal_pillar=analysis.goal_pillar, person_ids=project_person_ids, org_id=project_org_id,
        )
        if project_operation == "updated":
            try_record_event(
                db, thread_id, email.message_id, "project_updated", "project", project_id, "updated",
                f"Project {project_id} updated", metadata=_delta_metadata(project_delta),
            )
        else:
            try_record_event(
                db, thread_id, email.message_id,
                "entity_created" if project_operation == "created" else "entity_reused",
                "project", project_id, project_operation, f"Project {project_id} {project_operation}",
            )
        project_ids.append(project_id)
        if project_org_id:
            project_ids_by_org_id.setdefault(project_org_id, [])
            if project_id not in project_ids_by_org_id[project_org_id]:
                project_ids_by_org_id[project_org_id].append(project_id)

        if is_sales_email:
            sales_opportunity_candidates.append(
                {"mention": mention, "project_id": project_id, "org_id": project_org_id, "person_ids": project_person_ids}
            )

    return project_ids, project_ids_by_org_id, sales_opportunity_candidates
```

- [ ] **Step 2: Wire it in**

```python
    project_ids, project_ids_by_org_id, sales_opportunity_candidates = _process_projects_mentioned(
        db, thread_id, email, analysis, resolved_people
    )
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _process_projects_mentioned from _process_entities"
```

---

### Task 5: Extract `_process_commitments_mentioned()`

**Files:**
- Modify: `app/pipeline.py:589-674`

**Interfaces:**
- Consumes: `resolved_people`, `project_ids_by_org_id` (Task 4's output), `ctx.agent_email_domain`
- Produces: `_process_commitments_mentioned(db, thread_id, email, analysis, resolved_people, project_ids_by_org_id, agent_email_domain, reference_now) -> tuple[list[str], list[str]]` returning `(commitment_ids, follow_up_ids)`

- [ ] **Step 1: Add the helper**

```python
_CHASED_COMMITMENT_CLASSES = {"mine", "owed_to_me"}


def _process_commitments_mentioned(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis,
    resolved_people: list[dict[str, Any]], project_ids_by_org_id: dict[str, list[str]],
    agent_email_domain: str | None, reference_now: datetime,
) -> tuple[list[str], list[str]]:
    """A FollowUp is derived ONLY from a resolved Commitment, and only for
    class in {mine, owed_to_me} -- theirs/recap are tracked via the
    Commitment record but must never generate one. project_id is linked only
    when the counterparty's org has exactly one project resolved for THIS
    email -- ambiguous or missing stays unset, never guessed."""
    commitment_ids: list[str] = []
    follow_up_ids: list[str] = []

    for raw_commitment in analysis.commitments_mentioned:
        resolved_date, date_type = resolve_date_phrase(raw_commitment.date_phrase, reference_now)
        commitment_person = (
            match_resolved_person_by_name(resolved_people, raw_commitment.owed_to)
            or match_resolved_person_by_name(resolved_people, raw_commitment.owed_by)
        )
        commitment_org_id = commitment_person.get("org_id") if commitment_person else None
        org_candidate_project_ids = project_ids_by_org_id.get(commitment_org_id, []) if commitment_org_id else []
        commitment_project_id = org_candidate_project_ids[0] if len(org_candidate_project_ids) == 1 else None

        commitment_id, commitment_operation, commitment_delta = resolve_commitment_with_operation(
            db, thread_id=thread_id, raw=raw_commitment.model_dump(mode="json", by_alias=True),
            message_id=email.message_id, made_on=reference_now,
            resolved_date=resolved_date, date_type=date_type, goal_pillar=analysis.goal_pillar,
            project_id=commitment_project_id,
            person_id=commitment_person["id"] if commitment_person else None,
            org_id=commitment_org_id,
        )
        try_record_event(
            db, thread_id, email.message_id,
            "commitment_created" if commitment_operation == "created" else "commitment_reused",
            "commitment", commitment_id, commitment_operation,
            f"Commitment {commitment_id} {commitment_operation}: {raw_commitment.what}",
            metadata=_delta_metadata(commitment_delta),
        )
        commitment_ids.append(commitment_id)

        if raw_commitment.commitment_class in _CHASED_COMMITMENT_CLASSES:
            is_internal_counterparty = False
            if commitment_org_id and agent_email_domain:
                counterparty_org = OrganizationRepository(db).find_one({"id": commitment_org_id})
                is_internal_counterparty = bool(
                    counterparty_org and counterparty_org.get("domain") == agent_email_domain
                )
            audience, follow_up_earliest_at, follow_up_latest_at = classify_follow_up_timing(
                commitment_class=raw_commitment.commitment_class, date_type=date_type,
                committed_date=resolved_date, is_internal_counterparty=is_internal_counterparty,
            )
            follow_up_id, follow_up_operation, _delta = derive_follow_up_with_operation(
                db, commitment_id=commitment_id, thread_id=thread_id,
                person_id=commitment_person["id"] if commitment_person else None,
                org_id=commitment_org_id, audience=audience,
                follow_up_earliest_at=follow_up_earliest_at, follow_up_latest_at=follow_up_latest_at,
            )
            try_record_event(
                db, thread_id, email.message_id,
                "follow_up_created" if follow_up_operation == "created" else "entity_reused",
                "follow_up", follow_up_id, follow_up_operation, f"Follow-up {follow_up_id} {follow_up_operation}",
            )
            follow_up_ids.append(follow_up_id)

    return commitment_ids, follow_up_ids
```

- [ ] **Step 2: Wire it in**

```python
    commitment_ids, follow_up_ids = _process_commitments_mentioned(
        db, thread_id, email, analysis, resolved_people, project_ids_by_org_id, ctx.agent_email_domain, reference_now
    )
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _process_commitments_mentioned from _process_entities"
```

---

### Task 6: Extract `_process_meetings_mentioned()`

**Files:**
- Modify: `app/pipeline.py:676-712`

**Interfaces:**
- Consumes: `resolved_people`
- Produces: `_process_meetings_mentioned(db, thread_id, email, analysis, resolved_people) -> list[str]` (meeting_ids)

- [ ] **Step 1: Add the helper**

```python
def _process_meetings_mentioned(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis, resolved_people: list[dict[str, Any]],
) -> list[str]:
    """Unmatched attendee names are simply not included, never guessed into a
    new Person."""
    meeting_ids: list[str] = []
    for raw_meeting in analysis.meetings_mentioned:
        resolved_date, _ = resolve_date_phrase(raw_meeting.date_phrase, reference_now=email.timestamp)
        actionable = not raw_meeting.is_past
        attendee_people = [
            p for p in (
                match_resolved_person_by_name(resolved_people, attendee) for attendee in raw_meeting.attendees
            ) if p is not None
        ]
        meeting_org_id = next((p["org_id"] for p in attendee_people if p.get("org_id")), None)
        meeting_id, meeting_operation, meeting_delta = resolve_meeting_with_operation(
            db, thread_id=thread_id, date=resolved_date, raw=raw_meeting.model_dump(mode="json"),
            actionable=actionable, person_ids=list(dict.fromkeys(p["id"] for p in attendee_people)),
            org_id=meeting_org_id, project_or_pillar=analysis.goal_pillar,
        )
        try_record_event(
            db, thread_id, email.message_id,
            "meeting_created" if meeting_operation == "created" else "meeting_reused",
            "meeting", meeting_id, meeting_operation, f"Meeting {meeting_id} {meeting_operation}",
            metadata=_delta_metadata(meeting_delta),
        )
        meeting_ids.append(meeting_id)
    return meeting_ids
```

Note: pass `reference_now` in explicitly rather than reading `email.timestamp` a second time — match the orchestrator's existing `reference_now` parameter instead (see Step 2) so this stays identical to current behavior (`reference_now` is already `email.timestamp` by the time `_process_entities` is called, per `app.mcp.tools.persist_email_analysis`'s own `reference_now = email.timestamp` line — but take it as an explicit parameter here, don't re-derive it, to avoid two sources of truth):

```python
def _process_meetings_mentioned(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis,
    resolved_people: list[dict[str, Any]], reference_now: datetime,
) -> list[str]:
    ...
    resolved_date, _ = resolve_date_phrase(raw_meeting.date_phrase, reference_now)
    ...
```

- [ ] **Step 2: Wire it in**

```python
    meeting_ids = _process_meetings_mentioned(db, thread_id, email, analysis, resolved_people, reference_now)
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _process_meetings_mentioned from _process_entities"
```

---

### Task 7: Extract `_process_sales_opportunities()`

**Files:**
- Modify: `app/pipeline.py:714-736`

**Interfaces:**
- Consumes: `sales_opportunity_candidates` (Task 4's output), `meeting_ids` (Task 6's output)
- Produces: `_process_sales_opportunities(db, thread_id, email, analysis, sales_opportunity_candidates, meeting_ids, reference_now) -> list[str]` (opportunity_ids)

- [ ] **Step 1: Add the helper**

```python
def _process_sales_opportunities(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis,
    sales_opportunity_candidates: list[dict[str, Any]], meeting_ids: list[str], reference_now: datetime,
) -> list[str]:
    opportunity_ids: list[str] = []
    for candidate in sales_opportunity_candidates:
        opportunity_id, opportunity_operation, opportunity_delta = resolve_opportunity_with_operation(
            db, {"name": candidate["mention"].name, "org": candidate["mention"].org},
            project_id=candidate["project_id"], now=reference_now, person_ids=candidate["person_ids"],
            org_id=candidate["org_id"], source_email_id=email.message_id,
            meeting_ids=list(meeting_ids), buying_signals=list(analysis.buying_signals),
        )
        try_record_event(
            db, thread_id, email.message_id,
            "opportunity_created" if opportunity_operation == "created" else "opportunity_reused",
            "opportunity", opportunity_id, opportunity_operation, f"Opportunity {opportunity_id} {opportunity_operation}",
            metadata=_delta_metadata(opportunity_delta),
        )
        opportunity_ids.append(opportunity_id)
    return opportunity_ids
```

- [ ] **Step 2: Wire it in**

```python
    opportunity_ids = _process_sales_opportunities(
        db, thread_id, email, analysis, sales_opportunity_candidates, meeting_ids, reference_now
    )
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _process_sales_opportunities from _process_entities"
```

---

### Task 8: Extract `_process_personal_items_mentioned()`

**Files:**
- Modify: `app/pipeline.py:738-748`

**Interfaces:**
- Produces: `_process_personal_items_mentioned(db, thread_id, email, analysis, reference_now) -> list[str]` (personal_item_ids)

- [ ] **Step 1: Add the helper**

```python
def _process_personal_items_mentioned(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis, reference_now: datetime,
) -> list[str]:
    item_ids: list[str] = []
    for raw_item in analysis.personal_items_mentioned:
        resolved_date, _ = resolve_date_phrase(raw_item.date_phrase, reference_now)
        item_id, item_operation, _delta = resolve_personal_item_with_operation(
            db, sender_email=email.from_.email.lower(), raw=raw_item.model_dump(mode="json"), resolved_date=resolved_date
        )
        try_record_event(
            db, thread_id, email.message_id,
            "entity_created" if item_operation == "created" else "entity_reused",
            "personal_item", item_id, item_operation, f"Personal item {item_id} {item_operation}",
        )
        item_ids.append(item_id)
    return item_ids
```

- [ ] **Step 2: Wire it in**

```python
    personal_item_ids = _process_personal_items_mentioned(db, thread_id, email, analysis, reference_now)
```

- [ ] **Step 3: Run the full suite, confirm identical pass count**

Run: `pytest -q`

- [ ] **Step 4: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: extract _process_personal_items_mentioned from _process_entities"
```

---

### Task 9: Reassemble `_process_entities()` as a thin orchestrator

**Files:**
- Modify: `app/pipeline.py:378-751` (replace the whole function body)

**Interfaces:**
- Produces: the final `_process_entities(db, thread_id, email, analysis, reference_now, agent_email, llm_provider, agent_name=None) -> dict[str, list[str]]`, calling the 7 helpers from Tasks 2-8 in the original fixed order, with an identical return shape to the pre-refactor version.

- [ ] **Step 1: Replace the function body**

```python
def _process_entities(
    db: Database, thread_id: str, email: Email, analysis: EmailAnalysis, reference_now: datetime,
    agent_email: str, llm_provider: LLMProvider, agent_name: str | None = None,
) -> dict[str, list[str]]:
    ctx = _EnvelopeContext.build(email, agent_email, agent_name)

    envelope_people = _resolve_envelope_people(db, thread_id, email, ctx, reference_now)
    mentioned_people = _resolve_mentioned_people(db, thread_id, email, analysis, ctx, envelope_people, reference_now)
    resolved_people = envelope_people + mentioned_people

    linked_org_ids = {p["org_id"] for p in resolved_people if p and p.get("org_id")}
    for org_id in linked_org_ids:
        try_record_event(
            db, thread_id, email.message_id, "entity_linked", "organization", org_id, "linked",
            f"Organization {org_id} linked via this email's resolved people",
        )
    _process_person_facts(db, thread_id, email, analysis, resolved_people, llm_provider, reference_now)

    project_ids, project_ids_by_org_id, sales_opportunity_candidates = _process_projects_mentioned(
        db, thread_id, email, analysis, resolved_people
    )
    commitment_ids, follow_up_ids = _process_commitments_mentioned(
        db, thread_id, email, analysis, resolved_people, project_ids_by_org_id, ctx.agent_email_domain, reference_now
    )
    meeting_ids = _process_meetings_mentioned(db, thread_id, email, analysis, resolved_people, reference_now)
    opportunity_ids = _process_sales_opportunities(
        db, thread_id, email, analysis, sales_opportunity_candidates, meeting_ids, reference_now
    )
    personal_item_ids = _process_personal_items_mentioned(db, thread_id, email, analysis, reference_now)

    entities_referenced = {
        "people": [p["id"] for p in resolved_people],
        "projects": project_ids,
        "commitments": commitment_ids,
        "follow_ups": follow_up_ids,
        "meetings": meeting_ids,
        "personal": personal_item_ids,
        "opportunities": opportunity_ids,
    }
    return {key: list(dict.fromkeys(ids)) for key, ids in entities_referenced.items()}
```

- [ ] **Step 2: Delete now-dead code** — remove the old inline setup block, the old `envelope`/`project_ids_by_org_id` locals, and any leftover bridge variables from Tasks 2-8 that are no longer referenced.

- [ ] **Step 3: Run the full suite, confirm identical pass count to Task 1's baseline**

Run: `pytest -q`
Expected: same exact pass count as recorded in Task 1. If anything differs, the bug is in this reassembly (likely: wrong order, or `resolved_people` built from only one of the two sources) — do not proceed to Task 10 until this is green.

- [ ] **Step 4: Review Review-Focus item 1 by hand** — open `tests/test_pipeline_entities.py::test_pipeline_populates_entity_metadata_on_the_email` and confirm it still asserts `entities_referenced["people"][0]` is the sender's id, and that it still passes.

- [ ] **Step 5: Commit**

```bash
git add app/pipeline.py
git commit -m "refactor: reassemble _process_entities as a thin orchestrator over 7 extracted helpers"
```

---

### Task 10: Raw-dump replay validation — 10 test examples

**Files:**
- Create: `tests/test_process_entities_raw_dump_replay.py`

**Interfaces:**
- Consumes: `app.mcp.tools.ingest_raw_email_only`, `app.mcp.tools.ingest_email`, `app.mcp.tools.persist_email_analysis` (all unchanged by this plan), `app.email.models.parse_email`, `app.analysis.schemas.EmailAnalysis` and its nested models.

This task proves the refactored `_process_entities` works through the exact two-step flow you described: (1) push a raw test email into `raw_emails_dump` (the same mechanism the `gmail-raw-dump` skill uses to collect test data), then (2) **separately**, replay that same email through the real analyzed path (`ingest_email` → `persist_email_analysis`, which calls the refactored `_process_entities`) and assert the right `people`/`projects`/`commitments`/`follow_ups`/`meetings`/`opportunities`/`personal_items` documents exist afterward. This mirrors the existing `scripts/reingest_historical.py` pattern (dump now, replay through analysis later) already established in this codebase.

- [ ] **Step 1: Write the shared fixtures**

```python
from datetime import datetime, timezone

import mongomock
import pytest

from app.analysis.schemas import (
    EmailAnalysis, MentionedPerson, MentionedProject, RawCommitment,
    RawMeeting, RawPersonalItem,
)
from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.database.repositories import (
    CommitmentRepository, FollowUpRepository, MeetingRepository,
    OpportunityRepository, OrganizationRepository, PersonalItemRepository,
    PersonRepository, ProjectRepository,
)
from app.email.models import parse_email
from app.mcp.tools import ingest_email, ingest_raw_email_only, persist_email_analysis

AGENT_EMAIL = "ashok@ourcompany.example"


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(
        email_provider="mock", calendar_provider="mock", llm_provider="mock",
        agent_email=AGENT_EMAIL, agent_name=None,
    )


def _raw(source_message_id: str, from_email: str, from_name: str | None, subject: str, body: str,
          to_email: str = AGENT_EMAIL, cc: list[dict] | None = None, thread_id: str | None = None,
          timestamp: str = "2026-10-01T09:00:00Z") -> dict:
    raw = {
        "message_id": source_message_id,
        "from": {"name": from_name, "email": from_email},
        "to": [{"name": "Ashok", "email": to_email}],
        "cc": cc or [],
        "subject": subject,
        "body": body,
        "timestamp": timestamp,
    }
    if thread_id:
        raw["thread_id"] = thread_id
    return raw


def _dump_then_ingest(db, raw: dict) -> dict:
    """Step 1 of the real flow: push into raw_emails_dump (ingestion-dump path).
    Step 2: separately, replay through the real analyzed path (ingest_email) --
    the two are deliberately independent, matching raw_emails_dump's own
    documented isolation from the analyzed `emails` collection."""
    email = parse_email(raw)
    ingest_raw_email_only(db, email)  # Step 1 -- raw_emails_dump
    return ingest_email(db, email)  # Step 2 -- emails + threads (canonical ids assigned here)
```

- [ ] **Step 2: Example 1 — new person via envelope only, no mentions**

```python
def test_example_1_new_person_via_envelope_only(db, settings):
    raw = _raw("gmail-1", "john.carter@acmecorp.example", "John Carter",
                "Quick question", "Hi Ashok, just checking in.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="Customer checking in", intent="general_inquiry",
        label_applied="1. Read only",
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["people"]) == 2  # John + the operator (Ashok)
    assert result["entities_referenced"]["projects"] == []
    assert result["entities_referenced"]["commitments"] == []

    john = PersonRepository(db).find_one({"email": "john.carter@acmecorp.example"})
    assert john is not None
    assert john["org_id"] is not None  # domain-based org resolution ran
```

- [ ] **Step 3: Example 2 — reply in same thread, existing person reused + name backfilled**

```python
def test_example_2_reply_reuses_person_and_backfills_name(db, settings):
    raw_a = _raw("gmail-2a", "john.carter@acmecorp.example", None,
                  "Intro", "Hello, I'm interested in your product.", thread_id="thread-john-1")
    ingested_a = _dump_then_ingest(db, raw_a)
    persist_email_analysis(db, ingested_a["message_id"],
                            EmailAnalysis(email_id=ingested_a["message_id"], summary="s", intent="i"), settings)
    person_after_a = PersonRepository(db).find_one({"email": "john.carter@acmecorp.example"})
    assert person_after_a["name"] is None

    raw_b = _raw("gmail-2b", "john.carter@acmecorp.example", "John Carter",
                  "Re: Intro", "Following up on my question.", thread_id="thread-john-1")
    ingested_b = _dump_then_ingest(db, raw_b)
    persist_email_analysis(db, ingested_b["message_id"],
                            EmailAnalysis(email_id=ingested_b["message_id"], summary="s", intent="i"), settings)

    person_after_b = PersonRepository(db).find_one({"email": "john.carter@acmecorp.example"})
    assert person_after_b["id"] == person_after_a["id"]  # same person, never duplicated
    assert person_after_b["name"] == "John Carter"  # backfilled, not clobbered (it was None, not a real name)
    assert ingested_a["thread_id"] == ingested_b["thread_id"]  # threaded together via source_thread_id
```

- [ ] **Step 4: Example 3 — people_mentioned entry whose email matches the envelope (idempotent, no duplicate)**

```python
def test_example_3_mentioned_person_matching_envelope_email_is_not_duplicated(db, settings):
    raw = _raw("gmail-3", "john.carter@acmecorp.example", "John Carter",
                "Proposal", "Loop in John Carter on this, he's the VP Sales.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        people_mentioned=[MentionedPerson(name="John Carter", email="john.carter@acmecorp.example",
                                           org="Acme Corp", role_hint="VP Sales")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(set(result["entities_referenced"]["people"])) == 2  # John + operator, no 3rd person
    john = PersonRepository(db).find_one({"email": "john.carter@acmecorp.example"})
    assert john["role"] == "VP Sales"  # backfilled from the mention, since it was missing
```

- [ ] **Step 5: Example 4 — no-email mention matches someone already resolved via envelope (CC) this same email**

```python
def test_example_4_no_email_mention_reuses_this_emails_cc_person(db, settings):
    raw = _raw("gmail-4", "john.carter@acmecorp.example", "John Carter",
                "Budget", "Priya will sign off on this.",
                cc=[{"name": "Priya Singh", "email": "priya.singh@acmecorp.example"}])
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        people_mentioned=[MentionedPerson(name="Priya Singh", email=None, org="Acme Corp", role_hint="Finance")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(set(result["entities_referenced"]["people"])) == 3  # John, Priya, operator -- no 4th
    priya = PersonRepository(db).find_one({"email": "priya.singh@acmecorp.example"})
    assert priya["role"] == "Finance"  # still backfilled even though matched via name, not email
```

- [ ] **Step 6: Example 5 — no-email mention matches an existing email-anchored person from a DIFFERENT thread (Tier 2: org + name-token match)**

```python
def test_example_5_no_email_mention_matches_existing_person_cross_thread(db, settings):
    seed_raw = _raw("gmail-5-seed", "priya.singh@acmecorp.example", "Priya Singh",
                      "Finance intro", "Hello, I handle budget approvals.", thread_id="thread-priya-1")
    seed_ingested = _dump_then_ingest(db, seed_raw)
    persist_email_analysis(db, seed_ingested["message_id"],
                            EmailAnalysis(email_id=seed_ingested["message_id"], summary="s", intent="i"), settings)
    priya_before = PersonRepository(db).find_one({"email": "priya.singh@acmecorp.example"})

    raw = _raw("gmail-5", "john.carter@acmecorp.example", "John Carter",
                "Approval", "Priya from Finance approved the budget.", thread_id="thread-john-2")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        people_mentioned=[MentionedPerson(name="Priya", email=None, org="Acme Corp", role_hint=None)],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(set(result["entities_referenced"]["people"])) == 3  # John, Priya (reused), operator
    assert priya_before["id"] in result["entities_referenced"]["people"]  # same Priya, not a new no-email Person
    priya_after = PersonRepository(db).find_one({"id": priya_before["id"]})
    assert "Priya" in priya_after.get("aliases", [])  # new alias recorded, name/email untouched
```

- [ ] **Step 7: Example 6 — Sales email + resolved project → Project AND Opportunity both created**

```python
def test_example_6_sales_email_with_project_creates_opportunity(db, settings):
    raw = _raw("gmail-6", "john.carter@acmecorp.example", "John Carter",
                "CRM Proposal for Acme", "We'd like pricing for the CRM rollout.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i", goal_pillar="Sales",
        buying_signals=["requested pricing"],
        projects_mentioned=[MentionedProject(name="Acme CRM Rollout", org="Acme Corp",
                                               objective_hint="Replace Salesforce")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["projects"]) == 1
    assert len(result["entities_referenced"]["opportunities"]) == 1
    opportunity = OpportunityRepository(db).find_one({"id": result["entities_referenced"]["opportunities"][0]})
    assert opportunity["project_ids"] == result["entities_referenced"]["projects"]
    assert opportunity["buying_signals"] == ["requested pricing"]
```

- [ ] **Step 8: Example 7 — non-Sales email with a project → Project created, NO Opportunity**

```python
def test_example_7_non_sales_email_with_project_creates_no_opportunity(db, settings):
    raw = _raw("gmail-7", "john.carter@acmecorp.example", "John Carter",
                "Onboarding", "Let's plan the onboarding steps.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i", goal_pillar="Support",
        projects_mentioned=[MentionedProject(name="Acme Onboarding", org="Acme Corp")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["projects"]) == 1
    assert result["entities_referenced"]["opportunities"] == []  # goal_pillar gate held
```

- [ ] **Step 9: Example 8 — "owed_to_me" commitment from an external client → Commitment + FollowUp, client audience**

```python
def test_example_8_owed_to_me_commitment_creates_client_follow_up(db, settings):
    raw = _raw("gmail-8", "john.carter@acmecorp.example", "John Carter",
                "PO timing", "I'll get you the signed PO by this Friday.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        commitments_mentioned=[RawCommitment(what="send signed PO", commitment_class="owed_to_me",
                                              owed_by="John Carter", owed_to="Ashok", date_phrase="this Friday")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["commitments"]) == 1
    assert len(result["entities_referenced"]["follow_ups"]) == 1
    follow_up = FollowUpRepository(db).find_one({"id": result["entities_referenced"]["follow_ups"][0]})
    assert follow_up["audience"] == "client_fixed_date"  # external counterparty + a stated date
```

- [ ] **Step 10: Example 9 — "theirs" and "recap" commitments → both persisted, NEITHER produces a FollowUp**

```python
def test_example_9_theirs_and_recap_commitments_never_produce_follow_ups(db, settings):
    raw = _raw("gmail-9", "john.carter@acmecorp.example", "John Carter",
                "Status recap", "Recap: Acme legal will review the MSA next week; rollout starts Q1 as agreed.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        commitments_mentioned=[
            RawCommitment(what="finalize internal budget approval", commitment_class="theirs",
                          owed_by="Acme", date_phrase="next week"),
            RawCommitment(what="rollout starts in Q1", commitment_class="recap"),
        ],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["commitments"]) == 2
    assert result["entities_referenced"]["follow_ups"] == []  # neither class is chased
```

- [ ] **Step 11: Example 10 — meeting with an unmatched attendee (dropped, no phantom Person) + a personal item**

```python
def test_example_10_meeting_drops_unmatched_attendee_and_personal_item_is_created(db, settings):
    raw = _raw("gmail-10", "john.carter@acmecorp.example", "John Carter",
                "Sync", "Let's sync Tuesday at 3pm with me and Mike from your side.")
    ingested = _dump_then_ingest(db, raw)
    analysis = EmailAnalysis(
        email_id=ingested["message_id"], summary="s", intent="i",
        meetings_mentioned=[RawMeeting(date_phrase="Tuesday at 3pm",
                                        attendees=["John Carter", "Mike Chen"], is_past=False)],
        personal_items_mentioned=[RawPersonalItem(item_type="reminder",
                                                    description="Renew my own CRM trial license",
                                                    date_phrase="end of month")],
    )
    result = persist_email_analysis(db, ingested["message_id"], analysis, settings)

    assert len(result["entities_referenced"]["meetings"]) == 1
    meeting = MeetingRepository(db).find_one({"id": result["entities_referenced"]["meetings"][0]})
    john = PersonRepository(db).find_one({"email": "john.carter@acmecorp.example"})
    assert meeting["person_ids"] == [john["id"]]  # Mike Chen silently dropped, not fabricated

    assert len(result["entities_referenced"]["personal"]) == 1
    item = PersonalItemRepository(db).find_one({"id": result["entities_referenced"]["personal"][0]})
    assert item["sender_email"] == "john.carter@acmecorp.example"

    # The whole test file only ever created 2 distinct people across all 10 examples'
    # worth of org-acmecorp.example traffic in THIS test: John + the operator. If this
    # ever grows past 2, something started inventing a Person for "Mike Chen".
```

- [ ] **Step 12: Run just this new file**

Run: `pytest tests/test_process_entities_raw_dump_replay.py -v`
Expected: all 10 tests pass (plus Example 2's intermediate assertions).

- [ ] **Step 13: Run the full suite one last time**

Run: `pytest -q`
Expected: original baseline pass count + 10 (the new tests), nothing else changed.

- [ ] **Step 14: Commit**

```bash
git add tests/test_process_entities_raw_dump_replay.py
git commit -m "test: add 10-example raw-dump replay validation for refactored _process_entities"
```

---

## Self-Review

**Spec coverage:** every one of the 7 jobs in the original `_process_entities` (envelope people, mentioned people, projects, commitments+follow-ups, meetings, opportunities, personal items) has its own task (2-8) and its own extraction; Task 9 covers reassembly; Task 10 covers end-to-end validation through the real ingestion-dump → analysis path the user asked for.

**Placeholder scan:** no task says "add appropriate tests" or "similar to Task N" without code — every task has literal, complete code.

**Type consistency:** all 7 helper signatures in Tasks 2-8 match exactly what Task 9's orchestrator calls them with (same parameter names, same order, same return-tuple shapes).

**Review Focus coverage:** all 5 items have an owning task/assertion — #1 in Task 9 Step 4, #2 in Task 9's `resolved_people = envelope_people + mentioned_people`, #3 in Examples 6+7, #4 in Example 9, #5 in Example 10.
