import json

from pydantic import ValidationError

from app.analysis.schemas import EmailAnalysis
from app.context.diff import diff_context
from app.context.models import (
    DICT_DELTA_FIELDS,
    LIST_FIELDS,
    ContextChange,
    ContextDelta,
    ListFieldDelta,
    ProvenancedValue,
    ThreadContext,
)
from app.interfaces.llm_provider import LLMProvider


def _merge_plain_string_list(existing: list[str], added: list[str], removed: list[str]) -> list[str]:
    removed_set = set(removed)
    kept = [value for value in existing if value not in removed_set]
    seen = set(kept)
    for value in added:
        if value not in seen:
            kept.append(value)
            seen.add(value)
    return kept


def _merge_provenanced_list(
    existing: list[ProvenancedValue], field_delta: ListFieldDelta, source_email_id: str
) -> list[ProvenancedValue]:
    removed_set = set(field_delta.removed)
    kept = [item for item in existing if item.value not in removed_set]
    seen = {item.value for item in kept}
    for new_item in field_delta.added:
        if new_item.value not in seen:
            kept.append(
                ProvenancedValue(value=new_item.value, basis=new_item.basis, source_email_ids=[source_email_id])
            )
            seen.add(new_item.value)
    return kept


def _apply_dict_updates(existing: dict, updates: dict) -> dict:
    result = dict(existing)
    for key, value in updates.items():
        if value is None:
            result.pop(key, None)
        else:
            result[key] = value
    return result


def apply_context_delta(previous: ThreadContext, delta: ContextDelta, source_email_id: str) -> ThreadContext:
    """Deterministically applies a bounded ContextDelta to the previous ThreadContext.

    Never drops existing information the delta didn't mention (an empty/omitted field
    on the delta means "no change"), and the LLM never has to reproduce anything it
    isn't actually changing -- this is what keeps update_context's response bounded by
    the current email instead of growing with the thread's whole history.
    """
    next_context = previous.model_copy(deep=True)

    if delta.summary:
        next_context.summary = delta.summary

    next_context.participants = _merge_plain_string_list(
        previous.participants, delta.participants_added, delta.participants_removed
    )

    for dict_field, delta_attr in DICT_DELTA_FIELDS.items():
        updates = getattr(delta, delta_attr)
        setattr(next_context, dict_field, _apply_dict_updates(getattr(previous, dict_field), updates))

    for field in LIST_FIELDS:
        field_delta: ListFieldDelta = getattr(delta, field)
        merged = _merge_provenanced_list(getattr(previous, field), field_delta, source_email_id)
        setattr(next_context, field, merged)

    return next_context


def build_next_context(
    previous: ThreadContext | None,
    analysis: EmailAnalysis,
    source_email_id: str,
    llm: LLMProvider,
    max_retries: int = 1,
) -> tuple[ThreadContext, list[ContextChange]]:
    previous_context = previous or ThreadContext()

    # Same retry treatment as app.analysis.extractor.analyze_email_with_validation, for
    # the same reason: a malformed Claude response (JSONDecodeError) or a response that
    # doesn't match ContextDelta's shape (ValidationError) previously had NO retry at all
    # here, unlike analyze_email. One extra attempt, same default budget -- not a new
    # retry mechanism, just extending the existing pattern to this call site too.
    delta: ContextDelta | None = None
    last_error: Exception | None = None
    for _ in range(max_retries + 1):
        try:
            raw_delta = llm.update_context(previous_context.model_dump(), analysis.model_dump())
            delta = ContextDelta.model_validate(raw_delta)
            break
        except (json.JSONDecodeError, ValidationError) as exc:
            last_error = exc

    if delta is None:
        raise last_error

    next_context = apply_context_delta(previous_context, delta, source_email_id)

    changes = diff_context(previous_context, next_context, source_email_id=source_email_id)
    return next_context, changes
