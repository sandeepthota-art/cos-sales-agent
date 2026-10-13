from pathlib import Path

from app.providers.llm.claude import _ANALYSIS_INSTRUCTIONS, _UPDATE_CONTEXT_INSTRUCTIONS
from app.providers.llm.openai import _UPDATE_CONTEXT_INSTRUCTIONS as openai_update_context_instructions

_SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"


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
        "1. Needs reply: ASAP",
        "1. Needs reply",
        "1. Needs reply: mention",
        "1. Read only",
        "1. Delete",
        "1. Undecided",
    ]:
        assert label in _ANALYSIS_INSTRUCTIONS
    # The retired name must not linger anywhere in the live prompt.
    assert "Needs reply: Soon" not in _ANALYSIS_INSTRUCTIONS
    # Semantic guidance (not just bare names) must be present for the two new labels.
    assert "not urgent" in _ANALYSIS_INSTRUCTIONS
    assert "by name" in _ANALYSIS_INSTRUCTIONS


def test_analysis_instructions_do_not_ask_for_priority_or_confidence():
    # priority and confidence were removed from EmailAnalysis entirely -- the prompt
    # must never ask the LLM to produce either.
    assert "priority" not in _ANALYSIS_INSTRUCTIONS.lower()
    assert "confidence (" not in _ANALYSIS_INSTRUCTIONS


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


def test_analysis_instructions_give_explicit_criteria_for_recognizing_a_sales_project():
    # Regression: projects_mentioned was previously just a bare field name with no
    # trigger criteria (unlike meetings_mentioned's explicit rule below it), and the
    # live projects collection stayed empty in production as a result. The prompt must
    # now name concrete deal-shaped signals, not just the field name.
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "sales deal" in lowered or "opportunity" in lowered
    for signal in ["negotiation", "pricing", "proposal", "renewal"]:
        assert signal in lowered


def test_analysis_instructions_require_goal_pillar_sales_to_agree_with_projects_mentioned():
    # The other half of the same fix: goal_pillar must be tied to genuine deal
    # recognition, never forced to "Sales" as a blanket default (that would break the
    # existing multi-pillar Finance/Operations classification used elsewhere).
    assert "goal_pillar and this projects_mentioned entry must agree" in _ANALYSIS_INSTRUCTIONS
    assert "never set goal_pillar to 'sales' for an email that reflects no genuine deal activity" in _ANALYSIS_INSTRUCTIONS.lower()


def test_analysis_instructions_never_invent_a_project():
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "leave projects_mentioned empty rather than guessing" in lowered


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


def test_analysis_instructions_require_evidence_on_person_facts_mentioned():
    # Evidence-generation task: person_facts_mentioned's field list must include
    # evidence, and the prompt must require it be a verbatim excerpt from THIS
    # email, never a paraphrase or a quote borrowed from elsewhere.
    assert "person_name, person_email, category, value, basis, evidence" in _ANALYSIS_INSTRUCTIONS
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "evidence is required" in lowered
    assert "verbatim" in lowered
    assert "never a quote borrowed from a different email" in lowered


def test_analysis_instructions_tell_the_llm_to_omit_unsupported_person_facts():
    # The prompt must push the "don't fabricate evidence" responsibility upstream --
    # an entry with no real supporting excerpt must be left out entirely, not
    # submitted with invented or paraphrased evidence.
    lowered = _ANALYSIS_INSTRUCTIONS.lower()
    assert "do not include that person_facts_mentioned entry at all" in lowered


def _read_skill(name: str) -> str:
    return (_SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _collapse_whitespace(text: str) -> str:
    # Markdown prose is hand-wrapped at ~80 columns -- a real newline+indent
    # inside a sentence is not semantically meaningful, so substring checks
    # against skill text normalize whitespace first (the same principle
    # app.entities.evidence_validation.normalize_for_match applies to email
    # bodies), rather than asserting on exact line-wrap positions.
    return " ".join(text.split())


def test_gmail_initial_ingest_skill_requires_evidence_on_person_facts_mentioned():
    # Evidence-generation task: the historical-ingestion skill must carry the same
    # evidence contract as skills/context-building/SKILL.md and _ANALYSIS_INSTRUCTIONS
    # -- a verbatim excerpt from the SAME email, never another email/thread/general
    # knowledge/inferred context.
    collapsed = _collapse_whitespace(_read_skill("gmail-initial-ingest"))
    lowered = collapsed.lower()
    assert "person_facts_mentioned` entry needs an `evidence` field" in collapsed
    assert "verbatim" in lowered
    assert "never a quote from a different email, a different thread, general knowledge, or inferred context" in lowered


def test_gmail_initial_ingest_skill_tells_claude_to_omit_unsupported_person_facts():
    collapsed = _collapse_whitespace(_read_skill("gmail-initial-ingest"))
    lowered = collapsed.lower()
    assert "leave that entry out of `person_facts_mentioned` entirely" in collapsed
    assert "persist_email_analysis` silently drops any entry whose evidence doesn't check out" in lowered


def test_gmail_initial_ingest_and_context_building_skills_state_the_evidence_rule_consistently():
    # Both skills should describe the same contract in materially the same words --
    # this pins them together so a future edit to one that forgets the other is
    # caught here rather than silently drifting.
    gmail_text = _collapse_whitespace(_read_skill("gmail-initial-ingest"))
    context_building_text = _collapse_whitespace(_read_skill("context-building"))
    shared_phrase = "a short excerpt copied VERBATIM from this specific email's own subject or body"
    assert shared_phrase in gmail_text
    assert shared_phrase in context_building_text
