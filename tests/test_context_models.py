import pytest
from pydantic import ValidationError

from app.context.models import ContextSnapshot, ProvenancedValue, ThreadContext


def test_thread_context_defaults_are_empty():
    context = ThreadContext()
    assert context.requirements == []
    assert context.company == {}
    assert context.summary == ""


def test_provenanced_value_requires_basis_literal():
    value = ProvenancedValue(value="100 seats", basis="stated", source_email_ids=["msg_001"])
    assert value.basis == "stated"


def test_participants_accepts_plain_strings():
    context = ThreadContext.model_validate({"participants": ["ashok@databeat.io", "john.t@614group.com"]})
    assert context.participants == ["ashok@databeat.io", "john.t@614group.com"]


def test_participants_rejects_wrapped_provenance_objects():
    # Regression: update_context's previous prompt wording ("every list item must be
    # an object with value/basis/source_email_ids") applied to ALL list fields,
    # including participants, which is declared list[str]. The model correctly
    # rejects the wrapped shape -- the fix belongs in the prompt (see
    # test_llm_provider_prompts.py), not in loosening this validation.
    with pytest.raises(ValidationError):
        ThreadContext.model_validate(
            {
                "participants": [
                    {"value": "ashok@databeat.io", "basis": "stated", "source_email_ids": ["msg_001"]}
                ]
            }
        )


def test_context_snapshot_round_trip():
    snapshot = ContextSnapshot(
        thread_id="thread_001",
        context_version=1,
        triggering_email_id="msg_001",
        context=ThreadContext(summary="Evaluating CRM"),
        changes_from_previous_context=[],
        created_at="2026-09-13T10:30:00Z",
    )
    assert snapshot.context.summary == "Evaluating CRM"
