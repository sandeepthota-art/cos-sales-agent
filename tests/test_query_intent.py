# tests/test_query_intent.py
"""Phase 22A: deterministic Stage-1 intent classification and the Stage-2
LLM-output validation boundary. No LLM call happens anywhere in this file."""
import pytest

from app.query.intent import IntentParseError, classify_intent, parse_llm_intent_output
from app.query.schemas import DateRangeKind, ParsedIntent, QueryIntentType

# One test per example question from the Phase 22A task, proving Stage 1 needs no
# LLM to handle the whole example set.
_EXAMPLES: list[tuple[str, QueryIntentType]] = [
    ("What sales meetings do I have today?", QueryIntentType.MEETINGS),
    ("What finance meetings do I have today?", QueryIntentType.MEETINGS),
    ("Who am I waiting on?", QueryIntentType.COMMITMENTS),
    ("Who owes me something?", QueryIntentType.COMMITMENTS),
    ("What commitments did I make?", QueryIntentType.COMMITMENTS),
    ("What follow-ups are overdue?", QueryIntentType.FOLLOW_UPS),
    ("What changed with this customer?", QueryIntentType.CROSS_ENTITY_ACTIVITY),
    ("What happened with this project?", QueryIntentType.CROSS_ENTITY_ACTIVITY),
    ("Give me the context on this person.", QueryIntentType.PERSON_CONTEXT),
    ("Give me the context on this company.", QueryIntentType.ORGANIZATION_CONTEXT),
    ("Prepare me for my meeting with this person.", QueryIntentType.MEETING_PREPARATION),
    ("What sales activity happened this week?", QueryIntentType.CROSS_ENTITY_ACTIVITY),
    ("Which prospects need follow-up?", QueryIntentType.FOLLOW_UPS),
    ("What are my open commitments?", QueryIntentType.COMMITMENTS),
    ("What meetings are coming up?", QueryIntentType.MEETINGS),
    ("What did this person last ask me to do?", QueryIntentType.COMMITMENTS),
    ("What do I need to respond to?", QueryIntentType.REPLY_DRAFTS),
    ("What happened in the latest thread?", QueryIntentType.THREAD_CONTEXT),
    ("What are the unresolved items associated with this project?", QueryIntentType.PROJECT_CONTEXT),
]


@pytest.mark.parametrize("text,expected_intent", _EXAMPLES)
def test_classify_intent_covers_the_task_example_questions(text, expected_intent):
    parsed = classify_intent(text)
    assert parsed.intent == expected_intent


def test_classify_intent_unrecognized_text_is_unsupported_not_guessed():
    parsed = classify_intent("asdkjfh qwoieury zzz nonsense")
    assert parsed.intent == QueryIntentType.UNSUPPORTED
    assert parsed.confidence == 0.0


def test_classify_intent_resolves_date_keywords():
    assert classify_intent("meetings today").date_range_kind == DateRangeKind.TODAY
    assert classify_intent("meetings tomorrow").date_range_kind == DateRangeKind.TOMORROW
    assert classify_intent("commitments this week").date_range_kind == DateRangeKind.THIS_WEEK
    assert classify_intent("follow-ups overdue").date_range_kind == DateRangeKind.OVERDUE
    assert classify_intent("meetings coming up").date_range_kind == DateRangeKind.UPCOMING


def test_classify_intent_extracts_a_named_person_mention():
    parsed = classify_intent("Give me the context on Ashok Ganapam.")
    assert parsed.intent == QueryIntentType.PERSON_CONTEXT
    assert parsed.person_text == "Ashok Ganapam"


def test_classify_intent_extracts_a_named_person_via_who_is():
    parsed = classify_intent("Who is Ash?")
    assert parsed.intent == QueryIntentType.PERSON_CONTEXT
    assert parsed.person_text == "Ash"


def test_classify_intent_extracts_a_named_organization_mention():
    parsed = classify_intent("Give me the company context on DataBeat.")
    assert parsed.intent == QueryIntentType.ORGANIZATION_CONTEXT
    assert parsed.org_text == "DataBeat"


def test_classify_intent_extracts_person_after_meeting_preparation_filler():
    # "for my meeting" must not itself be misread as the person mention.
    parsed = classify_intent("Prepare me for my meeting with Ashok Ganapam.")
    assert parsed.intent == QueryIntentType.MEETING_PREPARATION
    assert parsed.person_text == "Ashok Ganapam"


def test_classify_intent_pronoun_reference_yields_no_extractable_mention():
    parsed = classify_intent("Prepare me for my meeting with this person.")
    assert parsed.person_text is None


# --- Stage 2: LLM output validation (no live call; validates a raw dict as if it
# had come from an LLM) ------------------------------------------------------------------


def test_parse_llm_intent_output_accepts_a_well_formed_payload():
    parsed = parse_llm_intent_output({"intent": "commitments", "person_text": "Ashok Ganapam", "confidence": 0.9})
    assert isinstance(parsed, ParsedIntent)
    assert parsed.intent == QueryIntentType.COMMITMENTS


def test_parse_llm_intent_output_rejects_an_intent_outside_the_vocabulary():
    with pytest.raises(IntentParseError):
        parse_llm_intent_output({"intent": "delete_all_records"})


def test_parse_llm_intent_output_rejects_malformed_shape():
    with pytest.raises(IntentParseError):
        parse_llm_intent_output({"not_intent_at_all": True})


def test_parse_llm_intent_output_rejects_a_raw_mongo_query_masquerading_as_intent():
    # The LLM must never be able to smuggle an arbitrary Mongo filter through --
    # this key simply isn't part of the schema, so pydantic ignores it silently by
    # default; the important guarantee is that the RESULT is still a valid,
    # narrow ParsedIntent and nothing resembling a Mongo filter is ever executed.
    parsed = parse_llm_intent_output({"intent": "meetings", "mongo_filter": {"$where": "this.password"}})
    assert not hasattr(parsed, "mongo_filter")
    assert parsed.intent == QueryIntentType.MEETINGS
