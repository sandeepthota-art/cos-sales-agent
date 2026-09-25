import json

import pytest
from pydantic import ValidationError

from app.analysis.schemas import EmailAnalysis
from app.context.models import ThreadContext
from app.email.models import parse_email
from app.interfaces.llm_provider import LLMProvider
from app.replies.drafter import draft_reply, needs_reply
from app.providers.llm.mock import MockLLMProvider


class _JSONFailingLLM(LLMProvider):
    """Simulates llm.draft_reply() raising JSONDecodeError or returning a dict that
    fails ReplyDraftContent validation -- draft_reply previously had NO retry for
    either case, unlike analyze_email/update_context."""

    def __init__(self, outcomes: list):
        # Each entry is a dict (valid draft), "raise_json" (JSONDecodeError), or
        # "raise_validation" (a shape ReplyDraftContent will reject).
        self._outcomes = outcomes
        self.call_count = 0

    def analyze_email(self, email):
        raise NotImplementedError

    def update_context(self, previous_context, new_analysis):
        raise NotImplementedError

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise NotImplementedError

    def draft_reply(self, context, latest_email):
        outcome = self._outcomes[min(self.call_count, len(self._outcomes) - 1)]
        self.call_count += 1
        if outcome == "raise_json":
            raise json.JSONDecodeError("Invalid control character", "bad json", 682)
        if outcome == "raise_validation":
            return {"subject": None}  # missing required "body", wrong type for "subject"
        return outcome


def _email(body="Can you send pricing information?"):
    return parse_email(
        {
            "message_id": "msg_004",
            "from": {"name": "John", "email": "john@example.com"},
            "to": [{"name": "Ashok", "email": "ashok@example.com"}],
            "subject": "Enterprise pricing",
            "body": body,
            "timestamp": "2026-09-13T10:30:00Z",
        }
    )


def test_needs_reply_true_when_buying_signals_present():
    analysis = EmailAnalysis(email_id="msg_004", summary="s", intent="evaluation", buying_signals=["pricing request"])
    assert needs_reply(analysis, _email()) is True


def test_needs_reply_false_when_no_signals_and_no_question():
    analysis = EmailAnalysis(email_id="msg_004", summary="s", intent="evaluation")
    email = _email(body="Thanks, sounds good.")
    assert needs_reply(analysis, email) is False


def test_needs_reply_true_when_body_contains_question():
    analysis = EmailAnalysis(email_id="msg_004", summary="s", intent="evaluation")
    email = _email(body="Can we schedule a call?")
    assert needs_reply(analysis, email) is True


def test_draft_reply_returns_subject_and_body():
    llm = MockLLMProvider()
    context = ThreadContext(summary="ABC Corp evaluating enterprise plan")
    draft = draft_reply(llm, context, _email())
    assert draft.subject == "Re: Enterprise pricing"
    assert len(draft.body) > 0


def test_draft_reply_retries_once_after_json_decode_error_then_succeeds():
    llm = _JSONFailingLLM(["raise_json", {"subject": "Re: Enterprise pricing", "body": "Recovered on retry"}])
    context = ThreadContext(summary="s")

    draft = draft_reply(llm, context, _email())

    assert draft.body == "Recovered on retry"
    assert llm.call_count == 2


def test_draft_reply_retries_once_after_validation_error_then_succeeds():
    llm = _JSONFailingLLM(["raise_validation", {"subject": "Re: Enterprise pricing", "body": "Recovered on retry"}])
    context = ThreadContext(summary="s")

    draft = draft_reply(llm, context, _email())

    assert draft.body == "Recovered on retry"
    assert llm.call_count == 2


def test_draft_reply_successful_first_attempt_makes_no_unnecessary_second_call():
    llm = _JSONFailingLLM([{"subject": "Re: Enterprise pricing", "body": "First try"}])
    context = ThreadContext(summary="s")

    draft = draft_reply(llm, context, _email())

    assert draft.body == "First try"
    assert llm.call_count == 1


def test_draft_reply_json_decode_error_retry_exhaustion_raises_cleanly():
    llm = _JSONFailingLLM(["raise_json", "raise_json"])
    context = ThreadContext(summary="s")

    with pytest.raises(json.JSONDecodeError):
        draft_reply(llm, context, _email())

    assert llm.call_count == 2  # max_retries=1 default -> 2 total attempts, then stop


def test_draft_reply_validation_error_retry_exhaustion_raises_the_validation_error():
    llm = _JSONFailingLLM(["raise_validation", "raise_validation"])
    context = ThreadContext(summary="s")

    with pytest.raises(ValidationError):
        draft_reply(llm, context, _email())
