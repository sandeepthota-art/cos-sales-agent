# Handoff — raw-dump-labeling-mcp

Read this first if you're picking up this repo cold. `docs/PHASE_PROMPTS.md`
does not exist in this repo — no pointer to follow there.

## Phase status

| Phase | Status | Commit(s) |
|---|---|---|
| 0 — Baseline | Done | `eea1dab` |
| 1 — Protect | Done | `eea1dab`, `80f6f49`, `7932b1e` |
| 2 — Safe independence | Done (chronology fix + run log explicitly deferred, xfail'd) | `c40211f`, `b97285b` |
| 3 — Input hygiene and validation | Done (sender-hints item explicitly skipped, no header fields to derive from) | `3ec39ab` |
| Backfill support (ad hoc, not a numbered phase) | Done | `eaf512b`, `8348f71` |
| 4 — Complete the context model | Done, 4A + 4B | `1a5ff70` (4A), `2f98136` (4B a/b), `78ef337` (4B skill/ops/docs) |
| 5 — Evaluation (golden set) | Not started | — |
| 6 — BRD features | Not started, not scoped against a BRD doc | — |
| 7 — Cleanup | Not started | — |

## Built vs. verified

**Built and unit-tested** (synthetic data, `mongomock`, 127 tests —
see "How to run tests" below): ingestion (Mode A, explicit/backfill/
scheduled), labeling (Mode B + sync catch-up), context building (claim/
persist/fail/stuck, profile rebuild, person-org links, thread state).

**NOT verified against real Claude Desktop output.** Every test in this
repo runs against synthetic fixtures and `mongomock` — there is zero
confirmation yet that a real Claude Desktop session reads
`skills/raw-dump-labeling/SKILL.md` or `skills/context-building/SKILL.md`
correctly, calls the tools in the intended sequence, or extracts
sensible facts from real email content. **Run `docs/SMOKE_TEST_PHASE4.md`
before relying on the context-building scheduled task unattended** — it
has not been run yet as of this handoff.

## Next steps, in order

1. Run `docs/SMOKE_TEST_PHASE4.md` end to end against a real mailbox.
2. Fix whatever it finds (expect at least one surprise on the first real
   run — evidence-grounding and domain-matching rules were only ever
   exercised against synthetic fixtures).
3. Re-run the smoke test until it's clean, then turn on the
   context-building scheduled task per `ops/scheduled-tasks/context-building.md`.
4. Decide and act on the Atlas-vs-local `.env` open question
   (`docs/DECISIONS.md`'s open questions) — rotate the Atlas password
   first if you intend to use it (see "Manual human steps" below).
5. Start Phase 5 (automated golden-set harness) — today's two eval sets
   (`eval/rule_boundary_cases.py`, `eval/label_eval_set.xlsx`, the latter
   gitignored/not tracked) are judgment-graded, not pytest-style
   pass/fail gates. Phase 5 should also add golden cases for departure
   statements specifically (see `docs/PHASE4_DESIGN.md`'s golden-case
   note) — person-org link closure is exactly the kind of judgment call
   that needs measured coverage, not just structural validation.
6. Phase 6/7 after that — neither is scoped yet.

## Known issues

| Issue | Status |
|---|---|
| Chronology fix (auto-close must not close siblings sent after the agent reply, by sort_key) | Deferred, Phase 2. Covered by an `xfail`'d test (`tests/test_claim_fencing_and_chronology.py`) so the suite stays green while the gap is tracked, not silently lost. |
| Run log (persistent record of scheduled-task runs) | Deferred, Phase 2 and reconfirmed in Phase 4's deferred list — doesn't exist for ingestion/labeling either, not a context-building-specific gap. |
| Weekly consolidation pass | Deferred, Phase 4. Profiles already rebuild correctly on every `persist_context_delta` call and via `scripts/rebuild_profiles.py`; a separate scheduled pass was judged unnecessary for correctness, only relevant at scale. |
| Relabel-on-context-arrival / `label_basis` | Deferred, Phase 4. Labeling and context building are deliberately independent this phase; wiring context facts back into label decisions is a labeling-side change, intentionally out of scope. |
| Sender hints (`List-Unsubscribe`/`Precedence`/`Auto-Submitted` headers) | Skipped, Phase 3. The `Email` model captures no header fields to derive hints from — would have been fabricated against a schema that doesn't carry the source data. |
| Test-count discrepancy — **resolved, not open.** | The prior turn's final report said "161 total" for Phase 4A; that was a reporting error (should have said 121). Verified via a `git worktree` checkout of `1a5ff70` and per-file `pytest --collect-only` counts — nothing was ever deleted or lost. See `docs/DECISIONS.md`'s 2026-10-13 log entry. Current true total is 127 (121 Phase 4A + 6 Phase 4B). |

## Manual human steps (none of these are automatable from this session)

- [ ] Re-install `skills/raw-dump-labeling/SKILL.md` AND
      `skills/context-building/SKILL.md` in Claude Desktop — both changed
      this session; Claude Desktop does not hot-reload a skill file from
      disk (see `ops/scheduled-tasks/README.md`).
- [ ] Set Gmail connector permissions per
      `ops/scheduled-tasks/README.md`'s "Enforcement" section — block the
      dangerous tools account-wide; read the section's caveat about which
      restrictions are prompt-level only, not a technical guarantee.
- [ ] **Rotate the Atlas password before using Atlas** — this repo's
      `.env` has, at points this session, held a real Atlas credential;
      treat any previously-used Atlas password as potentially exposed and
      rotate it before pointing this project back at Atlas.
- [ ] Create a local `.env` from `.env.example` (never commit `.env` —
      it's gitignored).
- [ ] Create `eval/private/accounts.json` from whatever example/template
      file this project expects there, if evaluation work needs real
      account data (`eval/private/` is gitignored — nothing under it is
      or should be tracked).
- [ ] Create the six Gmail labels manually once (see
      `ops/scheduled-tasks/README.md`'s one-time setup checklist) before
      any scheduled task runs.

## How to run tests

```
python -m pytest -q          # full suite
python -m pytest --collect-only -q   # count without running
```

Synthetic data only, `mongomock` — no network/Atlas/Gmail/LLM calls.
Current: 127 collected, 126 passed, 1 xfailed (the chronology-fix gap
above), 0 failed.
