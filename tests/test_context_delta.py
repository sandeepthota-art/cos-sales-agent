import json

import pytest
from pydantic import ValidationError

from app.context.engine import apply_context_delta
from app.context.models import ContextDelta, LIST_FIELDS, ProvenancedValue, ThreadContext


# --- A. Empty/small previous context + delta ---


def test_empty_previous_context_with_empty_delta_stays_empty():
    previous = ThreadContext()
    delta = ContextDelta()

    result = apply_context_delta(previous, delta, source_email_id="msg_001")

    assert result == ThreadContext()


def test_empty_previous_context_with_small_delta_applies_cleanly():
    previous = ThreadContext()
    delta = ContextDelta.model_validate({"pain_points": {"added": [{"value": "Pricing", "basis": "stated"}]}})

    result = apply_context_delta(previous, delta, source_email_id="msg_001")

    assert result.pain_points == [ProvenancedValue(value="Pricing", basis="stated", source_email_ids=["msg_001"])]


# --- B. Adding a participant ---


def test_adding_a_participant_appends_without_duplicating():
    previous = ThreadContext(participants=["ashok@databeat.io"])
    delta = ContextDelta.model_validate(
        {"participants_added": ["john.t@614group.com", "ashok@databeat.io"]}  # one dup, one new
    )

    result = apply_context_delta(previous, delta, source_email_id="msg_002")

    assert result.participants == ["ashok@databeat.io", "john.t@614group.com"]


def test_removing_a_participant():
    previous = ThreadContext(participants=["ashok@databeat.io", "john.t@614group.com"])
    delta = ContextDelta.model_validate({"participants_removed": ["john.t@614group.com"]})

    result = apply_context_delta(previous, delta, source_email_id="msg_003")

    assert result.participants == ["ashok@databeat.io"]


# --- C. Updating an existing fact (represented as remove old + add new) ---


def test_updating_a_fact_is_expressed_as_removed_plus_added():
    previous = ThreadContext(
        pricing={"budget": "$50K"},
        commitments=[ProvenancedValue(value="Send proposal by Friday", basis="stated", source_email_ids=["msg_001"])],
    )
    delta = ContextDelta.model_validate(
        {
            "pricing_updates": {"budget": "$75K"},
            "commitments": {
                "removed": ["Send proposal by Friday"],
                "added": [{"value": "Send proposal by Monday", "basis": "stated"}],
            },
        }
    )

    result = apply_context_delta(previous, delta, source_email_id="msg_002")

    assert result.pricing == {"budget": "$75K"}
    assert [c.value for c in result.commitments] == ["Send proposal by Monday"]


# --- D. Adding a new fact ---


def test_adding_a_new_fact_to_a_field_with_existing_items():
    previous = ThreadContext(pain_points=[ProvenancedValue(value="Pricing", basis="stated", source_email_ids=["msg_001"])])
    delta = ContextDelta.model_validate({"pain_points": {"added": [{"value": "Slow onboarding", "basis": "stated"}]}})

    result = apply_context_delta(previous, delta, source_email_id="msg_002")

    values = {p.value for p in result.pain_points}
    assert values == {"Pricing", "Slow onboarding"}


# --- E. Preserving unchanged context ---


def test_unrelated_fields_are_untouched_by_a_narrow_delta():
    previous = ThreadContext(
        summary="Evaluating CRM",
        participants=["ashok@databeat.io"],
        competitors=[ProvenancedValue(value="Salesforce", basis="stated", source_email_ids=["msg_001"])],
        company={"name": "Acme Corp"},
    )
    delta = ContextDelta.model_validate({"pain_points": {"added": [{"value": "Pricing", "basis": "stated"}]}})

    result = apply_context_delta(previous, delta, source_email_id="msg_002")

    assert result.summary == "Evaluating CRM"
    assert result.participants == ["ashok@databeat.io"]
    assert result.competitors == previous.competitors
    assert result.company == {"name": "Acme Corp"}


# --- F. Provenance preservation ---


def test_existing_items_keep_their_original_provenance():
    previous = ThreadContext(
        requirements=[ProvenancedValue(value="100 seats", basis="stated", source_email_ids=["msg_001"])]
    )
    delta = ContextDelta.model_validate({"requirements": {"added": [{"value": "SSO support", "basis": "inferred"}]}})

    result = apply_context_delta(previous, delta, source_email_id="msg_005")

    original = next(r for r in result.requirements if r.value == "100 seats")
    new = next(r for r in result.requirements if r.value == "SSO support")
    assert original.source_email_ids == ["msg_001"]  # untouched, NOT overwritten with msg_005
    assert original.basis == "stated"
    assert new.source_email_ids == ["msg_005"]
    assert new.basis == "inferred"


# --- G. Multiple consecutive deltas ---


def test_multiple_consecutive_deltas_accumulate_correctly():
    context = ThreadContext()

    delta_1 = ContextDelta.model_validate({"requirements": {"added": [{"value": "100 seats", "basis": "stated"}]}})
    context = apply_context_delta(context, delta_1, source_email_id="msg_001")

    delta_2 = ContextDelta.model_validate(
        {
            "requirements": {"added": [{"value": "150 seats", "basis": "stated"}]},
            "participants_added": ["john.t@614group.com"],
        }
    )
    context = apply_context_delta(context, delta_2, source_email_id="msg_002")

    delta_3 = ContextDelta.model_validate({"requirements": {"removed": ["100 seats"]}})
    context = apply_context_delta(context, delta_3, source_email_id="msg_003")

    assert [r.value for r in context.requirements] == ["150 seats"]
    assert context.participants == ["john.t@614group.com"]


