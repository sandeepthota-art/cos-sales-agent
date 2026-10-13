# Phase 4 smoke test (manual, real Gmail + Claude Desktop)

Everything in this repo's automated test suite runs against `mongomock`
with synthetic data. This checklist is the one pass that actually
exercises the context-building skill against real Gmail content and a
real Claude Desktop session — run it once before relying on the
scheduled task unattended.

Never paste real email content into a bug report, chat, or commit while
running this -- note message_id/thread_id/field names only.

## Setup

- [ ] `skills/context-building/SKILL.md` is installed in Claude Desktop
      (see `ops/scheduled-tasks/README.md`'s "Installing the
      context-building skill").
- [ ] Ingest roughly 10 recent real emails spanning 3-4 distinct threads
      (Mode A, explicit dump — see `skills/raw-dump-labeling/SKILL.md`).
      Mixing a couple of threads with a role-address participant
      (`support@`, `noreply@`, etc.) and a couple with a free-mail
      participant (`gmail.com`, `outlook.com`, etc.) gives the checklist
      below something real to check.

## Run

- [ ] Run the context-building skill ONCE with a per-run cap of **3
      threads** (override the default 5 for this smoke test, so the run
      is small enough to review by hand).
- [ ] Capture the end-of-run report (threads processed, validation
      retries, `mark_context_failed` calls, `get_stuck_context` result).

## Verify

- [ ] **Evidence quotes are real.** For a sample of the `observations`
      written this run, open the source email by `source_message_id` and
      confirm the `evidence` string is an exact (whitespace/case aside)
      substring of that email's actual subject/body — not a paraphrase,
      not something from a different message.
- [ ] **Role-address people docs carry no attributes.** A role address
      (`support@`, `noreply@`, etc.) that appeared as a participant gets a
      `people` document (`is_role_address: true`), but its `profile` is
      `{}` — it was never given a title/department/etc. This is expected
      Phase 4B behavior (participation alone creates the doc; the
      attribute whitelist still rejects any fact about it), not a bug —
      don't expect the document to be MISSING.
- [ ] **Free-mail domains never get an organization.** No `organizations`
      document exists keyed by a free-mail domain (gmail.com, outlook.com,
      etc.), even though the free-mail ADDRESS itself has a `people` doc.
- [ ] **No sensitive attributes anywhere.** Spot-check a few `people`/
      `organizations` `profile` dicts — every key is one of the whitelisted
      attribute names in `app/context_config.py` (including the new
      `"name"` attribute for organizations). Nothing resembling health,
      religion, politics, ethnicity, sexual orientation, finances, or
      credentials.
- [ ] **Thread status is plausible.** For each processed thread, does the
      `threads.status` value (active/waiting_on_us/waiting_on_them/
      resolved/unknown) match what you'd say reading the thread yourself?
      This is a judgment check, not a pass/fail assertion — flag anything
      that looks clearly wrong.
- [ ] **`get_stuck_context` is empty.** If it isn't, read the reported
      `context_last_error` for the stuck message_ids before continuing.

## Re-run idempotency

- [ ] Run the context-building skill a SECOND time immediately
      afterward, same cap. Confirm:
  - It reports "nothing to process" / claims nothing new (the threads
    from the first run are already `context_version=1` and not
    `needs_rebuild`).
  - No new `observations` were inserted (same count as after the first
    run).
  - No `people`/`organizations` `message_count` changed from the first
    run's values.

## If anything here fails

Don't try to patch it live against real data. Note the exact message_id/
thread_id/field involved (never the content itself), stop the scheduled
task, and fix it against a synthetic `mongomock` test first — the same
discipline every other test in this repo already follows.
