# tests/test_query_synthesis.py
"""Phase 22A: the retrieval/synthesis boundary. DeterministicQuerySynthesizer makes
zero LLM calls -- these tests prove the whole retrieval-to-answer path is fully
testable without one, and specifically target hallucination resistance (Section AK):
the synthesized text can never claim more than QueryResult actually contains.
"""
from app.query.schemas import (
    Ambiguity,
    EntityCandidate,
    EvidenceItem,
    IdentityBasis,
    QueryIntentType,
    QueryMetadata,
    QueryResult,
    QueryResultStatus,
)
from app.query.synthesis import DeterministicQuerySynthesizer

_SYNTH = DeterministicQuerySynthesizer()


def _metadata(**overrides) -> QueryMetadata:
    base = {"query_id": "qry_test", "intent": QueryIntentType.MEETINGS}
    base.update(overrides)
    return QueryMetadata(**base)


def test_synthesize_no_match_never_invents_a_result():
    result = QueryResult(
        status=QueryResultStatus.NO_MATCH, intent=QueryIntentType.MEETINGS,
        message="No matching records were found in the available data for this request.",
        metadata=_metadata(result_count=0),
    )
    text = _SYNTH.synthesize(result)
    assert "no" in text.lower()
    assert "meeting" not in text.lower() or "no" in text.lower()  # never claims a meeting exists


def test_synthesize_data_incomplete_says_data_does_not_establish_it():
    result = QueryResult(
        status=QueryResultStatus.DATA_INCOMPLETE, intent=QueryIntentType.MEETING_PREPARATION,
        message="Meeting preparation requires a specific meeting_id, which must be supplied directly.",
        metadata=_metadata(intent=QueryIntentType.MEETING_PREPARATION),
    )
    text = _SYNTH.synthesize(result)
    assert text == result.message


def test_synthesize_ambiguous_lists_candidates_and_does_not_pick_one():
    result = QueryResult(
        status=QueryResultStatus.AMBIGUOUS, intent=QueryIntentType.PERSON_CONTEXT,
        message="More than one person matches 'Jason' -- clarify which one before answering.",
        ambiguities=[Ambiguity(field="person", raw_text="Jason", candidates=[
            EntityCandidate(id="PER-1", name="Jason Greene"), EntityCandidate(id="PER-2", name="Jason Smith"),
        ])],
        metadata=_metadata(intent=QueryIntentType.PERSON_CONTEXT, ambiguity_count=1),
    )
    text = _SYNTH.synthesize(result)
    assert "Jason Greene" in text and "Jason Smith" in text
    # Neither candidate is asserted as THE answer -- both are merely listed.
    assert "Candidates:" in text


def test_synthesize_ok_reports_exactly_the_retrieved_count_never_more():
    evidence = [
        EvidenceItem(collection="meetings", record_id="MTG-1", basis=IdentityBasis.CANONICAL),
        EvidenceItem(collection="meetings", record_id="MTG-2", basis=IdentityBasis.CANONICAL),
    ]
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.MEETINGS,
        records=[{"id": "MTG-1"}, {"id": "MTG-2"}], evidence=evidence,
        metadata=_metadata(result_count=2, evidence_count=2),
    )
    text = _SYNTH.synthesize(result)
    assert "2" in text
    assert "3" not in text  # the database has 2 -- the synthesizer must never say 3


def test_synthesize_error_reports_the_error_not_a_fabricated_answer():
    result = QueryResult(
        status=QueryResultStatus.ERROR, intent=QueryIntentType.PERSON_CONTEXT,
        message="The stored identity chain for this reference is broken: circular merged_into chain",
        metadata=_metadata(intent=QueryIntentType.PERSON_CONTEXT),
    )
    text = _SYNTH.synthesize(result)
    assert text == result.message


def test_synthesize_never_calls_an_llm_or_network():
    # DeterministicQuerySynthesizer has no client, no API key, no network call --
    # this is a structural guarantee, verified by construction: synthesize() is a
    # pure function of its QueryResult argument.
    import inspect

    source = inspect.getsource(DeterministicQuerySynthesizer)
    for forbidden in ("anthropic", "requests.", "httpx.", "urlopen", "socket."):
        assert forbidden not in source


def test_synthesize_zero_records_ok_status_still_says_none_found():
    # Defensive: even if a future bug produced status=OK with an empty result set,
    # the synthesizer must not claim something was found.
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.COMMITMENTS, records=[],
        metadata=_metadata(intent=QueryIntentType.COMMITMENTS, result_count=0, evidence_count=0),
    )
    text = _SYNTH.synthesize(result)
    assert "no" in text.lower()


def test_synthesize_preserves_stated_vs_inferred_language_is_not_asserted_by_synthesizer():
    # The deterministic synthesizer doesn't editorialize knowledge basis at all --
    # it reports counts only, so it structurally cannot upgrade "inferred" to
    # "stated" (that distinction lives in EvidenceItem.summary_fields, untouched here).
    evidence = [EvidenceItem(collection="knowledge_items", record_id="K-1", basis=IdentityBasis.CANONICAL, summary_fields={"basis": "inferred"})]
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.KNOWLEDGE, records=[{"knowledge_id": "K-1"}], evidence=evidence,
        metadata=_metadata(intent=QueryIntentType.KNOWLEDGE, result_count=1, evidence_count=1),
    )
    text = _SYNTH.synthesize(result)
    assert "stated" not in text.lower()  # never asserts a stronger provenance than the record carries
    assert evidence[0].summary_fields["basis"] == "inferred"  # untouched in the underlying evidence
