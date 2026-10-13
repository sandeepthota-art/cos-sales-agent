# Roadmap — raw-dump-labeling-mcp

Phase tracker. Checkboxes reflect verified state as of 2026-10-13 (see `docs/BASELINE_REPORT.md` for evidence behind every Phase 0 mark).

## Phase 0 — Baseline

- [x] Repo inventory documented (git state, layout, entrypoints, MCP tools, collections/indexes, skills, tests, config, network surface) — `docs/BASELINE_REPORT.md` STEP 1.
- [x] Tests run once and results recorded — 33/33 passed in 2.87s against `mongomock`, no network (`docs/BASELINE_REPORT.md` STEP 1, Tests). **Updated 2026-10-13**: now 42/42 (33 baseline + 9 added for the thread-resolution fallback carry-over, `tests/test_thread_resolution_fallback.py`).
- [x] Gap check done (README/.gitignore/.env.example/dependency pinning/test command/DB docs/`docs/` folder) — `docs/BASELINE_REPORT.md` STEP 2.
- [x] Secrets/real-data/operator-specific-value scan done — found a real Atlas credential and a real operator email in `.env` (file:line only, not reproduced); no secrets in code or test fixtures.
- [x] `.gitignore` added (covers `.env`, local data, caches).
- [x] `.env.example` added (placeholders only).
- [x] Carry-over candidates inventoried from cos-sales-agent (12 components) — `docs/CARRYOVER.md`.
- [x] Tracker and decisions files created — this file, `docs/DECISIONS.md`.
- [x] Open questions written down — `docs/DECISIONS.md` Open questions.
- [x] **Updated 2026-10-13**: `git init -b main` done, Phase 1 STEP 1 privacy scan completed first, two commits made (baseline; eval harness, real-data files gitignored not committed). See Phase 1 below.
- [ ] Atlas vs. local `.env` discrepancy unresolved (locked decision says Atlas; live `.env` points at local `mongodb://localhost:27017`) — user decision needed, see `docs/DECISIONS.md`.

## Phase 1 — Protect

- [x] Privacy scan before `git init` — `.gitignore` additions and `docs/CARRYOVER.md` scrub (real third-party domain replaced with a generic phrase) done first.
- [x] `git init -b main` + two commits: baseline (`eea1dab`, 20 files), eval harness (`80f6f49`, 7 files — only the privacy-scan-approved synthetic files).
- [x] Confirmed `.gitignore` actually keeps `.env`/caches/the 6 real-data eval files out — verified via `git status --ignored` after init, before staging.
- [x] Tool allowlists written per scheduler, with exact Gmail tool names (`list_labels`, `label_message`, `unlabel_message`) and an expanded forbidden list (`create_label`/`update_label`/`delete_label`/`*_thread` variants/`apply_sensitive_*`) — `ops/scheduled-tasks/ingestion.md`, `labeling.md`.
- [x] Scheduled-task specs written (purpose, cadence, allowed/forbidden tools, exact prompt text, concurrency, failure modes) — `ops/scheduled-tasks/` (`ingestion.md`, `labeling.md`, `context-building.md` stub, `README.md` with one-time label-setup checklist and Enforcement section).
- [x] No operator-specific hardcoding confirmed — grepped `app/`/`skills/` for known real domains and always-Delete/Read-only-for-X patterns, zero matches.
- [x] `AGENT_EMAIL` empty-case handled: startup warning (`app/server.py::main`) and a regression test (`tests/test_core_engine_reliability.py::test_empty_agent_email_takes_reopen_path_never_auto_closes`) confirming no auto-close, reopen path taken instead.
- [x] Gmail sync moved out of ingestion entirely (`skills/raw-dump-labeling/SKILL.md` step 4a) into labeling's sync catch-up — ingestion now makes zero Gmail write calls. See `docs/DECISIONS.md` decision log.
- [ ] Decide and resolve the Atlas-vs-local `.env` discrepancy before any shared/deployed use — still open (user confirmed "keep local for now" as an interim exception, not a resolution).
- [ ] Confirm the real Gmail connector's exact tool names (`list_labels`/`label_message`/`unlabel_message` and the full forbidden list) from a live Claude Desktop session — everything in `ops/scheduled-tasks/` is evidence-based from `SKILL.md`'s own documentation, not a verified live tool schema (no Gmail MCP tools are loadable from this coding session).
- [ ] Connector-permissions UI may not support per-scheduled-task scoping — `ops/scheduled-tasks/README.md`'s Enforcement section documents that blocking `label_message`/`unlabel_message` for ingestion specifically (and Gmail read for labeling specifically) is prompt-level only today, not a technical guarantee.

## Phase 2 — Safe independence

