from app.providers.llm.claude import _ANALYSIS_INSTRUCTIONS, _UPDATE_CONTEXT_INSTRUCTIONS
from app.providers.llm.openai import _UPDATE_CONTEXT_INSTRUCTIONS as openai_update_context_instructions


def test_analysis_instructions_mention_entity_signal_fields():
    for expected in [
        "people_mentioned",
        "projects_mentioned",
        "commitments_mentioned",
        "meetings_mentioned",
        "personal_items_mentioned",
        "goal_pillar",
        "label_applied",
        "confidence",
    ]:
        assert expected in _ANALYSIS_INSTRUCTIONS


def test_analysis_instructions_mention_all_six_brd_labels_with_semantics():
    for label in [
        "Needs reply: ASAP",
        "Needs reply",
        "Needs reply: mention",
        "Read only",
        "Delete",
        "Undecided",
    ]:
        assert label in _ANALYSIS_INSTRUCTIONS
    # The retired name must not linger anywhere in the live prompt.
    assert "Needs reply: Soon" not in _ANALYSIS_INSTRUCTIONS
    # Semantic guidance (not just bare names) must be present for the two new labels.
    assert "not urgent" in _ANALYSIS_INSTRUCTIONS
    assert "by name" in _ANALYSIS_INSTRUCTIONS


def test_analysis_instructions_mention_p1_p2_priority_guidance():
    assert "'P1'" in _ANALYSIS_INSTRUCTIONS
    assert "'P2'" in _ANALYSIS_INSTRUCTIONS
    assert "business priority" in _ANALYSIS_INSTRUCTIONS
    # No finer-grained criteria exist in the BRD than "business priority" -- the prompt
    # must not claim otherwise by inventing e.g. tier/urgency thresholds here.
    assert "P0" not in _ANALYSIS_INSTRUCTIONS
    assert "P3" not in _ANALYSIS_INSTRUCTIONS


def test_analysis_instructions_do_not_ask_the_llm_for_canonical_ids():
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "per-" not in lowered
    assert "prj-" not in lowered
    assert "canonical id" not in lowered


def test_analysis_instructions_disambiguate_flat_meetings_from_structured_meetings_mentioned():
    # Regression: Claude previously put RawMeeting-shaped objects into the legacy flat
    # `meetings` field (which app.analysis.schemas.EmailAnalysis declares as list[str]),
    # because the prompt never said that field held plain strings.
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "plain-text strings" in lowered or "plain text strings" in lowered
    assert "never contain these objects" in lowered or "never objects" in lowered


def test_update_context_instructions_carve_out_participants_as_plain_strings():
    # Regression: the pre-delta wording ("every list item must be an object with
    # value/basis/source_email_ids") applied to ALL list fields, but ThreadContext.
    # participants is list[str] -- Claude wrapped participants in that object shape
    # and failed Pydantic validation. The instructions must still explicitly exempt it
    # under the new delta contract.
    lowered = _UPDATE_CONTEXT_INSTRUCTIONS.lower()
    assert "participants_added" in lowered
    assert "plain lists of strings" in lowered
    assert "never wrapped in value/basis/source_email_ids" in lowered


def test_update_context_instructions_require_a_bounded_delta_not_the_full_context():
    # This is the core contract of the scalability fix: the LLM must never be asked to
    # reproduce the whole accumulated context (that's what caused max_tokens truncation
    # on long threads) -- only what the current email changes.
    lowered = _UPDATE_CONTEXT_INSTRUCTIONS.lower()
    assert "do not reproduce" in lowered or "never reproduce" in lowered
    assert "bounded" in lowered
    assert "no change" in lowered
    assert "do not wrap the delta in prose" in lowered or "json only" in lowered


def test_openai_and_claude_share_the_identical_update_context_instructions():
    # Both providers must receive the same contract -- sharing one constant (like
    # _ANALYSIS_INSTRUCTIONS already does) makes it structurally impossible for the
    # two prompts to drift apart again the way the pre-fix duplicated strings could.
    assert openai_update_context_instructions is _UPDATE_CONTEXT_INSTRUCTIONS