# --- H. Large context with a very small delta (the actual scalability fix) ---


def _build_large_context() -> ThreadContext:
    # Mirrors the exact per-field item counts observed in the real 25-email run's thread
    # that hit the max_tokens truncation: 44 ProvenancedValue items across 7 of the 11
    # LIST_FIELDS, plus 5 participants (not itself a LIST_FIELD).
    def items(n: int, label: str) -> list[ProvenancedValue]:
        return [
            ProvenancedValue(
                value=f"{label} #{i} -- a realistically-sized sales-context fact, roughly this long",
                basis="stated",
                source_email_ids=[f"msg_{i:03d}"],
            )
            for i in range(n)
        ]

    return ThreadContext(
        participants=[f"person{i}@example.com" for i in range(5)],
        products_discussed=items(7, "product"),
        objections=items(2, "objection"),
        buying_signals=items(7, "buying signal"),
        commitments=items(9, "commitment"),
        open_questions=items(4, "open question"),
        next_actions=items(9, "next action"),
        meetings=items(6, "meeting"),
    )


def test_large_context_has_comparable_size_to_the_real_failure_case():
    large_context = _build_large_context()

    total_list_items = sum(len(getattr(large_context, field)) for field in LIST_FIELDS)
    context_json_size = len(json.dumps(large_context.model_dump()))

    assert total_list_items == 44
    assert context_json_size > 5000  # comparable order of magnitude to the real ~8668-char case


def test_tiny_delta_correctly_updates_a_large_context_without_reproducing_it():
    large_context = _build_large_context()
    before_json_size = len(json.dumps(large_context.model_dump()))

    # This is exactly what a real bounded LLM delta response looks like: one new fact,
    # nothing else -- not 44 echoed items.
    tiny_delta_raw = {"pain_points": {"added": [{"value": "New pain point from this email", "basis": "stated"}]}}
    tiny_delta_json_size = len(json.dumps(tiny_delta_raw))

    delta = ContextDelta.model_validate(tiny_delta_raw)
    result = apply_context_delta(large_context, delta, source_email_id="msg_050")

    # The delta the "LLM" had to produce is tiny regardless of how large the context is --
    # this is the structural fix: response size no longer scales with accumulated context.
    assert tiny_delta_json_size < 200
    assert tiny_delta_json_size < before_json_size / 10

    # Yet nothing from the large prior context was lost.
    for field in LIST_FIELDS:
        if field == "pain_points":
            continue
        assert getattr(result, field) == getattr(large_context, field)
    assert result.participants == large_context.participants
    assert len(result.pain_points) == 1
    assert result.pain_points[0].value == "New pain point from this email"


# --- I. Malformed/invalid delta must fail validation safely ---


def test_invalid_delta_with_wrapped_participants_fails_validation():
    # The exact shape that broke ThreadContext.participants in the real 25-email run --
    # ContextDelta must reject it too, not silently coerce it.
    with pytest.raises(ValidationError):
        ContextDelta.model_validate(
            {"participants_added": [{"value": "ashok@databeat.io", "basis": "stated", "source_email_ids": ["x"]}]}
        )


def test_invalid_delta_with_bare_strings_in_a_list_field_fails_validation():
    # requirements.added must be DeltaItem objects ({value, basis}), not bare strings.
    with pytest.raises(ValidationError):
        ContextDelta.model_validate({"requirements": {"added": ["100 seats"]}})


def test_invalid_delta_with_bad_basis_literal_fails_validation():
    with pytest.raises(ValidationError):
        ContextDelta.model_validate({"pain_points": {"added": [{"value": "Pricing", "basis": "maybe"}]}})


def test_invalid_delta_with_non_dict_dict_field_update_fails_validation():
    with pytest.raises(ValidationError):
        ContextDelta.model_validate({"pricing_updates": ["not", "a", "dict"]})


# --- J. Existing context behavior / regression cases ---


def test_regression_context_never_loses_information_the_delta_did_not_mention():
    # Same guarantee test_context_engine.py's tests already assert end-to-end via
    # build_next_context -- exercised here directly at the merge layer.
    previous = ThreadContext(
        competitors=[ProvenancedValue(value="Salesforce", basis="stated", source_email_ids=["msg_001"])]
    )
    empty_delta = ContextDelta()

    result = apply_context_delta(previous, empty_delta, source_email_id="msg_002")

    assert result.competitors == previous.competitors


def test_regression_dict_field_key_removal_via_null_value():
    previous = ThreadContext(opportunity={"stage": "discovery", "amount": "$50K"})
    delta = ContextDelta.model_validate({"opportunity_updates": {"amount": None, "stage": "negotiation"}})

    result = apply_context_delta(previous, delta, source_email_id="msg_003")

    assert result.opportunity == {"stage": "negotiation"}
