import json

import pytest
from pydantic import ValidationError

from app.analysis.schemas import EmailAnalysis
from app.context.engine import build_next_context
from app.context.models import ThreadContext
from app.interfaces.llm_provider import LLMProvider
from app.providers.llm.mock import MockLLMProvider


class _JSONFailingLLM(LLMProvider):
    """Simulates llm.update_context() raising JSONDecodeError or returning a dict that
    fails ContextDelta validation -- build_next_context previously had NO retry for
    either case, unlike analyze_email_with_validation."""

    def __init__(self, outcomes: list):
        # Each entry is a dict (valid delta), "raise_json" (JSONDecodeError), or
        # "raise_validation" (a delta shape ContextDelta will reject).
        self._outcomes = outcomes
        self.call_count = 0

    def analyze_email(self, email):
        raise NotImplementedError

    def update_context(self, previous_context, new_analysis):
        outcome = self._outcomes[min(self.call_count, len(self._outcomes) - 1)]
        self.call_count += 1
        if outcome == "raise_json":
            raise json.JSONDecodeError("Expecting value", "bad json", 0)
        if outcome == "raise_validation":
            return {"participants_added": [{"not": "a plain string"}]}
        return outcome

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise NotImplementedError

    def draft_reply(self, context, latest_email):
        raise NotImplementedError


def test_build_first_context_version_from_empty_previous():
    llm = MockLLMProvider()
    analysis = EmailAnalysis(
        email_id="msg_001",
        summary="ABC Corp evaluating CRM",
        intent="evaluation",
        requirements=["100 seats"],
        competitors=["Salesforce"],
    )
    context, changes = build_next_context(previous=None, analysis=analysis, source_email_id="msg_001", llm=llm)

    assert context.requirements[0].value == "100 seats"
    assert context.requirements[0].source_email_ids == ["msg_001"]
    assert any(c.type == "ADDED" and c.field == "requirements" for c in changes)


def test_build_second_context_version_accumulates_on_first():
    llm = MockLLMProvider()
    analysis_1 = EmailAnalysis(email_id="msg_001", summary="Intro", intent="evaluation", requirements=["100 seats"])
    context_v1, _ = build_next_context(previous=None, analysis=analysis_1, source_email_id="msg_001", llm=llm)

    analysis_2 = EmailAnalysis(
        email_id="msg_007", summary="Follow up", intent="evaluation", requirements=["150 seats"]
    )
    context_v2, changes = build_next_context(
        previous=context_v1, analysis=analysis_2, source_email_id="msg_007", llm=llm
    )

    values = {r.value for r in context_v2.requirements}
    assert "100 seats" in values
    assert "150 seats" in values
    assert any(c.type == "ADDED" and c.detail == "150 seats" for c in changes)


def test_context_never_loses_prior_information_when_new_analysis_is_empty():
    llm = MockLLMProvider()
    analysis_1 = EmailAnalysis(email_id="msg_001", summary="Intro", intent="evaluation", competitors=["Salesforce"])
    context_v1, _ = build_next_context(previous=None, analysis=analysis_1, source_email_id="msg_001", llm=llm)

    analysis_2 = EmailAnalysis(email_id="msg_002", summary="Just checking in", intent="evaluation")
    context_v2, _ = build_next_context(previous=context_v1, analysis=analysis_2, source_email_id="msg_002", llm=llm)

    assert "Salesforce" in {c.value for c in context_v2.competitors}


def _analysis():
    return EmailAnalysis(email_id="msg_001", summary="s", intent="evaluation")


def test_json_decode_error_from_update_context_is_retried():
    llm = _JSONFailingLLM(["raise_json", {"summary": "Recovered on retry"}])
    context, _ = build_next_context(previous=None, analysis=_analysis(), source_email_id="msg_001", llm=llm)
    assert context.summary == "Recovered on retry"
    assert llm.call_count == 2


def test_validation_error_from_update_context_is_retried():
    llm = _JSONFailingLLM(["raise_validation", {"summary": "Recovered on retry"}])
    context, _ = build_next_context(previous=None, analysis=_analysis(), source_email_id="msg_001", llm=llm)
    assert context.summary == "Recovered on retry"
    assert llm.call_count == 2


def test_successful_first_update_context_call_makes_no_unnecessary_second_call():
    llm = _JSONFailingLLM([{"summary": "First try"}])
    context, _ = build_next_context(previous=None, analysis=_analysis(), source_email_id="msg_001", llm=llm)
    assert context.summary == "First try"
    assert llm.call_count == 1


def test_update_context_retry_failure_surfaces_final_error_and_does_not_retry_indefinitely():
    llm = _JSONFailingLLM(["raise_json", "raise_json"])
    with pytest.raises(json.JSONDecodeError):
        build_next_context(previous=None, analysis=_analysis(), source_email_id="msg_001", llm=llm)
    assert llm.call_count == 2  # max_retries=1 default -> 2 total attempts, then stop


def test_update_context_final_validation_error_is_the_one_surfaced():
    llm = _JSONFailingLLM(["raise_validation", "raise_validation"])
    with pytest.raises(ValidationError):
        build_next_context(previous=None, analysis=_analysis(), source_email_id="msg_001", llm=llm)
