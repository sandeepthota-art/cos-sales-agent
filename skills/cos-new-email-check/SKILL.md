---
name: "cos-new-email-check"
description: "Use when the user asks about new emails (e.g. \"what new email did I get\", \"check my inbox\", \"anything new from X\") — processes them via the cos-sales-agent granular tools, never process_email."
---

# Checking for new email via CoS Sales Agent (granular pipeline)

Trigger phrases: "what new email did I get", "check my inbox", "anything new from X", or similar requests to check for new mail.

## Hard rules

- Only ever use the `cos-sales-agent` connector's tools (`mcp__remote-devices__cos-sales-agent__*`). No other MCP connector is used for this skill's CoS processing job.
- Never call `process_email`. Always use the granular Phase 1 tools instead: `ingest_email` → `persist_email_analysis` → `persist_context_delta` → (`create_reply_draft` if warranted, or `set_reply_withheld_reason` if a reply is needed but deliberately not drafted) → `mark_email_completed`.
- Never send, reply (dispatch), forward, delete, trash, or mark spam.
  The two Gmail write actions ever permitted are *labeling* a message
  (step 4 below, via `create_label`/`label_message`) and *creating a
  draft* (via `create_draft`), only immediately after `create_reply_draft`
  succeeds in step 5 below -- never a send/reply/forward call, and a
  draft must never be sent automatically by this skill.
- Never create a calendar event.
- Only touch one email at a time unless the user explicitly asks for more than one.
- Never invent people, commitments, meetings, projects, or reply content that isn't genuinely present in the email. If nothing warrants a reply, don't fabricate one.
- Never call Claude, OpenAI, or any other LLM API for classification — you are the classifier, using your own reading of the email. Do not expose your reasoning/chain-of-thought in any tool call or stored data — only the resulting structured fields.
- **Before creating any Project or Opportunity, check for an existing one with the same name/entity first** (e.g. via `get_project_summary` / `list_projects` / `list_opportunities`). Reuse it rather than creating a new one just because this message's own `MentionedProject` didn't repeat the `org`/entity value an earlier message already supplied — a thread that already has a Project/Opportunity should almost never get a second one from a later message in the same thread.

## Procedure

1. **Search Gmail** for the message(s) the user is asking about (recent inbox, a sender, a subject — whatever matches their request).
   - If the search surfaces a thread rather than a single message, **open it (`get_thread`) and enumerate its actual `messages[]`** — never treat a thread's own `id` field as a message id (Gmail sets a thread's id equal to the id of the message that started it, so doing this silently resolves to the OLDEST message, never a later one). Every distinct Gmail `message_id` is its own separate candidate, regardless of shared thread/subject/sender.
   - Check each candidate against this connector's read-only lookups (`search_emails` / `list_processed_emails`) to see which are actually unprocessed before deciding what's "new" — a thread with one already-`COMPLETED` message and one new reply means exactly one candidate here, not zero.
2. **For each candidate message, call `ingest_email`** (maps Gmail id→message_id, threadId→thread_id, sender/recipients→from/to/cc, date→timestamp; include in_reply_to/references if available).
   - If the response has `already_completed: true`, **stop for that email** — do not reprocess it, do not call any of the persist_* tools for it. Just note that it was already handled.
   - If it's new, read `previous_context` (if any) before reasoning about it.
3. **For genuinely new emails, read the email yourself and determine Sales vs Not Sales before producing the analysis:**
   - Classify as **Sales** (`goal_pillar: "Sales"`) only when the email shows genuine activity tied to a specific prospect/customer organization: an active sales negotiation, pricing or quote discussion, a proposal discussion or submission, contract discussion/negotiation, a pilot/POC/trial, a renewal, an expansion or upsell, a purchase/order discussion, a specific customer/prospect engagement or deal, or other concrete evidence of an actual sales opportunity.
   - Do **NOT** classify as Sales merely because the email mentions a company, mentions a product, mentions revenue or business, contains generic marketing content, is a newsletter, is purely informational, is an internal discussion unrelated to a specific deal, mentions a potential customer with no actual deal activity, or uses generic sales terminology without evidence of a genuine opportunity.
   - "Sales" means the user's OWN pipeline — a deal their business is running with a prospect/customer. An unsolicited email FROM an outside vendor pitching a product, service, or proposal TO the user is not Sales, no matter how deal-shaped it reads (pricing, phased implementation, signature/kickoff language, etc.) — that is inbound solicitation, not the user's sales activity.
   - If Sales: set `goal_pillar: "Sales"`, and populate `projects_mentioned` only when there's enough evidence to identify the actual engagement/project (never invent one).
   - If not Sales: leave `goal_pillar: ""` and do not add a `projects_mentioned` entry.
   - Produce the full `EmailAnalysis` (people_mentioned, projects_mentioned, commitments_mentioned, meetings_mentioned, personal_items_mentioned, goal_pillar, label_applied, etc.) reflecting only what's actually in the email — then call `persist_email_analysis`.
   - A bounded `ContextDelta` (only what this email changes, never a full rewrite of context) — then call `persist_context_delta`.
4. **Apply the matching Gmail label.** Call the Gmail connector's `list_labels`; if the label name matching this email's own `label_applied` value (exactly one of `Needs reply: ASAP`, `Needs reply`, `Needs reply: mention`, `Read only`, `Delete`, `Undecided`) isn't already present, call `create_label` with that exact string as `displayName` first. Then call `label_message` with this email's real Gmail id (never the canonical `EML-nnn`) and that label's id.
5. **If the email genuinely needs a reply** (a real question, request, or open item directed at the user), first check whether drafting one is actually appropriate: phishing/spoofing red flags (a sender domain that doesn't match the identity/company they claim, fabricated or implausible contact details, unfilled template placeholders like `[Product/Service Name]`, `[Target Date]` still present in the body), a request for sensitive data (bank details, credentials, passwords, government IDs), or any other reason a drafted reply would be unsafe or premature. If any apply, never draft a committal reply (agreeing to next steps, a call, signature, or kickoff) — either skip the draft or keep it strictly non-committal, call `set_reply_withheld_reason` with the message_id and a concise, specific reason, and always flag the suspicion in your report (step 7). Otherwise, draft a reasonable reply and call `create_reply_draft`. If no reply is warranted, skip this step entirely — don't create a draft just to have one.
   - **Immediately after `create_reply_draft` succeeds**, also create a
     matching draft directly in the user's own Gmail mailbox via the Gmail
     connector's `create_draft` tool, using the exact same subject and
     body, addressed as a reply within the original thread (use the
     thread/message identifiers so it nests correctly under the original
     conversation). This is for the user to open, edit, and send himself
     from Gmail — never sent automatically by this skill.
6. **Call `mark_email_completed`** for the message once analysis has been persisted.
7. **Report back in plain English** — what came in, who it's from, what it's about, whether it was classified as a sales opportunity, what (if anything) was created (people, commitments, follow-ups, meetings, a reply draft and whether a matching Gmail draft was also created), the Gmail label applied, whether a reply was needed but withheld (and why), whether it showed any phishing/spoofing red flags, and what needs the user's attention. Do not dump raw JSON/tool output.

## Verifying "is this really new"

When in doubt before ingesting, it's fine to sanity-check via the read-only tools first (`get_thread`, `search_emails`, or scanning `list_processed_emails`) — but `ingest_email`'s own `already_completed` flag is the authoritative duplicate check and must always be honored before persisting anything further.
