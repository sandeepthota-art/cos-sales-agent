import json

from app.analysis.extractor import analyze_email_with_validation
from app.email.models import parse_email
from app.interfaces.llm_provider import LLMProvider


class _BrokenLLM(LLMProvider):
    def __init__(self, bad_payloads: list[dict]):
        self._payloads = bad_payloads
        self._calls = 0

    def analyze_email(self, email):
        payload = self._payloads[min(self._calls, len(self._payloads) - 1)]
        self._calls += 1
        return payload

    def update_context(self, previous_context, new_analysis):
        raise NotImplementedError

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise NotImplementedError

    def draft_reply(self, context, latest_email):
        raise NotImplementedError


class _JSONFailingLLM(LLMProvider):
    """Simulates llm.analyze_email() itself raising JSONDecodeError (a malformed Claude
    response _extract_json couldn't parse at all), rather than returning a dict that
    later fails Pydantic validation -- the failure mode _BrokenLLM doesn't cover."""

    def __init__(self, outcomes: list):
        # Each entry is either a dict (a valid payload) or the string "raise" (simulate
        # a JSONDecodeError from that attempt).
        self._outcomes = outcomes
        self.call_count = 0

    def analyze_email(self, email):
        outcome = self._outcomes[min(self.call_count, len(self._outcomes) - 1)]
        self.call_count += 1
        if outcome == "raise":
            raise json.JSONDecodeError("Expecting value", "bad json", 0)
        return outcome

    def update_context(self, previous_context, new_analysis):
        raise NotImplementedError

    def verify_same_fact(self, existing_value, new_value, subject, predicate):
        raise NotImplementedError

    def draft_reply(self, context, latest_email):
        raise NotImplementedError


def _email():
    return parse_email(
        {
            "message_id": "msg_001",
            "from": {"name": "John", "email": "john@example.com"},
            "to": [{"name": "Ashok", "email": "ashok@example.com"}],
            "subject": "Hi",
            "body": "Body",
            "timestamp": "2026-09-13T10:30:00Z",
        }
    )


def test_analyze_email_with_validation_succeeds_on_valid_payload():
    llm = _BrokenLLM([{"email_id": "msg_001", "summary": "s", "intent": "evaluation"}])
    outcome = analyze_email_with_validation(llm, _email())
    assert outcome.success is True
    assert outcome.analysis.email_id == "msg_001"
    assert outcome.error is None


def test_analyze_email_with_validation_retries_once_then_succeeds():
    llm = _BrokenLLM(
        [
            {"summary": "missing email_id and intent"},
            {"email_id": "msg_001", "summary": "s", "intent": "evaluation"},
        ]
    )
    outcome = analyze_email_with_validation(llm, _email(), max_retries=1)
    assert outcome.success is True
    assert outcome.analysis.email_id == "msg_001"


def test_analyze_email_with_validation_fails_after_exhausting_retries():
    llm = _BrokenLLM([{"summary": "still invalid"}])
    outcome = analyze_email_with_validation(llm, _email(), max_retries=1)
    assert outcome.success is False
    assert outcome.analysis is None
    assert outcome.error is not None


def test_json_decode_error_from_analyze_email_is_retried():
    llm = _JSONFailingLLM(["raise", {"email_id": "msg_001", "summary": "s", "intent": "evaluation"}])
    outcome = analyze_email_with_validation(llm, _email(), max_retries=1)
    assert outcome.success is True
    assert outcome.analysis.email_id == "msg_001"
    assert llm.call_count == 2


def test_successful_first_attempt_makes_no_unnecessary_second_call():
    llm = _JSONFailingLLM([{"email_id": "msg_001", "summary": "s", "intent": "evaluation"}])
    outcome = analyze_email_with_validation(llm, _email(), max_retries=1)
    assert outcome.success is True
    assert llm.call_count == 1


def test_json_decode_error_retry_failure_surfaces_final_error_cleanly():
    llm = _JSONFailingLLM(["raise", "raise"])
    outcome = analyze_email_with_validation(llm, _email(), max_retries=1)
    assert outcome.success is False
    assert outcome.analysis is None
    assert "JSONDecodeError" in outcome.error
    assert llm.call_count == 2  # exhausted the retry budget, did not retry indefinitely
