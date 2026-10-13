# eval/

## Synthetic, in the test suite (safe to read, commit, and run anywhere)

- `golden/labeling_cases.jsonl`, `golden/context_cases.jsonl` -- Phase 5's
  golden eval sets. Fully synthetic: fictional names, every address on a
  reserved domain (`example.com`/`*.example`/`*.test`/`*.invalid`).
  Validated by `eval/eval_models.py` and `tests/test_eval_golden_sets.py`.
  See `docs/EVAL_RUNBOOK.md` for how to actually run an eval against them.
- `eval_models.py` -- pydantic models + loaders for the two golden files above.
- `rule_boundary_cases.py` -- the original frozen offline benchmark these
  golden cases were converted from (and extended). Kept as-is; still a
  valid, independent reference for `skills/raw-dump-labeling/SKILL.md`'s
  rule boundaries. `run_rule_boundary_eval.py` runs it and writes
  `rule_boundary_eval_results.xlsx` -- synthetic, tracked, safe.
- `generate_eval_sheet.py`, `fill_eval_sheet.py`, `add_blind_test_rows.py`,
  `fill_blind_test_results.py` -- tooling for building/filling an eval
  spreadsheet; synthetic, no real mailbox data committed by these scripts
  themselves.
- `reports/` -- `scripts/score_eval.py`'s output
  (`<date>_<skill_version>.md`). Counts and case_ids only, never message
  content -- safe to commit, but check before sharing externally.

## Local-only, real mailbox data (gitignored, NOT part of the test suite)

These files require a live mailbox and a real Claude Desktop run against
it. They are listed in `.gitignore` and were never committed:

- `label_eval_set.xlsx`
- `golden_set_live_run_results.xlsx`
- `golden_set_live_run_report.md`
- `fill_golden_set_live_run.py`
- `fill_gmail_ids.py`
- `fill_actual_results.py`

If one of these exists on your machine, **never read, move, copy, or
commit it** -- it may contain real email content. Nothing in this repo's
automated pipeline (ingestion, labeling, context building, the Phase 5
eval harness) ever reads these files; they're a separate, manual,
judgment-graded workflow for spot-checking a live mailbox.

`eval/private/` is also gitignored, for anything explicitly marked
private (e.g. a local `accounts.json` with real account details) --
nothing should ever be tracked there either.
