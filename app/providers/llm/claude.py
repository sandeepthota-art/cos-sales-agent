import json
import re
from typing import Any

import anthropic

from app.email.models import Email
from app.interfaces.llm_provider import LLMProvider

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
    "personal_items_mentioned (list of {item_type, description, date_phrase}), "
    "goal_pillar (a short label for which business goal this relates to, e.g. 'Sales'), "
    "label_applied -- exactly one of: "
    "'Needs reply: ASAP' (he must respond, and it is time-critical or from a key relationship), "
    "'Needs reply' (he must respond, but it is not urgent), "
    "'Needs reply: mention' (a thread he was only reading now asks him something by name), "
    "'Read only' (informational; nothing is being asked of him), "
    "'Delete' (meeting accept/decline notices, or cold outreach with no prior relationship), "
    "'Undecided' (you cannot place it with confidence) -- "
    "priority -- exactly one of 'P1' or 'P2', for this message's overall business priority "
    "(no finer-grained rule than that is defined -- use your judgment on business importance), "
    "confidence (0.0-1.0). "
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

    def analyze_email(self, email: Email) -> dict[str, Any]:
        result = self._complete_json(
            _ANALYSIS_INSTRUCTIONS,
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
