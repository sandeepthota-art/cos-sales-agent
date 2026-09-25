"""Phase 22A: Stage-1 deterministic intent classification, plus the Stage-2
LLM-output validation boundary (Section N).

Stage 1 (classify_intent) is a plain regex/keyword classifier -- the SAME style
app.providers.llm.mock.MockLLMProvider already uses for email analysis -- and is
the architecture's primary, always-available path: it needs no LLM call at all, so
the core retrieval layer (app.query.service) is fully testable without one (Section
AH). It is not a full NLU system and does not claim to be; ambiguous or unrecognized
phrasing classifies as UNSUPPORTED rather than guessing.

Stage 2 (parse_llm_intent_output) exists for a FUTURE LLM-assisted classifier: if one
is ever wired in (Phase 22B+), its raw output must pass through this exact function,
which validates it against the same ParsedIntent schema Stage 1 produces and rejects
anything malformed or naming an intent outside QueryIntentType -- the LLM never
returns a MongoDB query, only a value of this fixed, checked shape. No live LLM call
happens anywhere in this module.
"""

import re
from typing import Any

from pydantic import ValidationError

from app.query.schemas import DateRangeKind, OwnershipDirection, ParsedIntent, QueryIntentType

# Phase 24.1: fixed-phrase matching, identical in kind to the date-keyword table
# below -- never an inference over arbitrary wording. Checked in order; the first
# match wins. Only applied when the classified intent is COMMITMENTS.
_OWNERSHIP_KEYWORDS: list[tuple[re.Pattern, OwnershipDirection]] = [
    (re.compile(r"\bwaiting on\b|\bowes? me\b|\bowed to me\b", re.IGNORECASE), OwnershipDirection.OTHER_PERSON_OWES),
    (re.compile(r"\bwhat do i owe\b|\bi owe\b|\bmy commitments?\b|\bcommitments? did i make\b|\bi committed\b", re.IGNORECASE), OwnershipDirection.USER_OWES),
]

_DATE_KEYWORDS: list[tuple[re.Pattern, DateRangeKind]] = [
    (re.compile(r"\btoday\b", re.IGNORECASE), DateRangeKind.TODAY),
    (re.compile(r"\btomorrow\b", re.IGNORECASE), DateRangeKind.TOMORROW),
    (re.compile(r"\byesterday\b", re.IGNORECASE), DateRangeKind.YESTERDAY),
    (re.compile(r"\bnext week\b", re.IGNORECASE), DateRangeKind.NEXT_WEEK),
    (re.compile(r"\blast week\b", re.IGNORECASE), DateRangeKind.LAST_WEEK),
    (re.compile(r"\bthis week\b", re.IGNORECASE), DateRangeKind.THIS_WEEK),
    (re.compile(r"\bnext month\b", re.IGNORECASE), DateRangeKind.NEXT_MONTH),
    (re.compile(r"\bthis month\b", re.IGNORECASE), DateRangeKind.THIS_MONTH),
    (re.compile(r"\bupcoming\b|\bcoming up\b", re.IGNORECASE), DateRangeKind.UPCOMING),
    (re.compile(r"\boverdue\b", re.IGNORECASE), DateRangeKind.OVERDUE),
    (re.compile(r"\brecent(?:ly)?\b", re.IGNORECASE), DateRangeKind.RECENT),
    (re.compile(r"\blatest\b|\blast\b", re.IGNORECASE), DateRangeKind.LATEST),
]

