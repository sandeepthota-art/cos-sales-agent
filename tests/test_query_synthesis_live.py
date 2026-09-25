# tests/test_query_synthesis_live.py
"""Phase 22B.1: ClaudeQuerySynthesizer. A fake client (matching the real
anthropic.Anthropic().messages.create(...) shape) is injected in every test --
no network call, no API key, ever."""
from app.query.schemas import (
    EvidenceItem,
    IdentityBasis,
    QueryIntentType,
    QueryMetadata,
    QueryResult,
    QueryResultStatus,
)
from app.query.synthesis import ClaudeQuerySynthesizer, _build_synthesis_prompt


class _FakeTextBlock:
    def __init__(self, text):
        self.text = text


class _FakeResponse:
    def __init__(self, text):
        self.content = [_FakeTextBlock(text)]


class _FakeMessages:
    def __init__(self, response_text=None, capture=None):
        self._response_text = response_text or "A grounded answer."
        self._capture = capture if capture is not None else []

    def create(self, **kwargs):
        self._capture.append(kwargs)
        return _FakeResponse(self._response_text)


class _FakeClient:
    def __init__(self, response_text=None):
        self.calls = []
        self.messages = _FakeMessages(response_text=response_text, capture=self.calls)


def _metadata(**overrides) -> QueryMetadata:
    base = {"query_id": "qry_test", "intent": QueryIntentType.MEETINGS}
    base.update(overrides)
    return QueryMetadata(**base)


def test_claude_synthesizer_calls_the_client_only_for_ok_status():
    client = _FakeClient()
    synth = ClaudeQuerySynthesizer(client=client)
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.MEETINGS, records=[{"id": "MTG-1"}],
        evidence=[EvidenceItem(collection="meetings", record_id="MTG-1", basis=IdentityBasis.CANONICAL)],
        metadata=_metadata(result_count=1, evidence_count=1),
    )

    text = synth.synthesize(result)

    assert text == "A grounded answer."
    assert len(client.calls) == 1


def test_claude_synthesizer_never_calls_the_client_for_non_ok_status():
    client = _FakeClient()
    synth = ClaudeQuerySynthesizer(client=client)

    for status, message in [
        (QueryResultStatus.NO_MATCH, "No matching records were found."),
        (QueryResultStatus.DATA_INCOMPLETE, "The available data does not establish this."),
        (QueryResultStatus.ERROR, "Something is broken."),
    ]:
        result = QueryResult(status=status, intent=QueryIntentType.MEETINGS, message=message, metadata=_metadata())
        text = synth.synthesize(result)
        assert text == message

    assert client.calls == []  # zero LLM calls across all three non-OK cases


def test_claude_synthesizer_ambiguous_never_calls_the_client_either():
    from app.query.schemas import Ambiguity, EntityCandidate

    client = _FakeClient()
    synth = ClaudeQuerySynthesizer(client=client)
    result = QueryResult(
        status=QueryResultStatus.AMBIGUOUS, intent=QueryIntentType.PERSON_CONTEXT,
        message="More than one person matches 'Jason'.",
        ambiguities=[Ambiguity(field="person", raw_text="Jason", candidates=[EntityCandidate(id="PER-1", name="Jason Greene")])],
        metadata=_metadata(intent=QueryIntentType.PERSON_CONTEXT, ambiguity_count=1),
    )

    text = synth.synthesize(result)

    assert "Jason Greene" in text
    assert client.calls == []


def test_build_synthesis_prompt_includes_exact_count_and_evidence_only():
    evidence = [
        EvidenceItem(collection="meetings", record_id="MTG-1", basis=IdentityBasis.CANONICAL, summary_fields={"date": "2026-01-01"}),
        EvidenceItem(collection="meetings", record_id="MTG-2", basis=IdentityBasis.CANONICAL, summary_fields={"date": "2026-01-02"}),
    ]
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.MEETINGS, records=[{}, {}], evidence=evidence,
        metadata=_metadata(result_count=2, evidence_count=2),
    )

    prompt = _build_synthesis_prompt(result)

    assert "Exact result count: 2" in prompt
    assert "MTG-1" in prompt and "MTG-2" in prompt
    assert "MTG-3" not in prompt  # never more than what's actually there


def test_build_synthesis_prompt_preserves_inferred_basis_label():
    evidence = [EvidenceItem(collection="knowledge_items", record_id="K-1", basis=IdentityBasis.CANONICAL, summary_fields={"basis": "inferred"})]
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.KNOWLEDGE, records=[{}], evidence=evidence,
        metadata=_metadata(intent=QueryIntentType.KNOWLEDGE, result_count=1, evidence_count=1),
    )

    prompt = _build_synthesis_prompt(result)

    assert "basis='inferred'" in prompt


def test_claude_synthesizer_system_prompt_forbids_hallucination():
    client = _FakeClient()
    synth = ClaudeQuerySynthesizer(client=client)
    result = QueryResult(
        status=QueryResultStatus.OK, intent=QueryIntentType.COMMITMENTS, records=[{"id": "COM-1"}],
        evidence=[EvidenceItem(collection="commitments", record_id="COM-1", basis=IdentityBasis.CANONICAL)],
        metadata=_metadata(intent=QueryIntentType.COMMITMENTS, result_count=1, evidence_count=1),
    )

    synth.synthesize(result)

    system_prompt = client.calls[0]["system"]
    assert "Never mention, invent, or assume" in system_prompt
    assert "Never state a count different" in system_prompt
    assert "inferred" in system_prompt.lower()


def test_claude_synthesizer_lazily_constructs_a_real_client_only_when_none_injected():
    # Constructing ClaudeQuerySynthesizer() with no client must not itself touch the
    # network or require an API key -- only calling .synthesize() on an OK result
    # would ever attempt that (and no test here does, since every OK-path test
    # injects a fake client explicitly).
    synth = ClaudeQuerySynthesizer()
    assert synth._client is None