- [x] Status fields — `classification_version`/`label_claimed_at`/`label_attempts`/`claim_id` judged sufficient; no richer run-state model needed for this phase's scope.
- [x] Claim/lease, hardened with fencing — `claim_id` (uuid4 hex) returned by `get_next_unprocessed_raw_email(_batch)`, required by `persist_raw_email_label`/`mark_raw_email_label_failed`, compare-and-swap (`app/db.py::update_if_claim_matches`) rejects a stale/unknown claim_id with `{"ok": false, "reason": "claim_lost"}` instead of silently overwriting. Any ingestion write to a document (reopen, auto-close, or any new thread message via `_clear_active_claims_in_thread`) clears its claim. `claim_batch_unclassified` rewritten as a loop of individually atomic single-document claims sharing one `claim_id`, closing the old find()-then-update_many TOCTOU window. Regression-tested in `tests/test_claim_fencing_and_chronology.py`.
- [x] Retry cap, now visible and recoverable — `get_stuck_emails` (read-only MCP tool, label_attempts >= cap) closes the gap where `SKILL.md` promised human-reviewability with no tool to find them. Recovery is `scripts/reset_stuck.py` (local-only, dry-run by default, never an MCP tool).
- [ ] Run log — **DEFERRED.** Two options proposed (new `start_run`/`finish_run` tools vs. a `processing_runs` collection written by existing tools), not decided or implemented this pass.
- [ ] Chronology — **DEFERRED.** Auto-close doesn't check whether a sibling was sent (by `sort_key`) after the closing agent reply; real gap, test exists and is marked `xfail(reason="deferred")` (`tests/test_claim_fencing_and_chronology.py`), not fixed this pass.

## Phase 3 — Input hygiene and validation

- [ ] Quote/signature stripping before classification — **MISSING in this repo, and confirmed MISSING in cos-sales-agent too** (`docs/CARRYOVER.md` #8) — nothing to import, must be written fresh.
- [ ] Sender-type gate (human/automated/newsletter/cold-outreach) before classification — not implemented here; cos-sales-agent also leaves this to LLM judgment at label time, not a separate gate (per `cos-sales-agent/docs/AUDIT_REPORT.md` §4).
- [ ] Evidence check (classification claims traceable to visible text) — not implemented; currently relies entirely on the Claude Desktop classifier's own judgment, per locked decision #3.
- [ ] Sent-time ordering guarantees for out-of-order/backfilled mail — `sort_key` (UTC-normalized at ingestion, `app/tools.py:126-130`) already gives correct chronological claim order; verify this holds for backfilled mail specifically before marking done.

## Phase 4 — Complete the context model

- [ ] Thread status (active/waiting/resolved) — does not exist here or in cos-sales-agent (`ThreadContext` has no such field either, per cos-sales-agent's own audit).
- [ ] Email intent/sentiment — does not exist here; cos-sales-agent has `intent` (free text) but no `sentiment` field at all.
- [ ] Person-org link with validity period — no Person/Organization entities exist here yet at all (`docs/CARRYOVER.md` #1/#2, both SKIPPED for now).
- [ ] Rebuildable profiles — no profile concept exists here yet.
- [ ] Decide the `raw_emails_dump` vs. future `emails`/context collection relationship (see `docs/DECISIONS.md` open question #1) before starting this phase.

## Phase 5 — Evaluation (golden set)

- [ ] Automated golden-set label-accuracy harness. Today: two non-automated, judgment-graded eval sets exist (`eval/rule_boundary_cases.py` — 16 synthetic cases, 15/16 match last run; `eval/label_eval_set.xlsx` — 14/14 match last run, requires a live mailbox). Neither is a pytest-style automated pass/fail gate.

## Phase 6 — BRD features

- [ ] Not yet scoped against a BRD document for this specific repo (unlike cos-sales-agent, no `*BRD*` file was found in this repo).

## Phase 7 — Cleanup

- [ ] Not started.

---

## Imported from CoS

| Date | Component | Source path | Imported as (path here) | Changes made from source |
|---|---|---|---|---|
| 2026-10-13 | Thread resolution fallback matching (carryover #4) | `cos-sales-agent/app/email/threading.py` (76 lines) | `app/threading.py` + wired into `app/tools.py::ingest_raw_email_only` | No canonical `THR-nnn` ids (never synthesizes one, reuses an existing raw Gmail `thread_id`); candidates built on the fly from `raw_emails_dump` docs (no separate `threads` collection here); dropped the pydantic `ThreadCandidate` in favor of a plain frozen dataclass; fixed a related latent bug while wiring it in — `thread_id`/`source_thread_id` now frozen after first write so a re-ingest can't recompute/drift them. |