# Checked in order -- first match wins. More specific phrasings (meeting prep,
# activity/change questions, explicit "context on") are checked before the more
# generic keywords ("meeting", "project") they'd otherwise also match.
_INTENT_RULES: list[tuple[re.Pattern, QueryIntentType]] = [
    (re.compile(r"\bprepare\b.*\bmeeting\b|\bmeeting prep", re.IGNORECASE), QueryIntentType.MEETING_PREPARATION),
    (
        # Checked before the more generic activity rule below -- "email activity"/
        # "recent emails" specifically means the emails collection (Phase 22B.3).
        re.compile(r"\bemail activity\b|\brecent emails?\b|\blatest emails?\b|\bemails? (?:with|from|about)\b", re.IGNORECASE),
        QueryIntentType.EMAIL_ACTIVITY,
    ),
    (
        re.compile(r"\bwhat (?:changed|happened)\b.*\b(with|to)\b|\bsales activity\b|\bactivity\b", re.IGNORECASE),
        QueryIntentType.CROSS_ENTITY_ACTIVITY,
    ),
    (
        re.compile(
            r"\bwaiting on\b|\bowes? me\b|\bowed to me\b|\bcommitments?\b|\bcommit(?:ted)\b|\bwhat did .* ask me\b"
            r"|\bwhat do i owe\b|\bi owe\b",
            re.IGNORECASE,
        ),
        QueryIntentType.COMMITMENTS,
    ),
    (re.compile(r"\bfollow[\s-]?ups?\b|\bneeds? follow", re.IGNORECASE), QueryIntentType.FOLLOW_UPS),
    (
        re.compile(r"\bcontext on\b.*\b(company|organization|customer)\b|\bcompany context\b", re.IGNORECASE),
        QueryIntentType.ORGANIZATION_CONTEXT,
    ),
    (
        # Reached only when the organization-specific rule above did NOT match --
        # "context on <anything>" with no company/organization/customer keyword
        # defaults to PERSON_CONTEXT, so a bare name ("context on Ashok Ganapam")
        # resolves without requiring the literal word "person" in the question.
        re.compile(r"\bcontext on\b|\bwho is\b|\bperson context\b", re.IGNORECASE),
        QueryIntentType.PERSON_CONTEXT,
    ),
    (re.compile(r"\bproject\b", re.IGNORECASE), QueryIntentType.PROJECT_CONTEXT),
    (re.compile(r"\bthread\b", re.IGNORECASE), QueryIntentType.THREAD_CONTEXT),
    (re.compile(r"\brespond to\b|\breply draft|\bdrafts?\b", re.IGNORECASE), QueryIntentType.REPLY_DRAFTS),
    (re.compile(r"\bknowledge\b|\bwhat do (?:we|I) know\b", re.IGNORECASE), QueryIntentType.KNOWLEDGE),
    (re.compile(r"\bmeetings?\b", re.IGNORECASE), QueryIntentType.MEETINGS),
    (re.compile(r"\bcompany\b|\borganization\b|\bcustomer\b", re.IGNORECASE), QueryIntentType.ORGANIZATION_CONTEXT),
    (re.compile(r"\bperson\b|\bcontact\b", re.IGNORECASE), QueryIntentType.PERSON_CONTEXT),
]

# Matches "with X", "for X", "about X", "on X" up to the next clause boundary --
# deliberately simple substring extraction, not NER. A bare pronoun-like reference
# ("this person", "this company", "this project") is recognized but yields no
# extractable name; a caller with an actual pre-resolved entity in hand should pass
# it via QueryRequest.filters directly rather than relying on text extraction.
_MENTION_PATTERN = re.compile(
    r"\b(?:with|for|about|on|who(?:'s| is))\s+((?!this\b)[A-Za-z][\w' .-]*?)"
    r"(?=\s*[?.!]|\s+(?:today|tomorrow|yesterday|this|next|last|with|for|about|on)\b|$)",
    re.IGNORECASE,
)
# A match starting with a determiner/possessive ("my meeting", "the deal") is not a
# name -- e.g. "prepare me FOR MY MEETING with this person" would otherwise extract
# "my meeting with" as a bogus person mention from the "for" trigger.
_NON_NAME_LEAD_WORDS = {"my", "the", "a", "an", "our", "your", "their", "his", "her"}


