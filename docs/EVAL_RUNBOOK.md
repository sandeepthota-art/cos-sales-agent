# Phase 5 eval runbook

How to run the golden-set evaluation against a real Claude Desktop
session. Everything here targets a dedicated eval database -- never the
real one. No network/Atlas/Gmail/LLM calls happen from this repo's own
code during an eval run; the only "model" being measured is Claude
Desktop itself, running the skills exactly as it would in production.

## 1. Point the MCP server at the eval database

Set `MONGODB_DATABASE` (or `SALES_AGENT_MONGODB_DATABASE`, which takes
priority) to a name ending in `_eval` before starting the server, e.g.:

```
MONGODB_DATABASE=raw_dump_eval
```

Every eval script (`scripts/load_golden.py`, `scripts/score_eval.py`)
refuses to run against anything else (`scripts/_common.py::require_eval_database`)
-- if you see a `RuntimeError` naming the database, fix the env var, not
the script.

Restart the MCP server (or your Claude Desktop connector session) after
changing this so it picks up the new database.

## 2. Load the golden cases

```
python scripts/load_golden.py            # dry run -- reports counts only
python scripts/load_golden.py --apply    # actually loads into the eval DB
```

This calls `ingest_raw_email_only` -- the SAME function the real
pipeline uses -- so ingestion hygiene (thread resolution, idempotency,
default field creation) is exercised identically for golden data.
Re-running `--apply` is safe; ingestion is idempotent on `source_message_id`.

## 3. Run the labeling skill in EVAL MODE

Paste a prompt that starts with the literal words `EVAL MODE` into the
Claude Desktop session running `skills/raw-dump-labeling/SKILL.md` --
see that file's "Eval mode" section. In eval mode:

- No Gmail tool is called at all (no `list_labels`, `label_message`,
  `unlabel_message`), and `mark_gmail_label_synced` is never called.
- Only `persist_raw_email_label`/`mark_raw_email_label_failed` run --
  labels are persisted to MongoDB only.
- Otherwise the skill runs its normal Mode B procedure against whatever
  the eval database holds.

Example prompt:

```
EVAL MODE. Run raw-dump-labeling classification (Mode B) against the
eval database. Do not call any Gmail tool. Do not call
mark_gmail_label_synced. Persist labels only. Process until
get_next_unprocessed_raw_email_batch returns empty.
```

## 4. Run the context-building skill in EVAL MODE

Same idea, against `skills/context-building/SKILL.md`. Context building
already never calls a Gmail tool in normal operation, so "eval mode"
here only changes the scope (run until empty, against the eval
database) -- there's no Gmail-skipping behavior to add.

```
EVAL MODE. Run context building against the eval database until
claim_next_thread_for_context returns {"empty": true}.
```

## 5. Record the run

Before scoring, write down:

- **`skill_version`**: the short git commit hash of
  `skills/raw-dump-labeling/SKILL.md` and `skills/context-building/SKILL.md`
  at the time of the run (`git log -1 --format=%h -- skills/`). This is
  what ties a report to the exact skill wording that produced it.
- **`date`**: the date the run happened (`YYYY-MM-DD`).

`scripts/score_eval.py` writes its report to
`eval/reports/<date>_<skill_version>.md` using exactly these two values
-- pass them as `--date` and `--skill-version`.

## 6. Score the run

```
python scripts/score_eval.py --date 2026-10-13 --skill-version a1b2c3d
```

Reads the eval database, compares `label_applied` against each
labeling case's `expected_label`, and the context collections against
each context case's `required_facts`/`forbidden_facts`. Prints the same
report it writes to `eval/reports/<date>_<skill_version>.md`. Never
prints a message body -- only case_ids, counts, and labels/attribute
names.

## 7. Clean up

Golden data lives only in the eval database -- drop it (or let it
accumulate for the next run; re-loading is idempotent) when you're done.
Never point a scheduled task at an eval database, and never point an
eval script at the real one -- the guard in step 1 is there specifically
to catch that mistake.
