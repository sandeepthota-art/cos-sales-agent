# raw-dump-labeling-mcp

A minimal, standalone MCP server that dumps Gmail emails into MongoDB
unanalyzed, then classifies them with one of six labels — kept current as a
conversation evolves (a reply closes an open item; a new inbound message
reopens one for re-evaluation). Built as a deliberately smaller, simpler
alternative to `cos-sales-agent`'s full pipeline, living in this repo as a
separate subfolder/branch because the two are intentionally decoupled: this
project has its own `app/`, its own `requirements.txt`, its own `.env`, and
imports nothing from the rest of this repo.

## 1. What this is

Five MCP tools, five files, one MongoDB collection (`raw_emails_dump`):

| Tool | Does |
|---|---|
| `ingest_raw_email_only` | Stores a raw Gmail email, zero analysis. Also runs the deterministic thread-lifecycle checks (see §3). |
| `get_next_unprocessed_raw_email` | Atomically claims the oldest email eligible for classification, with full thread context attached. |
| `persist_raw_email_label` | Records the label Claude decided for one email. |
| `mark_raw_email_label_failed` | Records a classification attempt that couldn't complete (retryable, capped at 3 attempts). |
| `mark_gmail_label_synced` | Confirms the real Gmail label was actually updated to match — a separate, trackable fact from the MongoDB write. |

The six labels (same taxonomy `cos-sales-agent`'s `EmailAnalysis.label_applied`
uses): `1. Needs reply: ASAP`, `1. Needs reply`, `1. Needs reply: mention`,
`1. Read only`, `1. Delete`, `1. Undecided`.

A companion skill, [`skills/raw-dump-labeling/SKILL.md`](skills/raw-dump-labeling/SKILL.md),
is what actually drives these tools from a Claude Desktop session — the
tools themselves do no classification; Claude reads each email and decides.

## 2. Why this exists, not just a feature inside cos-sales-agent

`cos-sales-agent`'s own `ingestion-dump` branch already has an
`ingest_raw_email_only` tool writing to `raw_emails_dump` — but that
project's full MCP server carries 20+ tools, 20+ MongoDB collections, and an
entity/knowledge/commitment extraction pipeline that has nothing to do with
"dump an email, then label it." Three reasons this is a separate project
instead of growing inside that one:

1. **Two orphaned files found during a codebase audit of `cos-sales-agent`**
   (`production_indexes.py`, `production_verification.py` — real code, zero
   callers) surfaced a broader question: how much of that codebase was
   genuinely load-bearing versus accumulated surface area. The answer, for
   the one feature actually being iterated on (raw-dump + labeling), was:
   almost none of it. Everything this feature needs is 5 functions, 1
   collection, 3 indexes.
2. **Faster iteration.** Every round of "add a field, change a rule, re-test"
   during this project's build touched 2 files (`db.py`, `tools.py`) and ran
   against a live MongoDB Atlas cluster in seconds — no 1600+ test suite, no
   unrelated collections to reindex, no risk of a change here breaking
   `cos-sales-agent`'s actual production pipeline.
3. **Honest scope.** This project was explicitly NOT built with a staging
   queue, thread-revision/concurrency control, or Gmail-sync retry/backoff —
   all were proposed during development and deliberately rejected as solving
   problems this single-operator, chat-driven workflow doesn't actually have
   (see §5, "What was deliberately not built"). Keeping it a separate,
   small codebase made that trade-off visible instead of buried in a larger
   system's sprawl.

## 3. How it works

### 3.1 Two modes, one skill

**Mode A — Dump.** Claude reads a Gmail message via the Gmail connector
(external, not part of this server), maps it to the `Email` shape, calls
`ingest_raw_email_only`. Pure persistence — the tool makes zero
classification decisions.

**Mode B — Label.** Claude calls `get_next_unprocessed_raw_email` in a loop
(capped at 20/run). For each email returned, Claude reads its content *and*
`thread_context` (every other email in the same conversation), decides one
of the six labels using the rules in `SKILL.md`, calls
`persist_raw_email_label`, then updates the real Gmail label and confirms
with `mark_gmail_label_synced`.

### 3.2 The lifecycle problem this solves

A label decided once is wrong the moment the conversation moves on. Two
deterministic rules handle this without ever needing Claude to re-scan
everything:

