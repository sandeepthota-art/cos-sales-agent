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
- [x] Chronology — auto-close now only closes a "Needs reply*" sibling strictly EARLIER than the closing agent reply by `(sort_key, source_message_id)`; a later sibling is never touched. `xfail` removed from `test_auto_close_never_closes_a_sibling_sent_after_the_agent_reply` (`tests/test_claim_fencing_and_chronology.py`) — it passes for real. See `docs/DECISIONS.md` decision log (same entry also covers the newest-first label-claim ordering change).

## Phase 3 — Input hygiene and validation

- [x] Quote/signature stripping before classification — `app/text_clean.py::strip_quotes_and_signature`, deterministic, conservative (keeps text when a boundary isn't clearly recognizable, never returns empty). Returned as `body_clean` in claim results (single + batch) and thread_context entries, never written over raw `body` (Mode A's raw-preservation rule untouched). 12 table-driven tests, `tests/test_text_clean.py`.
- [x] Evidence check — `persist_raw_email_label` now requires `evidence` (<=300 chars, non-empty), checked case/whitespace-insensitively against the email's own `subject`/`body_clean` plus every thread sibling's — rejects with `{"ok": false, "reason": "evidence_not_found"}` instead of writing, checked before the `claim_id` compare-and-swap. Stored in `label_history`. 8 tests, `tests/test_evidence_check.py`.
- [ ] Sender-type gate (human/automated/newsletter/cold-outreach) — **SKIPPED.** Checked `app/models.py`'s `Email` model: no `List-Unsubscribe`/`Precedence`/`Auto-Submitted` header fields are captured anywhere in this project's schema, so there's nothing to derive `sender_hints` from. Per the task's own instruction, skipped rather than fabricated. Revisit if/when header capture is ever added to `ingest_raw_email_only`'s field mapping.
- [x] Sent-time ordering guarantees for out-of-order/backfilled mail — already confirmed correct and tested in Phase 0/1 (`test_get_next_unprocessed_raw_email_claims_newest_by_sort_key`, renamed when claim order flipped — see `docs/DECISIONS.md`); no change needed this phase.

## Phase 4 — Complete the context model

Done 2026-10-13. See `docs/PHASE4_DESIGN.md` for the full design (collections,
tools, rules, deviations) and `docs/DECISIONS.md`'s decision log entry.

- [x] Thread status (active/waiting_on_us/waiting_on_them/resolved/unknown) — `threads.status`, set via `persist_context_delta`.
- [x] Email intent/sentiment — `email_insights.intent`/`.sentiment`/`.sender_type`, one doc per message.
- [x] Person-org link with validity period — `person_org_links` (`valid_from`/`valid_to`, `valid_to` set only on evidenced departure, never by a domain change alone).
- [x] Rebuildable profiles — `people`/`organizations.profile` rebuilt from the append-only `observations` log (latest `observed_at` wins); `scripts/rebuild_profiles.py` reproduces this from scratch.
- [x] Decide the `raw_emails_dump` vs. future `emails`/context collection relationship — closed, `docs/DECISIONS.md` closed question #1: read directly, no canonical id, no separate collection.
- [ ] Deferred (not in this phase, see `docs/PHASE4_DESIGN.md`'s deferred-items table): weekly consolidation pass, relabel-on-context-arrival/`label_basis`, a run log, a sender-type gate in labeling.

## Phase 5 — Evaluation (golden set)

Done 2026-10-13. See `docs/EVAL_RUNBOOK.md` for how to run it and
`eval/README.md` for what's synthetic vs. local-only real-data.

- [x] Golden set for labeling: `eval/golden/labeling_cases.jsonl` (40
      cases, all six labels present >=3 times each, all 16 required
      categories covered) — includes the original `eval/rule_boundary_cases.py`
      cases converted into this format (that file is kept as-is, now a
      source the golden set was derived from, not replaced).
- [x] Golden set for context extraction: `eval/golden/context_cases.jsonl`
      (18 threads, each with required/forbidden facts).
- [x] Isolation guard (`scripts/_common.py::require_eval_database`) --
      every eval script refuses to run against a database not ending in
      `_eval`.
- [x] `scripts/load_golden.py` -- loads both golden sets through
      `ingest_raw_email_only`, the same function the real pipeline uses.
      Dry-run default, `--apply` to write.
- [x] `scripts/score_eval.py` -- accuracy, per-label precision/recall,
      confusion matrix, critical-error detection (Needs-reply*
      downgraded, injection obeyed), evidence_not_found/stuck cases,
      context precision/recall, forbidden-fact detection. Writes
      `eval/reports/<date>_<skill_version>.md`.
- [x] Eval-mode section added to `skills/raw-dump-labeling/SKILL.md`
      (skips all Gmail calls when the prompt says `EVAL MODE`).
      `skills/context-building/SKILL.md` needed no change -- it never
      calls Gmail regardless of mode.
- [x] 24 new deterministic pytest tests (golden-file validation, eval-db
      guard, load/score logic) -- no LLM, no network.
- [ ] Still not automated end-to-end: a REAL Claude Desktop run is
      required to produce `label_applied`/observations to score against
      -- this phase built the harness and golden data, not a way to run
      Claude Desktop from pytest. That's by design (`docs/EVAL_RUNBOOK.md`),
      not a gap.

## Phase 6 — BRD features

- [ ] Not yet scoped against a BRD document for this specific repo (unlike cos-sales-agent, no `*BRD*` file was found in this repo).

## Phase 7 — Cleanup

- [ ] Not started.

---

## Imported from CoS

| Date | Component | Source path | Imported as (path here) | Changes made from source |
|---|---|---|---|---|
| 2026-10-13 | Thread resolution fallback matching (carryover #4) | `cos-sales-agent/app/email/threading.py` (76 lines) | `app/threading.py` + wired into `app/tools.py::ingest_raw_email_only` | No canonical `THR-nnn` ids (never synthesizes one, reuses an existing raw Gmail `thread_id`); candidates built on the fly from `raw_emails_dump` docs (no separate `threads` collection here); dropped the pydantic `ThreadCandidate` in favor of a plain frozen dataclass; fixed a related latent bug while wiring it in — `thread_id`/`source_thread_id` now frozen after first write so a re-ingest can't recompute/drift them. |
