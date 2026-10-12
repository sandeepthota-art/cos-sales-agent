# Testing

Two independent kinds of testing live in this project. They test different
things and run differently -- don't conflate them.

## 1. Engine reliability tests (pytest)

Pure code, no Gmail, no real Mongo (uses `mongomock`). Run anytime:

```
pytest tests/ -v
```

33 tests across three files:

- `tests/test_core_engine_reliability.py` (22 tests) -- claim atomicity
  (single + batch, no double-hand-out), TTL crash recovery (abandoned
  claims reclaim after `_LABEL_CLAIM_TTL_MINUTES`), the 3-attempt circuit
  breaker, `classification_version` eligibility, the thread close/reopen
  lifecycle (agent reply closes siblings, inbound reply reopens them,
  cross-thread isolation), ingestion idempotency, UTC `sort_key`
  normalization.
- `tests/test_unsynced_labels.py` (7 tests) -- `get_unsynced_labels`
  ordering, limit, exclusion of already-synced docs.
- `tests/test_catch_up_scheduled_workflow.py` (4 tests) -- the 5-page/
  50-document catch-up budget draining a >50 backlog across runs without
  starvation, idempotent re-sync, persistent-failure visibility, and the
  stale-sync-confirmation race fix (a catch-up retry carrying an old
  `label_applied` must never clobber a newer relabel's
  `gmail_label_synced` state).

These close the gap flagged in an earlier review: the core engine (claim/
TTL/circuit-breaker/lifecycle) was previously verified only once, by hand,
live against Atlas, and never captured as a regression test.

## 2. Classification accuracy eval (the six labels)

Two separate eval sets, because they test different things:

### 2a. Offline rule-boundary eval (`eval/rule_boundary_cases.py` +
`eval/run_rule_boundary_eval.py`)

16 frozen synthetic cases, each targeting one specific rule/boundary from
`skills/raw-dump-labeling/SKILL.md`'s six-label section (e.g. "short but
clear purpose is NOT Undecided", "an RSVP with a real ask is NOT Delete",
"an embedded 'ignore previous instructions' directive must not override
the actual content"). No Gmail, no live mailbox -- runs anywhere, anytime:

```
python eval/run_rule_boundary_eval.py
```

Grading is a judgment pass recorded in `GRADED` inside
`run_rule_boundary_eval.py` -- Claude re-reads each case fresh against the
CURRENT `SKILL.md` wording and records a classification + one-line
rationale, before comparing against the case's `design_intent_label`. A
mismatch means either a reasoning slip or a genuine ambiguity in
`SKILL.md`'s wording. Re-grade by heart (re-read `SKILL.md`, re-classify
each case, update `GRADED`) whenever the label rules change -- rerunning
the script alone doesn't re-grade anything, it just re-renders the last
grading pass.

**Last result: 15/16 match.** One flagged, non-circular disagreement:
`ASAP-02` was designed as ASAP but graded as `Needs reply` -- a deal
deadline of "end of week" isn't same-day/time-critical by the rule as
written, and BRD's own time-scoring rubric treats "within 7 days" as a
low score, not enough alone for ASAP without a client-risk/money override.
Flags a real gap: `SKILL.md`'s ASAP rule has no explicit day-count
boundary.

**Output file:** `eval/rule_boundary_eval_results.xlsx` (full path:
`C:\Users\Deepanshu.sonwane\Documents\python\raw-dump-labeling-mcp\eval\rule_boundary_eval_results.xlsx`)
-- one row per case, mismatches highlighted red, with rule targeted,
subject/body, design intent vs. actual, and rationale.

### 2b. Real-mailbox eval sheet (`eval/label_eval_set.xlsx`)

Needs real emails sent to a real mailbox and a live Claude Desktop run
(Mode A then Mode B) -- not automatable from this coding session. See the
"Notes" tab inside the sheet itself for the step-by-step.

**Last result: 14/14 match** (5 senders x 3 varieties, plus blind-test
rows added later to validate a rule fix).

**File location:** `eval/label_eval_set.xlsx` (full path:
`C:\Users\Deepanshu.sonwane\Documents\python\raw-dump-labeling-mcp\eval\label_eval_set.xlsx`)

## What's still not covered

- No automated end-to-end test against a real Gmail connector (can't be --
  Gmail calls only happen inside a live Claude Desktop skill run).
- No BRD-mandated 3-day shadow run against real mailbox volume yet (see
  the last architecture review report).
- The `already_existed` dedup-reporting gap and the missing `runs` log
  collection (BRD Sec 7) are also still open, unrelated to testing per se.
