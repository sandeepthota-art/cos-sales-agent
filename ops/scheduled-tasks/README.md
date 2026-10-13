# Scheduled tasks

Three real scheduled tasks: `ingestion.md` (Mode A, hourly), `labeling.md`
(Mode B + sync catch-up, hourly), and `context-building.md` (every 2
hours). All three are independent — separate claim fields, separate
tool allowlists, none reads or writes another's state.

## Stagger schedule

Offset each task's start time so no two ever fire in the same minute:

| Task | Cadence | Offset |
|---|---|---|
| Ingestion | hourly | `:00` |
| Labeling | hourly | `:30` |
| Context building | every 2 hours | `:15` |

This is a scheduling convenience, not a correctness requirement — each
task's own claim/lease or compare-and-swap mechanism is what actually
makes concurrent/overlapping runs safe (see each task's own "Concurrency
expectations" section). Staggering just keeps runs from competing for the
same MongoDB connections/CPU at the same instant.

## Installing the context-building skill

`skills/context-building/SKILL.md` is new -- like any `SKILL.md` change,
it must be (re-)installed in Claude Desktop (Settings → Connectors → the
raw-dump-labeling connector → Skills, or wherever this Claude Desktop
version surfaces skill installation) before the context-building
scheduled task's prompt can use it. The same applies any time
`skills/raw-dump-labeling/SKILL.md` or `skills/context-building/SKILL.md`
changes -- Claude Desktop does not hot-reload a skill file from disk.

## One-time setup: create the six Gmail labels manually

Neither scheduled task calls `create_label` anymore (removed
2026-10-13 — see `labeling.md`). Before either task runs for the first
time, create these six labels in Gmail by hand (Settings → Labels →
Create new label), exact text, including the `1. ` prefix:

- [ ] `1. Needs reply: ASAP`
- [ ] `1. Needs reply`
- [ ] `1. Needs reply: mention`
- [ ] `1. Read only`
- [ ] `1. Delete`
- [ ] `1. Undecided`

If a label is later renamed or deleted in Gmail, both tasks will fail
loudly (`list_labels` won't find it) rather than silently recreate or
substitute one — re-run this checklist to fix it.

## Enforcement

Prompt text (in `ingestion.md`/`labeling.md`) is an instruction to
Claude, not a technical guarantee — a model can misread a request, and a
scheduled task runs unattended with no human reviewing each call before
it happens. **The actual enforcement boundary is what the Claude Desktop
connector permissions allow the task to call at all**, not what the
prompt says. Set these to **blocked** in the Gmail connector's permissions
for both scheduled tasks, regardless of what each task's own allowlist
says it needs:

| Tool category | Blocked for |
|---|---|
| Send / reply / forward any email | Both tasks — neither ever needs this |
| Trash / delete a message | Both tasks |
| Mark as spam | Both tasks |
| Create a draft | Both tasks |
| `create_label`, `update_label`, `delete_label` | Both tasks — label-definition CRUD is never used anywhere in this skill (removed 2026-10-13, see `labeling.md`) |
| Any `*_thread` label variant | Both tasks — this skill operates per-message, never per-thread |
| Any `apply_sensitive_*` tool | Both tasks — unrelated to the six-label taxonomy, never used |
| `label_message`, `unlabel_message` | **Ingestion task only, in principle** — see the note below; this is NOT a real technical guarantee today |
| Gmail read/search | **Labeling task only, in principle** — same caveat |

**Names beyond `list_labels`/`label_message`/`unlabel_message` above are
inferred from the Gmail connector's general shape, not individually
confirmed** — confirm the connector's actual full tool list from a live
Claude Desktop session before finalizing permissions.

### What's actually enforceable today

**Blocking the dangerous, never-needed-by-either-task tools account-wide
(send/reply/forward/trash/spam/draft, and all the label-definition/
thread/sensitivity tools in the top half of the table) is effective** —
neither task has any legitimate use for them, so blocking them account-wide
costs nothing and is a real technical guarantee, not just a prompt
convention.

**Preventing ingestion specifically from using `label_message`/
`unlabel_message` is prompt-level only, not a technical guarantee**,
because those two tools MUST stay available account-wide for the labeling
task to function — if the connector permissions UI only has one
permission set per connector (not per scheduled task), there is no way to
block them for ingestion while leaving them usable for labeling. The only
thing stopping ingestion from calling them today is its own prompt text
(`ingestion.md`) saying not to. Same caveat in reverse for Gmail
read/search and the labeling task. If the permissions UI ever supports
per-task scoping, tighten this immediately; until then, this gap is real
and should be treated as an accepted risk, not a solved one.

**Do not treat a correct prompt as sufficient on its own** for the tools
in the top half of the table — block them account-wide, don't rely on the
prompt. For `label_message`/`unlabel_message`/Gmail read, the prompt IS
currently the only enforcement available, which is a materially weaker
guarantee — know the difference.

### The same gap applies across all three scheduled tasks now

The raw-dump-labeling MCP server exposes all 12 tools (8 labeling/
ingestion + 4 context-building) to every connected session — there is no
per-scheduled-task tool scoping at the server or connector level today.
Keeping ingestion off the labeling/context tools, labeling off the
ingestion/context tools, and context-building off the labeling/ingestion
tools (each task's own `ops/scheduled-tasks/*.md` "Forbidden" section and
`skills/*/SKILL.md`'s own-tools-only wording) is **prompt-level only, not
a technical guarantee**, same caveat as the Gmail `label_message`/
`unlabel_message` split above. **What IS a real guarantee**: each tool's
own claim-fencing (`claim_id` vs. `context_claim_id` compare-and-swap) and
field separation (`label_*`/`classification_version` vs. `context_*`) —
even if a task's prompt somehow called the wrong tool, it cannot corrupt
the other lifecycle's state, because the two never share a field or a
claim token. The blast radius of this gap is therefore "a task could call
a tool it has no business calling," never "a task's run could silently
corrupt another task's data."
