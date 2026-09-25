import re
from typing import Any

from rapidfuzz import fuzz

from app.email.models import Email
from app.entities.dates import find_date_phrase
from app.interfaces.llm_provider import LLMProvider

_COMPETITORS = ["Salesforce", "HubSpot", "Microsoft", "Zoho"]
_PAIN_KEYWORDS = ["pricing", "manual", "slow", "integration", "clunky", "expensive"]
_BUYING_SIGNAL_PATTERNS = [
    (re.compile(r"\bpricing\b", re.IGNORECASE), "pricing request"),
    (re.compile(r"\bdemo\b", re.IGNORECASE), "demo request"),
    (re.compile(r"\bproposal\b", re.IGNORECASE), "proposal request"),
    (re.compile(r"\bsecurity review\b", re.IGNORECASE), "security review"),
    (re.compile(r"\bprocurement\b", re.IGNORECASE), "procurement request"),
]
_SEAT_PATTERN = re.compile(r"\b(\d+)\s*(seats?|users?|licen[sc]es?)\b", re.IGNORECASE)
# Step 4 (deduplication.py) only calls this for pairs rapidfuzz's token_sort_ratio already
# scored in the 60-90 "ambiguous band". 40 used to accept almost everything in that band,
# which wrongly merged genuinely distinct facts observed in the demo dataset -- e.g.
# "pricing request" vs "proposal request" and "demo request" vs "procurement request" both
# score 64.52, yet are different buying signals. 80 rejects both of those (a ~15-point
# margin) while still accepting real same-fact paraphrases such as "data migration
# concerns" vs "concerns about data migration" (88.46).
_SAME_FACT_SIMILARITY_THRESHOLD = 80

# Negative lookahead excludes "...will meet" -- that phrasing is a meeting signal, not a
# commitment (spec S5.1.1's explicit "We will meet in 2 weeks" example: meeting detected,
# no commitment). "I will send"/"I'll follow up"/"we will confirm" etc. still match.
_MINE_COMMITMENT_PATTERN = re.compile(r"\b(?:i will|i'll|we will)\b(?!\s+meet\b)", re.IGNORECASE)
_OWED_TO_ME_COMMITMENT_PATTERN = re.compile(r"\bcould you\b|\bcan you\b", re.IGNORECASE)
# Broadened per spec S5.1.1: a meeting doesn't require an explicit invitation -- the noun
# forms "meeting"/"meetings" and the phrase "catch up" must also trigger detection. Note
# "met" (past tense) intentionally does NOT match "meet" -- see
# test_mock_llm_does_not_detect_historical_meeting_mention_as_a_meeting.
_MEETING_LANGUAGE = re.compile(r"\b(meet|meeting|meetings|call|sync|catch up)\b", re.IGNORECASE)

# Real-world false positive (MTG-384): an email merely REFERENCING an already-established
# meeting ("our sprint review on Friday", "after the meeting", "during our call",
# "following our meeting", "we discussed in yesterday's meeting") was being treated as a
# NEW meeting proposal, because it still contains a _MEETING_LANGUAGE trigger word used as
# a plain noun. A meeting should only be proposed when the email actually requests,
# proposes, schedules, reschedules, or confirms one -- not for a backward-looking mention
# of a meeting that's already on the calendar or already happened. This pattern targets
# the specific grammatical shape of a backward reference (a preposition tying the meeting
# noun to something already established -- "during/after/following OUR/THE meeting",
# "yesterday's/today's/last week's meeting", "we discussed in", "as discussed") rather than
# blacklisting arbitrary phrases -- forward-looking proposal language ("let's schedule a
# call", "can we meet", "the meeting is in 10 days") never matches this shape.
_MEETING_REFERENCE_MARKERS = re.compile(
    r"\b(?:during|after|following)\s+(?:our|the|that|this)\s+(?:meeting|meetings|call|sync|catch up)\b"
    r"|\b(?:yesterday|today|last\s+\w+)'?s\s+(?:meeting|meetings|call|sync)\b"
    r"|\bwe\s+discussed\s+in\b"
    r"|\bas\s+discussed\b",
    re.IGNORECASE,
)