def _extract_mention(text: str) -> str | None:
    # finditer, not search: the FIRST preposition match isn't always a name ("for my
    # meeting" before "with this person") -- try each match in order and return the
    # first one that survives the non-name-lead-word check, rather than giving up
    # after a single rejected candidate.
    for match in _MENTION_PATTERN.finditer(text):
        candidate = match.group(1).strip()
        if candidate and candidate.split()[0].lower() not in _NON_NAME_LEAD_WORDS:
            return candidate
    return None


def classify_intent(text: str) -> ParsedIntent:
    """Deterministic, regex-based -- no LLM call. Returns UNSUPPORTED (never a
    guessed intent) when nothing recognizable matches.
    """
    date_range_kind = next((kind for pattern, kind in _DATE_KEYWORDS if pattern.search(text)), None)
    intent = next((intent for pattern, intent in _INTENT_RULES if pattern.search(text)), QueryIntentType.UNSUPPORTED)
    mention = _extract_mention(text)

    person_text = org_text = project_text = None
    if mention:
        if intent == QueryIntentType.ORGANIZATION_CONTEXT:
            org_text = mention
        elif intent == QueryIntentType.PROJECT_CONTEXT:
            project_text = mention
        else:
            person_text = mention

    ownership_hint = None
    if intent == QueryIntentType.COMMITMENTS:
        ownership_hint = next((direction for pattern, direction in _OWNERSHIP_KEYWORDS if pattern.search(text)), None)

    return ParsedIntent(
        intent=intent, person_text=person_text, org_text=org_text, project_text=project_text,
        date_range_kind=date_range_kind, ownership_hint=ownership_hint,
        confidence=1.0 if intent != QueryIntentType.UNSUPPORTED else 0.0,
    )


class IntentParseError(Exception):
    """Raised by parse_llm_intent_output when an (untrusted) LLM's output does not
    conform to ParsedIntent -- the caller must treat this as "no usable intent",
    never fall back to executing something LLM-supplied directly against MongoDB."""


def parse_llm_intent_output(raw: dict[str, Any]) -> ParsedIntent:
    """Validates a Stage-2 LLM's raw output (already-parsed JSON) against the exact
    same ParsedIntent schema Stage 1 produces. Never returns a MongoDB filter --
    the LLM's only allowed output shape is this typed intent, which app.query.service
    then routes through the SAME trusted retrieval builders Stage 1 uses. Raises
    IntentParseError (not ValidationError directly, so callers have one exception
    type to catch) on anything malformed or naming an intent outside QueryIntentType.
    """
    try:
        return ParsedIntent.model_validate(raw)
    except ValidationError as exc:
        raise IntentParseError(f"LLM intent output failed schema validation: {exc}") from exc


def classify_intent_with_fallback(text: str, llm_classify=None) -> ParsedIntent:
    """Phase 22B.2: Stage 2 is invoked ONLY when Stage 1 could not determine an
    intent at all -- never to override or second-guess a Stage-1 classification it
    already trusts. `llm_classify`, if given, is a plain callable (text) -> dict
    (already-parsed JSON, however the caller obtained it -- this function never
    constructs an LLM client itself, so it is trivially testable with a fake
    callable and never makes a real network call on its own). Its output is always
    passed through parse_llm_intent_output; a malformed or invalid-intent response
    is caught and the result stays UNSUPPORTED rather than raising out of query
    classification entirely -- one bad LLM response must not break the query layer.

    Stage 2 can only ever produce a ParsedIntent (a person/org NAME hint at most) --
    it never resolves an identity itself, so it structurally cannot pick between
    ambiguous candidates; that decision still belongs entirely to
    app.query.entity_resolution, exactly as for a Stage-1-classified query.
    """
    stage1 = classify_intent(text)
    if stage1.intent != QueryIntentType.UNSUPPORTED or llm_classify is None:
        return stage1

    try:
        raw = llm_classify(text)
        return parse_llm_intent_output(raw)
    except IntentParseError:
        return stage1
    except Exception:  # noqa: BLE001 -- any provider failure (timeout, bad JSON, ...) must not crash query classification
        return stage1
