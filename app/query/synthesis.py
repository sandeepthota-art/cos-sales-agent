"""Phase 22A/22B: the retrieval/synthesis boundary (Section P).

app.query.service.execute_query returns a QueryResult built ENTIRELY from real
MongoDB documents. Nothing past this point may add a new fact -- a synthesizer's
only job is to phrase what QueryResult already contains: summarize, explain,
organize. It must never invent a record, resolve an ambiguity on its own, upgrade
inferred knowledge to a stated fact, or answer at all when the result says the data
doesn't establish it.

Two implementations:
  - DeterministicQuerySynthesizer: the default, always-available, template-based
    synthesizer -- it cannot hallucinate because it only ever formats fields
    already present in QueryResult, with zero LLM call. This is what proves "the
    core retrieval layer must function without an LLM" end to end.
  - ClaudeQuerySynthesizer (Phase 22B.1): a live-LLM-backed synthesizer, added as
    the smallest adapter necessary rather than by extending
    app.interfaces.llm_provider.LLMProvider -- that interface's four methods are
    all scoped to email ingestion (analyze_email/update_context/verify_same_fact/
    draft_reply), and none fit generic Q&A synthesis; overloading it would blur an
    interface with a different, established purpose. ClaudeQuerySynthesizer never
    constructs its own `anthropic.Anthropic()` client internally by default in a
    way that forces a network call during tests -- a client (or any object
    exposing the same `.messages.create` shape) is passed in, so tests inject a
    fake and never touch the network. For every status OTHER than OK, it never
    calls the LLM at all -- it delegates to DeterministicQuerySynthesizer's exact
    same handling, since there is nothing to creatively synthesize in a NO_MATCH/
    AMBIGUOUS/DATA_INCOMPLETE/ERROR result and every one of those must already be
    phrased exactly as the retrieval layer determined, never re-interpreted.
"""

from typing import Any, Protocol

from app.query.schemas import EvidenceItem, QueryResult, QueryResultStatus


class QuerySynthesizer(Protocol):
    def synthesize(self, result: QueryResult) -> str: ...


_INTENT_LABELS = {
    "meetings": "meetings",
    "commitments": "commitments",
    "follow_ups": "follow-ups",
    "knowledge": "knowledge items",
    "reply_drafts": "reply drafts",
    "person_context": "person context",
    "organization_context": "organization context",
    "project_context": "project context",
    "thread_context": "thread activity",
    "cross_entity_activity": "activity",
    "meeting_preparation": "meeting preparation context",
}


class DeterministicQuerySynthesizer:
    """Zero LLM calls. Cannot hallucinate: every sentence it produces is built
    directly from QueryResult's own status/message/records/evidence fields."""

    def synthesize(self, result: QueryResult) -> str:
        if result.status == QueryResultStatus.AMBIGUOUS:
            names = ", ".join(c.name for a in result.ambiguities for c in a.candidates)
            return f"{result.message} Candidates: {names}."
        if result.status == QueryResultStatus.NO_MATCH:
            return result.message or "No matching records were found in the available data."
        if result.status == QueryResultStatus.DATA_INCOMPLETE:
            return result.message or "The available data does not establish this."
        if result.status == QueryResultStatus.ERROR:
            return result.message or "This request could not be completed."

        label = _INTENT_LABELS.get(result.intent.value, result.intent.value)
        count = result.metadata.result_count
        if count == 0:
            return f"No {label} were found in the available data."
        if count == 1:
            return f"Found 1 {label[:-1] if label.endswith('s') else label} record, backed by {result.metadata.evidence_count} evidence item(s)."
        return f"Found {count} {label} records, backed by {result.metadata.evidence_count} evidence item(s)."


_SYNTHESIS_SYSTEM_PROMPT = (
    "You are a Chief-of-Staff sales assistant. You answer using ONLY the evidence list "
    "provided below -- it is the complete and final set of facts available to you. "
    "Rules, none of which may ever be broken:\n"
    "1. Never mention, invent, or assume a person, organization, meeting, commitment, "
    "follow-up, email, or relationship that is not explicitly listed in the evidence.\n"
    "2. Never state a count different from the exact count given.\n"
    "3. Never invent or alter a date, status, or identity.\n"
    "4. If an evidence item's basis is 'inferred', describe it as inferred, never as a "
    "confirmed or stated fact.\n"
    "5. If an evidence item's basis is 'legacy_thread_scoped' or 'unresolved', note that "
    "the identity link is approximate, never present it as certain.\n"
    "6. Answer only the question asked, using only what is listed. If the evidence is "
    "insufficient to fully answer, say so explicitly rather than filling the gap.\n"
    "Respond with plain text only -- no JSON, no markdown code fences."
)


def _evidence_summary_line(item: EvidenceItem) -> str:
    fields = ", ".join(f"{k}={v!r}" for k, v in item.summary_fields.items())
    return f"- [{item.collection}#{item.record_id}] basis={item.basis.value}" + (f" {fields}" if fields else "")


def _build_synthesis_prompt(result: QueryResult) -> str:
    lines = [
        f"Question intent: {result.intent.value}",
        f"Exact result count: {result.metadata.result_count}",
        f"Exact evidence count: {result.metadata.evidence_count}",
        "Evidence:",
    ]
    lines.extend(_evidence_summary_line(item) for item in result.evidence)
    return "\n".join(lines)


class ClaudeQuerySynthesizer:
    """Phase 22B.1. `client` must expose `.messages.create(model=..., max_tokens=...,
    system=..., messages=[...])` returning an object with `.content[0].text` -- the
    same shape `anthropic.Anthropic()` provides, matching
    app.providers.llm.claude's existing usage pattern. Passing a real
    `anthropic.Anthropic()` (or None, which lazily constructs one only on first real
    use) wires this to the live API; passing a fake object with the same shape (as
    every test in this codebase does) makes this fully offline and deterministic to
    test.
    """

    def __init__(self, client: Any = None, model: str = "claude-sonnet-5", max_tokens: int = 500):
        self._client = client
        self._model = model
        self._max_tokens = max_tokens
        self._fallback = DeterministicQuerySynthesizer()

    def _get_client(self) -> Any:
        if self._client is None:
            import anthropic

            self._client = anthropic.Anthropic()
        return self._client

    def synthesize(self, result: QueryResult) -> str:
        if result.status != QueryResultStatus.OK:
            # Nothing to creatively synthesize in a non-OK result -- and every one
            # of these states must be reported exactly as retrieval determined it,
            # never re-interpreted by an LLM.
            return self._fallback.synthesize(result)

        client = self._get_client()
        response = client.messages.create(
            model=self._model, max_tokens=self._max_tokens, system=_SYNTHESIS_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_synthesis_prompt(result)}],
        )
        return response.content[0].text