class MockLLMProvider(LLMProvider):
    def analyze_email(self, email: Email) -> dict[str, Any]:
        body = email.body

        competitors = [c for c in _COMPETITORS if c.lower() in body.lower()]
        pain_points = [kw for kw in _PAIN_KEYWORDS if kw in body.lower()]
        buying_signals = [label for pattern, label in _BUYING_SIGNAL_PATTERNS if pattern.search(body)]
        requirements = [f"{m.group(1)} seats" for m in _SEAT_PATTERN.finditer(body)]

        facts = []
        if competitors:
            facts.append({"subject": "Customer", "predicate": "uses", "object": competitors[0]})

        intent = "evaluation"
        if buying_signals:
            intent = "buying_signal"
        if "meet" in body.lower() or "call" in body.lower():
            intent = "meeting_request"

        date_phrase = find_date_phrase(body)

        people_mentioned = [
            {
                "name": email.from_.name or email.from_.email,
                "email": email.from_.email,
                "org": None,
                "role_hint": None,
            }
        ]

        commitments_mentioned = []
        if _MINE_COMMITMENT_PATTERN.search(body):
            commitments_mentioned.append(
                {
                    "what": "follow up",
                    "class": "mine",
                    "owed_by": None,
                    "owed_to": None,
                    "date_phrase": date_phrase,
                    "importance_hint": None,
                }
            )
        if _OWED_TO_ME_COMMITMENT_PATTERN.search(body):
            commitments_mentioned.append(
                {
                    "what": "requested action",
                    "class": "owed_to_me",
                    "owed_by": None,
                    "owed_to": None,
                    "date_phrase": date_phrase,
                    "importance_hint": None,
                }
            )

        meetings_mentioned = []
        if _MEETING_LANGUAGE.search(body) and not _MEETING_REFERENCE_MARKERS.search(body):
            meetings_mentioned.append(
                {
                    "date_phrase": date_phrase,
                    "attendees": [],
                    "is_past": False,
                    "actions_raised": [],
                }
            )

        # Plain-string summaries of the SAME structured mentions above, kept in sync so
        # ContextDelta.commitments/meetings (built from these two fields in
        # update_context below) never drifts from what commitments_mentioned/
        # meetings_mentioned already extracted -- see the real observed bug where
        # threads.latest_context.commitments stayed [] even though a canonical
        # Commitment had been resolved from the same email.
        commitments = [c["what"] for c in commitments_mentioned]
        meetings = [
            f"Meeting: {m['date_phrase']}" if m["date_phrase"] else "Meeting proposed"
            for m in meetings_mentioned
        ]

        label_applied = "Needs reply: ASAP" if buying_signals else "Read only"
        # BRD gives no finer-grained P1/P2 rule than "business priority" -- this reuses
        # the SAME buying_signals signal label_applied already keys off, rather than
        # inventing a second, unrelated heuristic. Deterministic, no wall-clock use.
        priority = "P1" if buying_signals else "P2"

        return {
            "email_id": email.message_id,
            "summary": body[:200],
            "intent": intent,
            "entities": [],
            "facts": facts,
            "requirements": requirements,
            "pain_points": [p.capitalize() for p in pain_points],
            "buying_signals": buying_signals,
            "objections": [],
            "competitors": competitors,
            "pricing_mentions": ["pricing"] if "pricing" in body.lower() else [],
            "commitments": commitments,
            "action_items": [],
            "meetings": meetings,
            "people": [],
            "companies": [],
            "products": [],
            "people_mentioned": people_mentioned,
            "projects_mentioned": [],
            "commitments_mentioned": commitments_mentioned,
            "meetings_mentioned": meetings_mentioned,
            "personal_items_mentioned": [],
            "goal_pillar": "Sales",
            "label_applied": label_applied,
            "priority": priority,
            "confidence": 0.8,
        }

    def update_context(self, previous_context: dict[str, Any], new_analysis: dict[str, Any]) -> dict[str, Any]:
        # Returns a bounded ContextDelta (see app.context.models), not the full context --
        # app.context.engine.apply_context_delta does the actual merge. Deduping against
        # what's already present is still done here (rather than left to the merge step)
        # so a value this analysis repeats verbatim is never listed as "added" at all,
        # exactly mirroring what a real LLM is instructed to do.
        def _new_values(field: str, values: list[str]) -> list[str]:
            existing_values = {item["value"] for item in previous_context.get(field, [])}
            return [v for v in values if v not in existing_values]

        def _field_delta(field: str, values: list[str], basis: str) -> dict[str, Any]:
            return {"added": [{"value": v, "basis": basis} for v in _new_values(field, values)]}

        delta: dict[str, Any] = {
            "requirements": _field_delta("requirements", new_analysis.get("requirements", []), "stated"),
            "pain_points": _field_delta("pain_points", new_analysis.get("pain_points", []), "stated"),
            "competitors": _field_delta("competitors", new_analysis.get("competitors", []), "stated"),
            "buying_signals": _field_delta("buying_signals", new_analysis.get("buying_signals", []), "stated"),
            "objections": _field_delta("objections", new_analysis.get("objections", []), "stated"),
            # Kept in sync with analyze_email's commitments_mentioned/meetings_mentioned
            # (via the plain commitments/meetings summary fields it derives them from) --
            # see the real observed bug where a resolved Commitment/Meeting never showed
            # up in threads.latest_context.
            "commitments": _field_delta("commitments", new_analysis.get("commitments", []), "stated"),
            "meetings": _field_delta("meetings", new_analysis.get("meetings", []), "stated"),
        }

        if new_analysis.get("buying_signals"):
            delta["next_actions"] = _field_delta(
                "next_actions", ["Customer shows strong buying intent"], "inferred"
            )

        if new_analysis.get("summary"):
            delta["summary"] = new_analysis["summary"]

        return delta

    def verify_same_fact(self, existing_value: str, new_value: str, subject: str, predicate: str) -> bool:
        return fuzz.token_sort_ratio(existing_value.lower(), new_value.lower()) >= _SAME_FACT_SIMILARITY_THRESHOLD

    def draft_reply(self, context: dict[str, Any], latest_email: Email) -> dict[str, Any]:
        company = context.get("company", {}).get("name", "there")
        greeting_name = latest_email.from_.name or latest_email.from_.email
        # recipient_preferences (app.entities.models.Person.preferences, folded in by
        # app.replies.drafter.draft_reply) -- absent/empty leaves this byte-identical
        # to the prior hardcoded signature, so every pre-existing test is unaffected.
        preferences = context.get("recipient_preferences") or {}
        signature = preferences.get("voice_signature") or "Best regards,\nSales Team"
        body = (
            f"Hi {greeting_name},\n\n"
            f"Thank you for your note regarding {company or 'your evaluation'}. "
            "We appreciate the additional detail and will follow up shortly with the "
            f"information you requested.\n\n{signature}"
        )
        if preferences.get("remove_long_dash"):
            body = body.replace("—", "-").replace("–", "-")
        return {
            "subject": f"Re: {latest_email.subject}",
            "body": body,
        }
