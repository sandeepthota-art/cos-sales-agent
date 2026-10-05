# Incremental Gmail Ingestion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a Claude session a deterministic way to know where the last Gmail ingestion run left off, so "what's new" becomes a precise date-range search instead of a heuristic guess.

**Architecture:** One new read-only MCP tool (`get_last_ingested_email`) plus a skill-prose update to Mode C of `gmail-initial-ingest`. No new persistence, no new classify/draft logic — the existing granular tool chain (`ingest_email → persist_email_analysis → ... → mark_email_completed`) and its already-tested duplicate-detection/retry-safety behavior are reused completely unchanged.

**Tech Stack:** Python 3.11, pymongo, mongomock (tests), FastMCP.

**Spec:** `docs/superpowers/specs/2026-10-05-incremental-gmail-ingestion-design.md`

## Global Constraints

- No server-side LLM or search API call anywhere — confirmed via explicit user decision. `get_last_ingested_email` is pure deterministic MongoDB reads.
- No changes to `ingest_email`, `persist_email_analysis`, `create_reply_draft`, `mark_email_completed`, or any of their existing tests — their duplicate-detection and retry-safety behavior is already correct and already tested.
- All tests use `mongomock` only, run in the local/testing environment — never against the real production MongoDB (`cos_sales_production_v1`), per explicit user instruction.
- `get_last_ingested_email` considers every ingested email regardless of `processing_status.stage` (including a stuck/incomplete one) — it answers "where does our data already reach to," not "what's fully processed."

## Review Focus

- **Empty `emails` collection**: a session's very first incremental sync must get a clean `None`, not an exception or a crash on `max()` over an empty sequence — pinned by `test_get_last_ingested_email_returns_none_when_no_emails_exist`.
- **Multiple emails out of insertion order**: the tool must pick the true maximum `timestamp`, not just the most recently-inserted document — pinned by `test_get_last_ingested_email_returns_the_email_with_the_latest_timestamp` (inserts the latest-timestamped email first, earliest last, to prove it's not accidentally sorting by insertion/natural order).
- **A document missing `source_message_id`/`thread_id`**: must not crash — matches `list_processed_emails`' own established tolerance for partially-written documents — pinned by `test_get_last_ingested_email_tolerates_a_document_missing_optional_fields`.
- **A document with a missing/empty `timestamp`**: must not crash the `max()` comparison (mirrors `list_processed_emails`' own `e.get("timestamp") or ""` guard) — pinned by `test_get_last_ingested_email_tolerates_a_document_with_no_timestamp`.
- **The tool must never write anything** — it's purely read-only, called at the start of every incremental sync, so a bug that mutates data here would corrupt the boundary it's supposed to report — pinned by `test_get_last_ingested_email_never_writes_to_mongodb`.

---

## Task 1: `get_last_ingested_email` tool

**Files:**
- Modify: `app/mcp/tools.py` (add the new function directly above `list_processed_emails`, which starts at line 599)
- Test: `tests/test_mcp_query_tools.py`

**Interfaces:**
- Consumes: `EmailRepository` (already imported in `app/mcp/tools.py`).
- Produces: `get_last_ingested_email(db: Database) -> dict[str, Any] | None`, returning `{"message_id", "source_message_id", "thread_id", "timestamp"}` for the email with the maximum `timestamp`, or `None` if the `emails` collection is empty. Task 2 registers this exact function as an MCP tool.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_query_tools.py` (this file already imports `EmailRepository` and has the `_email(message_id, thread_id=None, **overrides)` fixture helper and the `db` fixture):

```python
# --- get_last_ingested_email ---------------------------------------------------------


def test_get_last_ingested_email_returns_none_when_no_emails_exist(db):
    assert tools.get_last_ingested_email(db) is None


def test_get_last_ingested_email_returns_the_email_with_the_latest_timestamp(db):
    # Inserted latest-timestamp first, earliest last -- proves this picks the true
    # max timestamp, not just the most-recently-inserted document.
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-003"},
        _email("EML-003", thread_id="THR-003", timestamp="2026-10-05T09:00:00Z", source_message_id="msg_003"),
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        _email("EML-001", thread_id="THR-001", timestamp="2026-10-01T09:00:00Z", source_message_id="msg_001"),
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-002"},
        _email("EML-002", thread_id="THR-002", timestamp="2026-10-03T09:00:00Z", source_message_id="msg_002"),
    )

    result = tools.get_last_ingested_email(db)

    assert result["message_id"] == "EML-003"
    assert result["source_message_id"] == "msg_003"
    assert result["thread_id"] == "THR-003"
    assert result["timestamp"] == "2026-10-05T09:00:00Z"


def test_get_last_ingested_email_tolerates_a_document_missing_optional_fields(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"},
        {"message_id": "EML-001", "timestamp": "2026-10-01T09:00:00Z"},
    )

    result = tools.get_last_ingested_email(db)

    assert result["message_id"] == "EML-001"
    assert result["source_message_id"] is None
    assert result["thread_id"] is None


def test_get_last_ingested_email_tolerates_a_document_with_no_timestamp(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"}, _email("EML-001", timestamp=None),
    )
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-002"}, _email("EML-002", timestamp="2026-10-01T09:00:00Z"),
    )

    result = tools.get_last_ingested_email(db)

    assert result["message_id"] == "EML-002"


