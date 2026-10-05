# CoS Sales Agent — Incremental Gmail Ingestion

Status: draft design, approved in conversation, pending written review.

## 1. Purpose

Today, "check what's new in Gmail" (Mode C of the `gmail-initial-ingest` skill)
searches Gmail's "recent inbox" heuristically and cross-checks candidates
against `search_emails`/`list_processed_emails` to guess what's actually new.
There is no systematic date boundary — a session has no deterministic way to
know exactly where the last ingestion run left off.

This spec adds exactly one new capability — a tool that reports the most
recent email already ingested — and uses it to make Mode C's "what's new"
search a precise, incremental date-range query instead of a heuristic guess.

**Explicitly confirmed scope** (per the resolved architectural question):
classification and drafting remain entirely Claude-driven, exactly as today.
No server-side LLM call is introduced anywhere. This spec reuses the existing
granular tool chain (`ingest_email → persist_email_analysis →
persist_context_delta → create_reply_draft/set_reply_withheld_reason →
mark_email_completed`) unchanged — it only changes *how a session decides
which Gmail messages to look at next*, nothing about what happens once it
finds one.

## 2. Current State (confirmed by direct code read)

- `Email.timestamp` (`app/email/models.py`) is the email's own send/receive
  datetime — already the field every existing sort/display uses
  (`list_processed_emails` sorts by it; it is what a Gmail date-range search
  query would be built from).
- `ingest_email` (`app/mcp/tools.py`) already provides full duplicate
  detection (keyed on `message_id`, with `source_message_id` preserving the
  real Gmail id) and retry-safety: it returns `already_completed: true` only
  when `processing_status.stage == ProcessingStage.COMPLETED`
  (`app/processing/models.py`), never on any earlier/failed stage.
- `mark_email_completed` already refuses to set `COMPLETED` unless
  `entities_referenced` is already populated (i.e. `persist_email_analysis`
  genuinely ran) — so a failed/incomplete email can never be marked processed
  by accident, and is always eligible for retry on the next run.
- **Items 2 and 5 of the original request (duplicate prevention; mark-processed
  only on success; failure leaves it retryable; already-processed skipped
  next time) are therefore already fully implemented today** — confirmed by
  existing tests (`test_ingest_email_skips_an_already_completed_email_without_reprocessing`,
  `test_mark_email_completed_raises_for_unknown_message_id`, and others in
  `tests/test_mcp_deterministic_tools.py`).
- Mode C (`skills/gmail-initial-ingest/SKILL.md`) is the only place this
  spec changes behavior — it currently has no boundary-detection step at all.

## 3. What's Missing

1. No tool reports "what's the most recent email we've already ingested" —
   nothing to anchor an incremental date-range Gmail search on.
2. Mode C's own instructions don't describe a systematic incremental
   boundary — just a heuristic "recent inbox" search.

## 4. New Tool: `get_last_ingested_email`

```python
# app/mcp/tools.py
def get_last_ingested_email(db: Database) -> dict[str, Any] | None:
    """Read-only: the most recently ingested email, by its own timestamp
    (Email.timestamp -- the email's send/receive date, not a separate
    ingestion-time field; this project has never stored one, and the
    email's own date is what a Gmail date-range search needs anyway).

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
        "timestamp": latest["timestamp"],
    }
```

Registered as an MCP tool (`app/mcp/server.py`) the same way every other
read-only tool in this file is — a thin wrapper calling
`tools.get_last_ingested_email(_get_db())`.

## 5. Skill Change: Mode C becomes boundary-driven

Replace Mode C's current "search Gmail for recent inbox, heuristically
cross-check" instruction with:

```text
1. Call get_last_ingested_email.
2. If it returns a result, search Gmail "after:<that email's date,
   YYYY/MM/DD> before:<today's date>".
   If it returns None (nothing ingested yet), search Gmail
   "after:2026/10/01 before:<today's date>".
3. Run the existing per-message procedure (ingest_email -> ... ->
   mark_email_completed) on every message the search returns, unchanged --
   ingest_email's own already_completed/dedup check makes it safe to
   include a message at or near the boundary twice across runs.
```

No other mode changes. Modes A/B/D/E/F/G are untouched — they already have
their own, different candidate-selection logic (exact count, named email,
one thread, explicit date range, sync, Q&A) that doesn't need a "what's the
boundary" step at all.

**Why `after:`/`before:` day-granularity is fine, not a gap:** Gmail search
only supports day-level date filters. A message on the exact same day as the
last-ingested one may be re-surfaced — this is intentional, not a bug: it's
what makes the boundary self-healing against a prior run that stopped
mid-day, and `ingest_email`'s existing dedup/retry-safety (Section 2) is
what makes re-surfacing safe rather than something this spec needs its own
new safeguard for.

## 6. Exact Files to Change

1. **`app/mcp/tools.py`** — add `get_last_ingested_email` (Section 4).
2. **`app/mcp/server.py`** — register it as an MCP tool.
3. **`skills/gmail-initial-ingest/SKILL.md`** — replace Mode C's candidate-
   selection instructions per Section 5.
4. **Tests**: `tests/test_mcp_query_tools.py` (new tests for
   `get_last_ingested_email`: empty collection returns `None`; returns the
   single email when one exists; returns the max-timestamp email among
   several; never crashes on a document missing `source_message_id`/
   `thread_id`), `tests/test_mcp_server.py` (registration test). No changes
   needed to any existing test file — the duplicate-detection and retry-
   safety behaviors this plan relies on already have their own passing
   tests today (Section 2), and this spec doesn't change that code at all.

## 7. What's Explicitly NOT Done Here

- No server-side LLM or search API call anywhere — confirmed, per the
  resolved architectural question. Classification and drafting remain
  entirely Claude-driven through the exact same granular tools as every
  other mode.
- No new "classify" or "draft" tool — `persist_email_analysis` and
  `create_reply_draft`/`set_reply_withheld_reason` are reused completely
  unchanged.
- No new duplicate-detection or retry-safety mechanism — both already exist
  and are already tested; this spec only adds a way to find the boundary to
  search from.
- No direct Gmail API/OAuth integration in this repository — Gmail access
  stays entirely external, via whichever Claude session's own Gmail
  connector is driving the skill, exactly as every other mode already works.
