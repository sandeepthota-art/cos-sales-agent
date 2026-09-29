# app/knowledge/deduplication.py
from datetime import datetime
from typing import Literal

from rapidfuzz import fuzz

from app.interfaces.llm_provider import LLMProvider
from app.knowledge.models import HistoryEntry, KnowledgeItem
from app.knowledge.normalize import classify_fact_key, extract_leading_number, normalize_text, slugify

_EXACT_MATCH_THRESHOLD = 90
_AMBIGUOUS_LOWER_BOUND = 60

# fact_keys whose value is fully captured by a leading quantity (e.g. "100" in "100 seats"
# means the same thing regardless of surrounding words). The numeric-equality carve-out in
# _apply_update only applies to these -- for every other fact_key (close_date, budget,
# pricing_tier, contract_length, and all set-membership fallback keys) a number is only part
# of the value's meaning, so two texts sharing a coincidental digit (e.g. "Nov 1, 2026" vs
# "Jan 1, 2026") must NOT be treated as unchanged.
_QUANTITY_FACT_KEYS = {"seat_count"}


def _find_exact(
    items: list[KnowledgeItem], thread_id: str, subject_key: str, predicate: str, fact_key: str
) -> KnowledgeItem | None:
    for item in items:
        if (
            item.thread_id == thread_id
            and item.subject_key == subject_key
            and item.predicate == predicate
            and item.fact_key == fact_key
        ):
            return item
    return None


def _find_fuzzy_candidates(
    items: list[KnowledgeItem], thread_id: str, subject_key: str, predicate: str, object_text: str
) -> list[tuple[KnowledgeItem, float]]:
    normalized_new = normalize_text(object_text)
    candidates = []
    for item in items:
        if item.thread_id != thread_id or item.subject_key != subject_key or item.predicate != predicate:
            continue
        score = fuzz.token_sort_ratio(normalize_text(item.current_value), normalized_new)
        candidates.append((item, score))
    candidates.sort(key=lambda pair: pair[1], reverse=True)
    return candidates


def _apply_update(item: KnowledgeItem, object_text: str, source_email_id: str, now: datetime) -> KnowledgeItem:
    updated_source_emails = list(dict.fromkeys([*item.source_emails, source_email_id]))
    new_value_normalized = normalize_text(object_text)
    current_value_normalized = normalize_text(item.current_value)

    new_number = extract_leading_number(object_text)
    current_number = extract_leading_number(item.current_value)

    is_unchanged_quantity = (
        item.fact_key in _QUANTITY_FACT_KEYS
        and new_number is not None
        and current_number is not None
        and new_number == current_number
    )
    value_changed = new_value_normalized != current_value_normalized and not is_unchanged_quantity

    history = list(item.history)
    current_value = item.current_value
    if value_changed:
        history.append(HistoryEntry(value=object_text, source_email_id=source_email_id, recorded_at=now))
        current_value = object_text

    return item.model_copy(
        update={
            "current_value": current_value,
            "history": history,
            "source_emails": updated_source_emails,
            "last_confirmed_at": now,
            "confidence": min(0.99, item.confidence + 0.01),
        }
    )


def process_new_fact(
    items: list[KnowledgeItem],
    thread_id: str,
    subject: str,
    predicate: str,
    object_text: str,
    source_email_id: str,
    basis: Literal["stated", "inferred"],
    llm: LLMProvider,
    now: datetime,
    person_id: str | None = None,
    org_id: str | None = None,
) -> tuple[list[KnowledgeItem], KnowledgeItem]:
    """person_id/org_id (optional): EXPLICIT canonical attribution, set by the
    caller when it already knows who this fact is about (see
    app.pipeline._process_person_facts) -- never re-derived here. Set only on a
    brand-new KnowledgeItem; an existing item matched by (thread_id, subject_key,
    predicate, fact_key) already carries whatever attribution it was created
    with, which this function never overwrites."""
    subject_key = slugify(subject)
    fact_key = classify_fact_key(predicate, object_text)

    exact_match = _find_exact(items, thread_id, subject_key, predicate, fact_key)
    if exact_match is not None:
        updated = _apply_update(exact_match, object_text, source_email_id, now)
        new_items = [updated if i.knowledge_id == updated.knowledge_id else i for i in items]
        return new_items, updated

    candidates = _find_fuzzy_candidates(items, thread_id, subject_key, predicate, object_text)
    target: KnowledgeItem | None = None

    if candidates and candidates[0][1] >= _EXACT_MATCH_THRESHOLD:
        target = candidates[0][0]
    elif candidates and _AMBIGUOUS_LOWER_BOUND <= candidates[0][1] < _EXACT_MATCH_THRESHOLD:
        best_item, _ = candidates[0]
        if llm.verify_same_fact(best_item.current_value, object_text, subject, predicate):
            target = best_item

    if target is not None:
        updated = _apply_update(target, object_text, source_email_id, now)
        new_items = [updated if i.knowledge_id == updated.knowledge_id else i for i in items]
        return new_items, updated

    new_item = KnowledgeItem(
        knowledge_id=f"knowledge_{thread_id}_{subject_key}_{predicate}_{fact_key}",
        thread_id=thread_id,
        subject_key=subject_key,
        predicate=predicate,
        fact_key=fact_key,
        current_value=object_text,
        person_id=person_id,
        org_id=org_id,
        history=[HistoryEntry(value=object_text, source_email_id=source_email_id, recorded_at=now)],
        source_emails=[source_email_id],
        basis=basis,
        first_seen_at=now,
        last_confirmed_at=now,
        confidence=0.75,
    )
    return [*items, new_item], new_item
