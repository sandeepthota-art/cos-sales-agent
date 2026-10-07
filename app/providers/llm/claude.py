import json
import re
from typing import Any

import anthropic

from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider
from app.providers.llm.thread_history import format_person_context, format_thread_history

# Claude sometimes wraps its JSON reply in a markdown code fence (```json ... ``` or
# plain ``` ... ```) even when told "JSON only" -- this pattern strips that fence, if
# present, before parsing. Anchored to the whole (already-stripped) response with
# DOTALL so it only matches when the fence wraps the entire reply, not a fence
# appearing incidentally inside a JSON string value.
_CODE_FENCE_PATTERN = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL | re.IGNORECASE)


def _extract_json(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fence_match = _CODE_FENCE_PATTERN.match(stripped)
    if fence_match:
        stripped = fence_match.group(1).strip()

    # raw_decode (rather than json.loads) parses only the first complete JSON value and
    # ignores anything after it -- Claude occasionally emits a complete, valid JSON object
    # followed by stray trailing text despite "JSON only" instructions (observed live: a
    # short valid object followed immediately by extra content on the next line). loads()
    # would reject that as "Extra data"; raw_decode simply takes the first value, which is
    # the actually-intended answer. Malformed/incomplete JSON (a genuinely broken or
    # truncated object) still raises JSONDecodeError exactly as before -- raw_decode only
    # tolerates trailing content AFTER a complete, valid value, never inside one.
    value, _ = json.JSONDecoder().raw_decode(stripped)
    if not isinstance(value, dict):
        raise json.JSONDecodeError(
            f"expected a JSON object, got {type(value).__name__}", stripped, 0
        )
    return value

_ANALYSIS_INSTRUCTIONS = (
    "You are a sales email analyst. Given the email below, return ONLY a JSON object with keys: "
    "summary, intent, entities, facts (list of {subject,predicate,object}), requirements, pain_points, "
    "buying_signals, objections, competitors, pricing_mentions, commitments, action_items, meetings, "
    "people, companies, products -- entities, requirements, pain_points, buying_signals, objections, "
    "competitors, pricing_mentions, commitments, action_items, meetings, people, companies, and products "
    "are EACH a list of short plain-text strings (e.g. meetings: [\"call scheduled for next week\"]) -- "
    "never objects; use meetings_mentioned below for structured meeting data, "
    "people_mentioned (list of {name, email, org, role_hint} for each person mentioned or corresponding), "
    "projects_mentioned (list of {name, org, objective_hint}), "
    "commitments_mentioned (list of {what, class: one of mine/owed_to_me/theirs/recap, owed_by, owed_to, "
    "date_phrase (the raw text phrase describing when, e.g. 'next Friday' -- never a resolved date), "
    "importance_hint}), "
    "meetings_mentioned (list of {date_phrase, attendees, is_past, actions_raised} objects -- structured "
    "meeting data; the plain-text 'meetings' field above must never contain these objects, only strings). "
    "Only include an entry in meetings_mentioned when the email actually requests, proposes, schedules, "
    "reschedules, or confirms a meeting, or explicitly invites the recipient to one. A mere reference to a "
    "meeting that is already established, already on the calendar, or already happened -- e.g. 'our sprint "
    "review on Friday', 'after the meeting', 'during our call', 'following our meeting', 'as discussed in "
    "yesterday's call' -- is NOT a new meeting proposal and must NOT produce a meetings_mentioned entry, "
    "even though it mentions a meeting/call by name. "
    "Recognize a SALES DEAL OR OPPORTUNITY thread and always emit a projects_mentioned entry for it: this "
    "includes an active negotiation, a pricing/quote discussion, a proposal or contract in progress, a "
    "pilot/POC/trial, a renewal, an expansion, or any other named engagement with a specific prospect or "
    "customer organization -- even if the email never uses the word 'project'. Use the deal/customer/"
    "initiative name as name (e.g. the counterparty's company name, or 'Acme Renewal' if the email itself "
    "frames it that way), the counterparty organization as org, and a short phrase capturing what's being "
    "pursued as objective_hint (e.g. '50-seat expansion', 'Q4 renewal', 'evaluating migration from "
    "Salesforce'). Do not invent a project for an email that is purely informational, internal, or "
    "unrelated to a specific deal -- leave projects_mentioned empty rather than guessing one into "
    "existence. Whenever the email is part of such a sales deal/opportunity, set goal_pillar to exactly "
    "'Sales' (not a paraphrase); goal_pillar and this projects_mentioned entry must agree -- never extract "
    "a projects_mentioned entry for a deal without also setting goal_pillar to 'Sales' for that same "
    "email, and never set goal_pillar to 'Sales' for an email that reflects no genuine deal activity. "
    "STRICT BOUNDARY on facts/requirements/pain_points/objections/buying_signals/action_items/entities "
    "and every other extracted field: these describe the BUSINESS domain only -- relationships between "
    "people and organizations, project timelines, deal terms, and concrete business action items. Never "
    "extract meta-instructions about this AI agent's own behavior, architecture, or configuration (e.g. "
    "\"the AI must write concise emails\", \"the system should create only one draft\", \"labelling should "
    "be performed\") as if they were business facts or requirements. Never extract software engineering "
    "specs -- system schemas, database structures, API/code-level logic -- as knowledge items either. If "
    "the email is itself an internal engineering discussion about this AI system (e.g. meeting minutes "
    "about the agent's own configuration or behavior), summarize that in summary/intent only and leave "
    "facts, requirements, pain_points, and the other business-fact lists empty rather than populating them "
    "with the system's own instructions. "
    "personal_items_mentioned (list of {item_type, description, date_phrase}), "
    "person_facts_mentioned (list of {person_name, person_email, category, value, basis}) -- for a "
    "qualitative statement EXPLICITLY about one named, identifiable person's role, responsibility, "
    "preference, goal, interest, concern, pain point, objection, or buying signal (e.g. 'Ashok is leading "
    "the analytics initiative' -> {person_name: 'Ashok', category: 'responsibility', value: 'leading the "
    "analytics initiative'}; 'Ashok prefers weekly calls on Tuesday' -> {person_name: 'Ashok', category: "
    "'preference', value: 'weekly calls on Tuesday'}; 'Vijender handles procurement' -> {person_name: "
    "'Vijender', category: 'responsibility', value: 'handles procurement'}). person_name must be a real "
    "name the email actually gives (matching or closely corresponding to a people_mentioned entry or the "
    "sender/recipient) -- never a placeholder, role title, or pronoun alone. category is exactly one of: "
    "role, responsibility, preference, goal, interest, concern, pain_point, objection, buying_signal, "
    "other. "
    "ATTRIBUTION IS EVIDENCE-GATED, NEVER AUTOMATIC: only add a person_facts_mentioned entry when the "
    "email's own wording makes that SPECIFIC person the subject of that SPECIFIC statement -- being merely "
    "mentioned, copied, or present in the thread is never enough. Distinguish carefully: a statement about "
    "the SENDER ('I need this by Friday') may be attributed to the sender by name if known; a statement "
    "about ANOTHER named person ('Ashok says...', 'Vijender handles...') may be attributed to THAT person; "
    "a statement about the organization or team as a whole ('procurement is worried about security', 'the "
    "team needs this by Friday') must NOT be attributed to any individual, even one who reported it -- e.g. "
    "'Ashok says the procurement team is worried about security' is a fact about the procurement team/"
    "organization, not a personal concern of Ashok's, and must not be turned into 'Ashok has a security "
    "concern'. If it is unclear whether a statement is about a specific person, the organization, or the "
    "deal in general, leave it OUT of person_facts_mentioned entirely and let it remain in the existing "
    "requirements/pain_points/objections/buying_signals/competitors lists instead (thread-scoped, not lost) "
    "-- never guess an attribution. Never hallucinate a role, title, or preference that the email does not "
    "state or clearly imply; basis is 'stated' for something the email says directly, 'inferred' only for a "
    "reasonable, directly-supported inference (never a speculation). "
    "goal_pillar (a short label for which business goal this relates to, e.g. 'Sales'), "
    "label_applied -- exactly one of: "
    "'1. Needs reply: ASAP' (he must respond, and it is time-critical or from a key relationship), "
    "'1. Needs reply' (he must respond, but it is not urgent), "
    "'1. Needs reply: mention' (a thread he was only reading now asks him something by name), "
    "'1. Read only' (informational; nothing is being asked of him), "
    "'1. Delete' (meeting accept/decline notices, cold outreach with no prior relationship, or any email "
    "from tiplus.prod@kotak.com -- always Delete regardless of content), "
    "'1. Undecided' (you cannot place it with confidence). "
    "Do not invent or assign any canonical entity ID yourself; only describe what you observe in the email. "
    "Entity ID assignment is handled separately by the system. "
    "Use empty lists/strings for anything not present. No prose, JSON only."
)

_UPDATE_CONTEXT_INSTRUCTIONS = (
    "You are updating a sales thread's context with what ONE new email adds. The previous_context you "
    "are given is the full accumulated context so far, for reference only -- do NOT reproduce it. "
    "Return ONLY a bounded DELTA describing what THIS email changes, never the complete context. "
    "Omit any field below that this email doesn't affect; an omitted field, or empty added/removed "
    "lists, means 'no change' -- everything already in the context stays exactly as it was, you do not "
    "need to (and must not) repeat it back. "
    "Do not wrap the delta in prose or markdown explanation -- JSON only, matching this shape: "
    "summary: a replacement string, only if this email changes the running summary, otherwise omit; "
    "participants_added / participants_removed: plain lists of strings (names or email addresses) -- "
    "never objects, never wrapped in value/basis/source_email_ids; "
    "company_updates / opportunity_updates / pricing_updates: plain {key: value} objects for keys this "
    "email adds or changes, with a key set to null meaning that key should be removed -- these three are "
    "flat dictionaries, never lists; "
    "for each of requirements, pain_points, products_discussed, competitors, objections, buying_signals, "
    "decisions, commitments, open_questions, next_actions, meetings: an object "
    "{\"added\": [{\"value\": ..., \"basis\": \"stated\"|\"inferred\"}], \"removed\": [\"exact prior "
    "value to invalidate\", ...]} -- only include entries this email actually adds or invalidates; never "
    "include a value already present that is still true, and never omit a value that is genuinely new. "
    "basis is 'stated' for customer-stated facts and 'inferred' for your own inferences. "
    "The commitments and meetings deltas must stay synchronized with new_analysis.commitments_mentioned and "
    "new_analysis.meetings_mentioned: if this email's analysis contains a commitment or meeting mention that "
    "isn't already reflected in previous_context, add a corresponding entry (a short plain-text description "
    "of it) to the commitments or meetings delta -- never leave commitments/meetings empty when "
    "commitments_mentioned/meetings_mentioned contains something new."
)


class ClaudeProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model

    def _complete_json(self, system: str, user: str) -> dict[str, Any]:
        # thinking={"type": "disabled"}: confirmed via a live diagnostic call that this
        # model enables extended thinking by default even when the request never asks
        # for it, and thinking tokens count against max_tokens -- with max_tokens=1024,
        # the model spent all 1024 (or all but a few) tokens on an internal ThinkingBlock,
        # leaving the actual JSON answer empty or cut off mid-string (JSONDecodeError on
        # every call). These four calls (analyze_email, update_context, verify_same_fact,
        # draft_reply) are deterministic structured-output extraction, not open-ended
        # reasoning, so thinking has no benefit here -- disabling it removed the failure
        # entirely (thinking_tokens: 0, stop_reason: end_turn, full valid JSON) in the same
        # diagnostic call. max_tokens is raised to 4096 as headroom for longer real emails/
        # threads with more facts and entities than the short sample used to diagnose this,
        # now that the budget isn't being consumed by thinking.
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            system=system,
            thinking={"type": "disabled"},
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(block.text for block in response.content if hasattr(block, "text"))
        return _extract_json(text)

    def analyze_email(
        self, email: Email, thread_history: list[dict[str, Any]] | None = None,
        person_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._complete_json(
            _ANALYSIS_INSTRUCTIONS,
            f"{format_person_context(person_context)}{format_thread_history(thread_history)}"
            f"Subject: {email.subject}\n\nBody:\n{email.body}",
        )
        result.setdefault("email_id", email.message_id)
        return result

    def update_context(self, previous_context: dict[str, Any], new_analysis: dict[str, Any]) -> dict[str, Any]:
        user = json.dumps({"previous_context": previous_context, "new_analysis": new_analysis})
        return self._complete_json(_UPDATE_CONTEXT_INSTRUCTIONS, user)

    def verify_same_fact(self, existing_value: str, new_value: str, subject: str, predicate: str) -> bool:
        instructions = "Answer ONLY with JSON: {\"same_fact\": true} or {\"same_fact\": false}."
        user = (
            f"Subject: {subject}\nPredicate: {predicate}\nExisting value: {existing_value}\n"
            f"New value: {new_value}\nAre these describing the same underlying fact?"
        )
        result = self._complete_json(instructions, user)
        return bool(result.get("same_fact", False))

    def draft_reply(self, context: dict[str, Any], latest_email: Email) -> dict[str, Any]:
        instructions = "Draft a professional sales reply. Return ONLY JSON: {\"subject\": ..., \"body\": ...}."
        user = json.dumps(
            {
                "context": context,
                "latest_email_subject": latest_email.subject,
                "latest_email_body": latest_email.body,
                "sender_name": latest_email.from_.name or latest_email.from_.email,
            }
        )
        return self._complete_json(instructions, user)
