# Roadmap — raw-dump-labeling-mcp

Phase tracker. Checkboxes reflect verified state as of 2026-10-13 (see `docs/BASELINE_REPORT.md` for evidence behind every Phase 0 mark).

## Phase 0 — Baseline

- [x] Repo inventory documented (git state, layout, entrypoints, MCP tools, collections/indexes, skills, tests, config, network surface) — `docs/BASELINE_REPORT.md` STEP 1.
- [x] Tests run once and results recorded — 33/33 passed in 2.87s against `mongomock`, no network (`docs/BASELINE_REPORT.md` STEP 1, Tests).
- [x] Gap check done (README/.gitignore/.env.example/dependency pinning/test command/DB docs/`docs/` folder) — `docs/BASELINE_REPORT.md` STEP 2.
- [x] Secrets/real-data/operator-specific-value scan done — found a real Atlas credential and a real operator email in `.env` (file:line only, not reproduced); no secrets in code or test fixtures.
- [x] `.gitignore` added (covers `.env`, local data, caches).
- [x] `.env.example` added (placeholders only).
- [x] Carry-over candidates inventoried from cos-sales-agent (12 components) — `docs/CARRYOVER.md`.
- [x] Tracker and decisions files created — this file, `docs/DECISIONS.md`.
- [x] Open questions written down — `docs/DECISIONS.md` Open questions.
- [ ] Not a git repository yet — no branch, no commits, no version control at all. Out of scope for this task to initialize (task only documents state); flagged here so Phase 1 doesn't assume git exists.
- [ ] Atlas vs. local `.env` discrepancy unresolved (locked decision says Atlas; live `.env` points at local `mongodb://localhost:27017`) — user decision needed, see `docs/DECISIONS.md`.

## Phase 1 — Protect

- [ ] `git init` + first commit (currently no version control at all).
- [ ] Confirm `.gitignore` actually keeps `.env`/secrets/local data out of the first commit (re-check after `git init`, since it was added before any repo existed).
- [ ] Decide and resolve the Atlas-vs-local `.env` discrepancy before any shared/deployed use.

## Phase 2 — Safe independence

- [ ] Status fields beyond `classification_version`/`label_claimed_at`/`label_attempts` if a richer run-state model is needed.
- [ ] Claim/lease — **already implemented**: `app/db.py::claim_next_unclassified`/`claim_batch_unclassified`, TTL-based (`_LABEL_CLAIM_TTL_MINUTES`, `app/tools.py:18`). Verify this box only after confirming no further hardening is needed.
- [ ] Retry cap — **already implemented**: `_MAX_LABEL_ATTEMPTS = 3` (`app/tools.py:22`), circuit breaker tested (`tests/test_core_engine_reliability.py`). Mark done after explicit review.
- [ ] Run log — **MISSING today.** No `runs`/`processing_runs`-style collection exists in this repo (only `raw_emails_dump`). Needed before this phase can be marked complete.

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
