# Baseline Report — Phase 0

Repo audited: `C:\Users\Deepanshu.sonwane\Documents\python\raw-dump-labeling-mcp`
Reference-only repo: `C:\Users\Deepanshu.sonwane\Documents\python\cos-sales-agent` (branch `ingestion-dump`, not modified).
No network calls made. No MongoDB Atlas/Gmail connection attempted. No migration/ingestion script run. `pytest` against `mongomock` was run once.

Legend: **FACT** = verified directly. **OPINION** = judgment, labeled. `NOT FOUND` = no evidence located.

---

## STEP 1 — Repo inventory

### Git state

**FACT.** `git status` in this repo returns `fatal: not a git repository (or any of the parent directories): .git`. There is no version control in place here — no branch, no commit history, no uncommitted-file diff to report. This is expected for a brand-new baseline repo, not an error condition.

### Top-level layout

**FACT** (from directory listing):

| Path | Contents |
|---|---|
| `app/` | `__init__.py`, `config.py`, `db.py`, `models.py`, `server.py`, `tools.py` |
| `tests/` | `test_core_engine_reliability.py`, `test_unsynced_labels.py`, `test_catch_up_scheduled_workflow.py` |
| `eval/` | 9 Python scripts + `label_eval_set.xlsx`, `rule_boundary_eval_results.xlsx`, `golden_set_live_run_results.xlsx`, `golden_set_live_run_report.md` |
| `skills/raw-dump-labeling/` | `SKILL.md` (343 lines) |
| `.env` | real local config (see Secrets below) |
| `requirements.txt` | dependency list, version-ranged (not pinned exact) |
| `TESTING.md` | hand-written testing doc (exists, not auto-generated) |
| `.pytest_cache/` | pytest's own cache dir |

No `README.md`, `.gitignore`, `.env.example`, or `docs/` existed before this task (all confirmed MISSING by direct `ls`/`Glob`, see STEP 2).

### Language and dependency files

**FACT.** Python, `requirements.txt` (`app/config.py`, `app/db.py` etc. — no `pyproject.toml`/`setup.py`/`Pipfile` found). Pins are range-bound, not exact:

```
pydantic>=2.6,<3.0
pydantic-settings>=2.2,<3.0
email-validator>=2.1,<3.0
pymongo>=4.6,<5.0
python-dotenv>=1.0,<2.0
mcp[cli]>=1.30,<2.0
pytest>=8.0,<9.0
mongomock>=4.3,<5.0
```

### Entrypoints

**FACT.**
- `app/server.py::main()` — the MCP server process (`if __name__ == "__main__": main()`, `app/server.py:141-142`). Runs `mcp.run()` (FastMCP stdio transport).
- No CLI entrypoint, no HTTP/FastAPI app, no scheduler script in `app/`. `eval/*.py` scripts are standalone, run manually, not wired to any entrypoint.

### MCP server and every tool it registers