- **Agent replies close open items.** If `ingest_raw_email_only` sees a new
  email FROM the configured `AGENT_EMAIL`, landing in a thread that has
  another email still labelled `Needs reply*`, that item is set to
  `1. Read only` immediately — no judgment call, just "we replied, so
  nothing is pending from us in this thread anymore." (`_close_open_needs_reply_in_thread`
  in `app/tools.py`.)
- **New inbound messages reopen siblings.** If the new email is NOT from the
  agent, every other already-classified email in that thread becomes
  eligible for re-evaluation again — reusing the exact same
  `classification_version` mechanism a global taxonomy-rule change uses, so
  no separate queue/staging state exists. Claude decides the new label next
  time it's claimed, with the new message now visible in `thread_context`.
  (`_reopen_thread_siblings_for_reevaluation` in `app/tools.py`.)

### 3.3 Gmail-sync is tracked, not assumed

A successful MongoDB write is never proof the matching Gmail label call
also succeeded — those are two separate systems, two separate writes. Every
document carries `gmail_label_synced` (bool); `persist_raw_email_label` and
the auto-close path both set it `False`, and only `mark_gmail_label_synced`
— called after the skill confirms the real Gmail API call worked — sets it
back to `True`. No automatic retry; a `False` value is just visibly,
honestly out of sync until someone runs it again.

## 4. Code walkthrough

```
app/
  config.py   Settings (pydantic-settings) -- mongodb_uri/database, agent_email, log_level
  models.py   Email/EmailAddress (pydantic), EmailLabel (the six-label Literal type)
  db.py       MongoDB connection + indexes + the repository (generic ops only)
  tools.py    The 5 MCP tools' real implementations -- all business/lifecycle rules live here
  server.py   FastMCP app, registers the 5 tools, stdio transport
eval/         One-off scripts that built/filled the validation spreadsheet (see §6)
skills/raw-dump-labeling/SKILL.md   The prompt that drives all of this from Claude Desktop
```

### `app/db.py` — pure data access, no business meaning

`_BaseRepository` has exactly four generic methods: `upsert_by_key`,
`update_many_by_key`, `find_one`, `find_many`. `RawEmailDumpRepository` adds
two things that genuinely need direct pymongo access:

- `claim_next_unclassified` — the atomic "find and lock" operation behind
  `get_next_unprocessed_raw_email`. One `find_one_and_update` with every
  threshold (`max_classification_version`, `claim_cutoff_iso`, `max_attempts`)
  passed in as a parameter — this method has no hardcoded knowledge of what
  those numbers mean, it just knows how to run the atomic query. Two subtle
  things worth knowing if you touch this method:
  - **The "unclaimed" sentinel is `""`, not `None`.** `"" < any ISO timestamp`
    is always true in BSON string ordering, so one `label_claimed_at < cutoff`
    comparison covers both "never claimed" and "claim expired" — no `$or`
    needed.
  - **`return_document=ReturnDocument.BEFORE`, not `AFTER`.** A confirmed
    `mongomock` 4.3.0 bug: `find_one_and_update` silently fails to match
    when a multi-condition query + `sort` + `projection` + an update that
    `$set`s the SAME field a condition filters on are combined with
    `AFTER`. Verified directly against real Atlas — the bug is
    `mongomock`-only. `BEFORE` sidesteps it; the actual write still happens
    identically either way (confirmed by a follow-up read in testing). The
    returned dict is patched with the new `label_claimed_at` value manually
    before returning.
- `siblings_in_thread` — plain read, every other email sharing a `thread_id`.

Nothing else lives here. No label-meaning, no "what counts as needs-reply",
no history-entry shape.

### `app/tools.py` — all business rules, the 5 public functions

Two private helpers remove duplication:
- `_now_iso()` — was `datetime.now(timezone.utc).isoformat()` repeated 5x.
- `_require_existing(repo, message_id)` — was the same
  `find_one` -> `if None: raise ValueError` pattern repeated 3x.

Two private functions hold the thread-lifecycle rules described in §3.2
(`_close_open_needs_reply_in_thread`, `_reopen_thread_siblings_for_reevaluation`)
— moved here from `db.py` specifically because deciding *what a label change
means* is a business decision, not a database operation; the repository
should stay reusable for anything, the rules should live in exactly one
place.

The five public functions (`ingest_raw_email_only`, `get_next_unprocessed_raw_email`,
`persist_raw_email_label`, `mark_raw_email_label_failed`,
`mark_gmail_label_synced`) are each under 40 lines, map 1:1 to an MCP tool
in `server.py`, and are individually documented in their own docstrings —
read those for the exact contract of each.

