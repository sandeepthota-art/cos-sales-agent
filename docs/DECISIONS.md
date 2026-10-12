# Decisions — raw-dump-labeling-mcp

## Locked decisions (given, not derived)

1. **All LLM work runs in Claude Desktop (scheduled tasks).** There is no LLM API key and no in-repo LLM call. Confirmed consistent with current code: `app/tools.py` and `app/server.py` contain no LLM client, no API key setting (`app/config.py:16-33` has no such field), and `skills/raw-dump-labeling/SKILL.md:282-283` states explicitly: "Never call Claude, OpenAI, or any other LLM API for classification -- you are the classifier."
2. **Three independent schedulers: ingestion, labeling, context building.** Each must be safe to run at any time, in any order, and overlapping runs must not double-process. Today only ingestion and labeling exist in this repo (as Mode A / Mode B of the one skill); context building does not exist yet (see open question below and `docs/ROADMAP.md` Phase 4).
3. **Six labels**, judged from sender intent and visible thread context only, never from word count or sender domain alone: `1. Needs reply: ASAP`, `1. Needs reply`, `1. Needs reply: mention`, `1. Read only`, `1. Delete`, `1. Undecided`. Manual Gmail relabels are not learned (confirmed: no code path reads a Gmail label back into `label_applied`).
4. **Context entities: People, Organizations, Emails, Threads.** People and Organizations are linked. Knowledge comes only from email content. None of People/Organizations/Threads-as-an-entity exist in this repo yet — only raw `Email` documents in `raw_emails_dump` (see `docs/ROADMAP.md` Phase 4).
5. **Storage is MongoDB Atlas.**

## Open discrepancy: locked decision #5 vs. current `.env`

**FACT**, logged here rather than silently fixed (not touched per task scope): this repo's live `.env` currently has `SALES_AGENT_MONGODB_URI`/`SALES_AGENT_MONGODB_DATABASE` (the Atlas connection) commented out at `.env:8-9`, in favor of a local `mongodb://localhost:27017` / `cos_sales` target at `.env:1-2`. This was a deliberate, already-made change for local-only testing done earlier, not an oversight — but it means the repo as configured today does **not** match locked decision #5. This is an open item for the user to resolve (uncomment when ready to point back at Atlas, or formally revise the locked decision) — not something this task changes.

## Decision log

| Date | Decision | Reason |
|---|---|---|
| 2026-10-13 | Phase 0 baseline documented in `docs/` (this file + `ROADMAP.md`, `CARRYOVER.md`, `BASELINE_REPORT.md`) | Establish a documented starting point before any further build work, per the Phase 0 task spec. |
| 2026-10-13 | Added `.gitignore` and `.env.example` (trivial hygiene only, no application code touched) | `.env` contains a real Atlas credential and a real operator email with no `.gitignore` in place — closing that gap before any `git init` happens is a pure safety/hygiene fix, explicitly allowed by the task's STEP 4 scope. |
| 2026-10-13 | Did not uncomment `.env`'s Atlas lines, did not otherwise touch `.env` | Storage target is a decision for the user, not something to silently "fix" alongside hygiene changes — logged as the open discrepancy above instead. |
| 2026-10-13 | User confirmed: keep local MongoDB for now; locked decision #5 ("Storage is MongoDB Atlas") stays as the eventual target, local is a deliberate, acknowledged interim exception, not a revision of the decision. | Explicit user choice during Phase 0 review. Revisit before any shared/production use — local-only is fine for solo dev/testing. |

## Open questions

1. **How does the `raw_emails_dump` flow relate to any future `emails`/context collections?** Today `raw_emails_dump` is the only collection and carries no canonical `EML-`/`THR-` id, no entity links, and no context/knowledge model — it is intentionally a flat, Gmail-id-keyed dump plus a label. The locked decisions describe a richer context model (People/Organizations/Emails/Threads, linked) that does not exist yet. Open: when Phase 4 context-building work starts, does it (a) read `raw_emails_dump` directly as its email source, (b) require a separate `emails`/canonicalization step first (mirroring cos-sales-agent's `EML-nnn` reassignment, deliberately NOT done here today per `app/models.py:30-35`), or (c) keep the two permanently separate the way cos-sales-agent's own `raw_emails_dump` ended up abandoned/dead on its `ingestion-dump` branch? Needs a decision before Phase 4 starts; not decided by this task.
2. **The Atlas-vs-local `.env` discrepancy above** needs the user's decision: uncomment now, or formally change the locked decision.
3. **Whether cos-sales-agent's `app/email/threading.py` fallback thread-matching (carry-over item #4 in `docs/CARRYOVER.md`) should be imported before or after Phase 3's input-hygiene work** — it's a real gap (no `thread_id` means a message can never be grouped into its conversation today) but isn't blocking Phase 0/1/2.