**FACT.** `app/server.py` registers exactly **7 tools** via `@mcp.tool()` (verified by reading the full 143-line file; count matches `skills/raw-dump-labeling/SKILL.md:3`'s own claim "exposes exactly 7 tools"):

| Tool | Line | One-line purpose |
|---|---|---|
| `ingest_raw_email_only` | `app/server.py:22` | Idempotent raw persistence of one Gmail email into `raw_emails_dump`; auto-closes/reopens thread siblings deterministically. |
| `get_next_unprocessed_raw_email` | `app/server.py:44` | Atomically claims the single oldest email eligible for classification, with thread context attached. |
| `get_next_unprocessed_raw_email_batch` | `app/server.py:63` | Same claim semantics as above, batched (default 10) in one round trip. |
| `persist_raw_email_label` | `app/server.py:77` | Records a decided label; appends to `label_history`; marks Gmail out of sync. |
| `mark_raw_email_label_failed` | `app/server.py:88` | Records a failed classification attempt; releases the claim for retry (cap 3). |
| `mark_gmail_label_synced` | `app/server.py:97` | Compare-and-swap confirmation that the real Gmail label now matches MongoDB. |
| `get_unsynced_labels` | `app/server.py:116` | Read-only list of labels correct in Mongo but not yet confirmed synced to Gmail. |

No other `@mcp.tool()` decorator exists in this file or elsewhere in `app/` (confirmed: only `app/server.py` imports `FastMCP`).

### Collections and indexes created

**FACT** (`app/db.py:18-31`). One collection: `raw_emails_dump`. Three indexes, all created unconditionally at `initialize_database()` time:

| Index | Line | Backs |
|---|---|---|
| `source_message_id` (unique) | `app/db.py:19` | Idempotent upsert / dedup on ingestion. |
| `(classification_version, sort_key)` compound | `app/db.py:23` | `RawEmailDumpRepository.claim_next_unclassified`'s filter+sort shape. |
| `label_applied` | `app/db.py:25` | The skill's end-of-run label breakdown (per the code comment; not otherwise queried in this module). |

No other collection, no `runs`/`processing_runs` log collection, no counters collection.

### Skills / prompt files

**FACT.** One skill: `skills/raw-dump-labeling/SKILL.md` (343 lines). Covers Mode A (dump, explicit and scheduled-window), Mode B (label, batched, 6-label taxonomy), and sync catch-up. States its own exact scope: "This connector exposes exactly 7 tools -- never assume any other tool... exists here" (`SKILL.md:3-4`).

### Tests

**FACT.** 3 test files, 33 tests total, run via `pytest tests/ -v`. Result of running them once, this session, against `mongomock` (no network):

```
33 passed in 2.87s
```

All 33 passed, 0 failed, 0 skipped. Breakdown (per `TESTING.md:14-30`, cross-checked against the actual run output):
- `test_core_engine_reliability.py` — 22 tests (claim atomicity, TTL crash recovery, 3-attempt circuit breaker, classification_version eligibility, thread close/reopen lifecycle, ingestion idempotency, UTC sort_key normalization).
- `test_unsynced_labels.py` — 7 tests (`get_unsynced_labels` ordering, limit, exclusion of synced docs).
- `test_catch_up_scheduled_workflow.py` — 4 tests (5-page/50-doc catch-up budget, idempotent re-sync, persistent-failure visibility, stale-sync-confirmation race fix).

Separately, `eval/` contains two **non-pytest** evaluation harnesses (not run as part of this baseline — they are judgment-graded, not pass/fail code tests):
- Offline rule-boundary eval (16 synthetic cases): last recorded result 15/16 match (`TESTING.md:63-67`, read as a doc claim, not re-verified by me since grading is a manual Claude judgment pass, not a deterministic re-run).
- Real-mailbox eval sheet: last recorded result 14/14 match (`TESTING.md:82-83`), requires a live Gmail/Claude Desktop run — out of scope for this task's hard rules (no network).

### Config and env variables

**FACT** (`app/config.py:16-33`). `Settings` (pydantic-settings), loaded from `.env` resolved relative to the file's own location (not cwd):

| Field | Env var(s), priority order | Default |
|---|---|---|
| `mongodb_uri` | `SALES_AGENT_MONGODB_URI` > `MONGODB_URI` | `mongodb://localhost:27017` |
| `mongodb_database` | `SALES_AGENT_MONGODB_DATABASE` > `MONGODB_DATABASE` | `cos_sales` |
| `agent_email` | `AGENT_EMAIL` | `""` |
| `log_level` | `LOG_LEVEL` | `INFO` |

**Current live `.env` contents (FACT, values not reproduced per instruction — file:line only):**
- `.env:1-2` — `MONGODB_URI`/`MONGODB_DATABASE` set to a local `mongod` target.
- `.env:8` — `SALES_AGENT_MONGODB_URI` is present but **commented out**, and its value (visible in the file) is a live MongoDB Atlas `mongodb+srv://` connection string containing a **plaintext username and password**. This is a real, currently-unprotected credential sitting in a tracked-looking file with no `.gitignore` in place (see STEP 2).
- `.env:9` — `SALES_AGENT_MONGODB_DATABASE` (Atlas database name) also commented out, same line group.
- `.env:14` — `AGENT_EMAIL` is set to a real-looking operator email address (not cos-sales-agent's placeholder/demo value).

**LOCKED DECISIONS discrepancy (factual, not a fix applied):** the locked decision for this project states storage is MongoDB Atlas. The live `.env` today has the Atlas URI/database **commented out** and points at `mongodb://localhost:27017` / database `cos_sales` instead — i.e., **this repo is not currently configured to use Atlas**, contradicting the locked decision. This was deliberate (left in place for local-only testing) and is not something this task changes; it is logged as an open item in `docs/DECISIONS.md`.

### Anything that touches the network

**FACT.** Only `app/db.py::get_client` (`MongoClient(uri, ...)`, `app/db.py:14-15`) ever opens a network connection, and only when the MCP server or a test actually calls `get_client`/`initialize_database`. The 33 pytest tests never call this — they construct `mongomock.MongoClient()` directly (confirmed: `import mongomock` in all three test files, no `app.db.get_client` import). No HTTP calls, no Gmail API calls, exist anywhere in `app/`. Nothing in this task connected to Atlas, Gmail, or any other network service.

---

## STEP 2 — What's missing for a clean baseline

| Item | Status | Evidence |
|---|---|---|
| README | **MISSING** | No `README.md` at repo root (`ls` returned "No such file or directory"). |
| `.gitignore` | **MISSING** | No `.gitignore` at repo root before this task (added now, see below — trivial hygiene only). |
| `.env.example` with placeholders only | **MISSING** | No such file existed before this task (added now, placeholders only). |
| Dependency pinning | **PARTIAL** | `requirements.txt` exists but uses range pins (`>=X,<Y`), not exact versions — reproducible within a major version, not bit-for-bit reproducible. |
| A test command | **EXISTS (documented, not scripted)** | `TESTING.md:11` documents `pytest tests/ -v`; no `Makefile`/`tox.ini`/`pyproject.toml` script wrapper exists, but the command itself is simple and works as-is (verified by running it). |
| Documented MongoDB database name and collection list | **PARTIAL** | The database name is only ever a config default/env var (`cos_sales`, `app/config.py:26`), not documented in any `docs/` file before this task. Collection list (`raw_emails_dump`) is documented only inside code comments/docstrings, not in a standalone doc — now covered in this report. |
| `docs/` folder | **MISSING** | Did not exist before this task; created now to hold the four files this task writes. |

### Secrets / real data / hardcoded operator-specific values found

**FACT**, file:line only, values not printed:

| Finding | Location | Severity |
|---|---|---|
| Real MongoDB Atlas credential (username + password) in plaintext | `.env:8` | High — a live credential sitting in an un-gitignored file. Not committed anywhere (no git repo exists), but at risk the moment one is initialized without a `.gitignore` in place first. |
| Real operator email address | `.env:14` | Medium — operator-specific, not a placeholder. Needed for the `agent_email` reply-detection feature to function at all; not a bug, but real PII in a config file. |
| No committed secrets elsewhere | `app/`, `tests/`, `eval/*.py` | — | Test fixtures use `example.com`/placeholder-style addresses (spot-checked `tests/test_core_engine_reliability.py:34`: `jane@customerco.example`, `agent@ourcompany.example` — fictional, RFC 2606 reserved `.example` domain). No real names/addresses found in test code. |
| `eval/label_eval_set.xlsx`, `eval/golden_set_live_run_results.xlsx` | `eval/` | **NOT INDEPENDENTLY VERIFIED** — these are binary `.xlsx` files; I did not open/parse them for real email content as part of this read-only audit (out of scope: STEP 1/2 ask about code-level findings, and opening a spreadsheet of a live eval run risks surfacing real mailbox data I'd then have to decide whether to report in full, which this task's "never print secret values" instruction doesn't cleanly cover for non-secret PII). **Flagging as unverified, not as clean.** |

No hardcoded operator-specific logic (sender rules, domain allowlists) was found anywhere in `app/*.py` — the only operator-specific value in code (vs. config) is the `.env` content above, which is config, not code.

---

## Scope skipped in this audit

- Did not open/parse the three `.xlsx` files in `eval/` for content (binary, out of scope for a code-level baseline; flagged above as unverified rather than assumed clean).
- Did not run the `eval/run_rule_boundary_eval.py` or any other `eval/*.py` script (these require a judgment-grading pass, not a pass/fail run, and are not part of STEP 1's "run tests once" instruction, which concerns `tests/`, not `eval/`).
- Did not attempt to start the MCP server process itself (`app/server.py::main`) — starting it would call `get_settings()`/`initialize_database()`, which opens a real MongoDB connection; forbidden by the hard rules.