### `app/models.py` / `app/config.py`

Both intentionally small: `Email`/`EmailAddress` are a trimmed copy of
`cos-sales-agent`'s own email models (this project never canonicalizes
`message_id` to an `EML-nnn` id the way that pipeline does — the real Gmail
id is kept as-is throughout, since there's no entity-resolution layer here
that needs a stable internal id). `Settings` only declares the three fields
this project actually reads (`mongodb_uri`, `mongodb_database`,
`agent_email`, `log_level`) — not the 30+ fields `cos-sales-agent`'s
`Settings` class carries for providers/scheduling/dashboards/auth this
project has none of.

## 5. What was deliberately not built

Proposed during development, rejected as solving a problem this
single-operator, Claude-Desktop-driven workflow doesn't have:

- **A staging/re-evaluation queue separate from the label.** Reusing
  `classification_version` (reset to `0` to reopen) does the same job with
  zero new state.
- **Thread-revision IDs + stale-result rejection.** Solves concurrent
  automated agents racing each other. This system is one operator
  triggering Mode A/B passes manually; the existing atomic claim
  (`claim_next_unclassified`) already prevents two *simultaneous calls*
  from grabbing the same email, which is the actual concurrency guarantee
  this workflow needs.
- **Gmail-sync retry/backoff automation.** `gmail_label_synced: false` is
  visible and manually retryable by re-running Mode B — an automated retry
  loop would be new infrastructure for a failure mode that, so far, hasn't
  recurred.
- **A generic list/query tool.** No way to ask "show me everything
  currently labelled X" without re-deriving it from a live Claude session's
  own memory. A real, acknowledged gap — not fixed, because nothing has
  needed it enough yet to justify the tool.

## 6. Validated against real Gmail — 14/14

`eval/label_eval_set.xlsx` tracks a real-world classification accuracy run:
14 real emails, sent from 5 different real mailboxes, dumped and labelled
through the actual MCP tools (not simulated). Every row now matches.

What that run actually found, worth knowing before trusting this blind:

- **3 of the first 4 "mismatches" were wrong ground truth, not model
  errors.** Two independent models (Haiku, then Sonnet, then Sonnet at high
  effort) converged on identical labels every time — when weaker and
  stronger models agree, that's evidence the *rules* support that reading,
  not that a stronger model would "catch" something different. Re-reading
  the six-label rules against the actual email content confirmed the
  original `Expected Label` was the wrong call in each case, not the
  classifier.
- **One real rule gap was found and fixed.** A standalone `"Thoughts?"`
  with zero context was being classified `Needs reply` instead of the
  intended `Undecided` — not a model limitation, but `SKILL.md`'s rules
  genuinely not distinguishing "short" from "contextless." Fixed with a
  narrower rule (contextless -> `Undecided`; short-but-has-a-topic -> still
  `Needs reply`), then validated **blind** (3 fresh emails, brand-new
  `message_id`s, zero anchoring from a prior label) — 3/3 match.
- **The thread-lifecycle rules (§3.2) were verified live**, not just unit
  tested: an agent reply correctly auto-closed a prior item to
  `1. Read only`; a subsequent inbound message correctly reopened it with
  the new message visible in `thread_context` on the next claim.

## 7. Setup & running

```powershell
cd raw-dump-labeling-mcp
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
# edit .env: set a real MONGODB_URI/SALES_AGENT_MONGODB_URI and AGENT_EMAIL
```

Register with Claude Desktop (`claude_desktop_config.json`):
```json
{
  "mcpServers": {
    "raw-dump-labeling": {
      "command": "<path to .venv>/Scripts/python.exe",
      "args": ["-m", "app.server"],
      "env": { "PYTHONPATH": "<path to this folder>" }
    }
  }
}
```

Open this folder as a project/workspace in Claude Desktop too (not just the
MCP `PYTHONPATH`) so `skills/raw-dump-labeling/SKILL.md` actually loads —
the MCP connection alone only gives Claude the 5 tools, not the rules for
using them.

Then, in a Claude Desktop chat: *"Dump my last 10 emails"* (Mode A), or
*"Label the ingested data"* (Mode B).

## 8. Indexes

`app/db.py::initialize_indexes`, created automatically on first connection:

| Index | Backs |
|---|---|
| `source_message_id` (unique) | Idempotent dedup on re-ingestion |
| `(classification_version, sort_key)` | `claim_next_unclassified`'s exact filter+sort shape |
| `label_applied` | The skill's end-of-run label breakdown |