def test_get_last_ingested_email_never_writes_to_mongodb(db):
    EmailRepository(db).upsert_by_key(
        {"message_id": "EML-001"}, _email("EML-001", timestamp="2026-10-01T09:00:00Z"),
    )
    before = {name: list(db[name].find({})) for name in db.list_collection_names()}

    tools.get_last_ingested_email(db)

    after = {name: list(db[name].find({})) for name in db.list_collection_names()}
    assert before == after
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_mcp_query_tools.py -k get_last_ingested_email -v`
Expected: FAIL with `AttributeError: module 'app.mcp.tools' has no attribute 'get_last_ingested_email'`

- [ ] **Step 3: Implement**

Add to `app/mcp/tools.py`, directly above `def list_processed_emails(db: Database, limit: int = 50) -> list[dict[str, Any]]:` (line 599):

```python
def get_last_ingested_email(db: Database) -> dict[str, Any] | None:
    """Read-only: the most recently ingested email, by its own timestamp
    (Email.timestamp -- the email's send/receive date; this project has
    never stored a separate ingestion-time field, and the email's own date
    is what a Gmail date-range search needs anyway).

    Returns None if nothing has been ingested yet. Considers every ingested
    email regardless of processing_status.stage (including one stuck
    mid-pipeline) -- the purpose is purely "where does our data already
    reach to," not "what's fully processed." A stuck email at the boundary
    is naturally re-surfaced by the next incremental search (Gmail's own
    date-range search is day-granularity, not second-precision) and safely
    re-ingested without duplication via ingest_email's existing dedup.

    Returns {"message_id", "source_message_id", "thread_id", "timestamp"}
    for the single email with the maximum timestamp, mirroring
    list_processed_emails' own sort-by-timestamp convention exactly (same
    `e.get("timestamp") or ""` key, same reason: a malformed document must
    never crash this lookup).
    """
    emails = EmailRepository(db).find_many({})
    if not emails:
        return None
    latest = max(emails, key=lambda e: e.get("timestamp") or "")
    return {
        "message_id": latest["message_id"],
        "source_message_id": latest.get("source_message_id"),
        "thread_id": latest.get("thread_id"),
        "timestamp": latest.get("timestamp"),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_mcp_query_tools.py -k get_last_ingested_email -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add app/mcp/tools.py tests/test_mcp_query_tools.py
git commit -m "feat: add get_last_ingested_email tool for incremental Gmail sync"
```

---

## Task 2: MCP server registration

**Files:**
- Modify: `app/mcp/server.py` (add one `@mcp.tool()` wrapper, directly above `list_processed_emails`'s own registration at line 146)
- Test: `tests/test_mcp_server.py`

**Interfaces:**
- Consumes: `tools.get_last_ingested_email` (Task 1).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_mcp_server.py`:

```python
def test_get_last_ingested_email_tool_is_registered():
    registered_tools = asyncio.run(mcp.list_tools())
    names = [tool.name for tool in registered_tools]
    assert "get_last_ingested_email" in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_mcp_server.py -k get_last_ingested_email -v`
Expected: FAIL with `assert "get_last_ingested_email" in names`

- [ ] **Step 3: Implement**

Add to `app/mcp/server.py`, directly above the existing `@mcp.tool()` / `def list_processed_emails(limit: int = 50) -> list[dict[str, Any]]:` (line 145-146):

```python
@mcp.tool()
def get_last_ingested_email() -> dict[str, Any] | None:
    """Read-only: the most recently ingested email (by its own timestamp),
    for determining where an incremental Gmail sync should resume from.
    Call this first in Mode C ("what's new check") -- if it returns a
    result, search Gmail from that email's date forward; if it returns
    None, nothing has been ingested yet, search from the project's
    agreed start date instead. Never writes anything.
    """
    return tools.get_last_ingested_email(_get_db())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_mcp_server.py -v`
Expected: PASS (full file)

- [ ] **Step 5: Commit**

```bash
git add app/mcp/server.py tests/test_mcp_server.py
git commit -m "feat: register get_last_ingested_email on the MCP server"
```

---

## Task 3: Mode C skill update

**Files:**
- Modify: `skills/gmail-initial-ingest/SKILL.md` (replace Mode C's body, currently lines 233-245)

**Interfaces:**
- Consumes: `get_last_ingested_email` (Tasks 1-2) -- now registered.

- [ ] **Step 1: Replace Mode C's body**

In `skills/gmail-initial-ingest/SKILL.md`, replace the entire current Mode C section:

```markdown
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
```

with:

```markdown
## Mode C: What's new check

1. Call `get_last_ingested_email` first.
   - If it returns a result, search Gmail `after:<that email's timestamp,
     formatted YYYY/MM/DD> before:<today's date, YYYY/MM/DD>`.
   - If it returns `None` (nothing ingested yet), search Gmail
     `after:2026/10/01 before:<today's date, YYYY/MM/DD>`.
2. If the search surfaces a thread, open it (`get_thread`) and enumerate
   its actual `messages[]` -- never treat a thread's own `id` field as a
   message id.
3. Run the per-message procedure on every message the search returns,
   unchanged. A message on the same day as the last-ingested one may be
   re-surfaced by the day-granularity search -- this is expected, not an
   error: `ingest_email`'s own `already_completed`/dedup check makes
   re-including it safe, and an email that was previously stuck mid-
   pipeline (never reached `COMPLETED`) gets a genuine retry this way.

Report in plain English per email: what came in, who it's from, whether it
was Sales, what was created, the Gmail label applied, whether a reply was
drafted/withheld (and why).
```

- [ ] **Step 2: Commit**

```bash
git add skills/gmail-initial-ingest/SKILL.md
git commit -m "docs: make Mode C's what's-new check boundary-driven, not heuristic"
```

---

## Final Verification

1. `python -m pytest -q --ignore=tests/test_api.py` from the repo root -- full suite green, no regressions (confirms no existing test for `ingest_email`/`persist_email_analysis`/`mark_email_completed` duplicate-detection or retry-safety broke, since none of those files were touched).
2. `python -c "from app.mcp import server"` -- confirms the module still imports cleanly.
3. Manual sanity check (optional, not required for this plan): with a local/mongomock-backed test db, insert one email, call `get_last_ingested_email` directly and confirm it returns that email's fields -- this is already covered by Task 1's own tests, listed here only as a reminder that this entire plan's tests run against mongomock, never the real production MongoDB.
