# tests/test_knowledge_deduplication.py
from datetime import datetime, timezone

from rapidfuzz import fuzz

from app.interfaces.llm_provider import LLMProvider
from app.knowledge.deduplication import process_new_fact
from app.knowledge.normalize import classify_fact_key, normalize_text
from app.providers.llm.mock import MockLLMProvider


def _now():
    return datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc)


class _StubLLM(LLMProvider):
    """Minimal LLMProvider stub with a controllable verify_same_fact answer,
    for exercising Step 4's ambiguous-band branch independent of
    MockLLMProvider's own internal similarity threshold."""

    def __init__(self, verify_result: bool):
        self._verify_result = verify_result

    def analyze_email(self, email, thread_history=None, person_context=None):
        raise NotImplementedError

    def update_context(self, previous_context, new_analysis):
        raise NotImplementedError

    def verify_same_fact(self, existing_value: str, new_value: str, subject: str, predicate: str) -> bool:
        return self._verify_result

    def draft_reply(self, context, latest_email):
        raise NotImplementedError


def test_first_mention_creates_new_knowledge_item():
    llm = MockLLMProvider()
    items, item = process_new_fact(
        items=[],
        thread_id="thread_001",
        subject="ABC Corp",
        predicate="requires",
        object_text="100 seats",
        source_email_id="msg_001",
        basis="stated",
        llm=llm,
        now=_now(),
    )
    assert len(items) == 1
    assert item.subject_key == "abc_corp"
    assert item.fact_key == "seat_count"
    assert item.current_value == "100 seats"
    assert item.history[0].value == "100 seats"
    assert item.source_emails == ["msg_001"]


def test_exact_repeat_updates_confidence_without_duplicating():
    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="100 seats", source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="100 seats", source_email_id="msg_005", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1
    assert set(item.source_emails) == {"msg_001", "msg_005"}
    assert len(item.history) == 1  # no new history entry for an unchanged value


def test_changed_value_updates_current_value_and_preserves_history():
    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="100 seats", source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="150 seats", source_email_id="msg_007", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1  # same knowledge item, not a duplicate
    assert item.current_value == "150 seats"
    assert [h.value for h in item.history] == ["100 seats", "150 seats"]
    assert item.status == "active"


def test_similar_phrasing_of_same_fact_is_deduplicated_via_rapidfuzz():
    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="100 seats", source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="requires",
        object_text="approximately 100 users", source_email_id="msg_005", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1
    assert "msg_005" in item.source_emails


def test_distinct_competitors_create_separate_knowledge_items():
    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="mentioned_competitor",
        object_text="Salesforce", source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, _ = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="mentioned_competitor",
        object_text="HubSpot", source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 2
    fact_keys = {item.fact_key for item in items}
    assert fact_keys == {"salesforce", "hubspot"}


def test_unrelated_statements_do_not_merge_despite_text_similarity():
    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text="Salesforce", source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, _ = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="evaluating",
        object_text="Salesforce replacement", source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    # different predicate -> never the same knowledge item regardless of text similarity
    assert len(items) == 2


def test_step3_auto_merges_differing_fact_keys_at_high_fuzzy_similarity():
    # Same predicate, but the fact_key classifier assigns these two phrasings different
    # keys (a typo breaks the shared slug). rapidfuzz still scores them >= 90, so Step 3's
    # auto-merge band should catch this without ever consulting the LLM.
    a, b = "Acme Cloud Platform", "Acme Cloud Platfrom"
    assert classify_fact_key("uses", a) != classify_fact_key("uses", b)
    score = fuzz.token_sort_ratio(normalize_text(a), normalize_text(b))
    assert score >= 90

    llm = MockLLMProvider()
    items, item1 = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=a, source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item2 = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=b, source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1
    assert item2.knowledge_id == item1.knowledge_id
    assert [h.value for h in item2.history] == [a, b]


def test_step4_llm_confirms_ambiguous_band_merge():
    # Same predicate, differing fact_key, and rapidfuzz score lands in the 60-90 ambiguous
    # band -- Step 4 must consult the LLM. A stub that answers True should merge.
    a, b = "an old CRM tool", "an old CRM system"
    assert classify_fact_key("uses", a) != classify_fact_key("uses", b)
    score = fuzz.token_sort_ratio(normalize_text(a), normalize_text(b))
    assert 60 <= score < 90

    llm = _StubLLM(verify_result=True)
    items, item1 = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=a, source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item2 = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=b, source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1
    assert item2.knowledge_id == item1.knowledge_id


def test_step4_llm_rejects_ambiguous_band_stays_separate():
    # Same pair, same ambiguous-band score, but a stub that answers False must NOT merge --
    # the ambiguity is resolved against merging, so two distinct knowledge items remain.
    a, b = "an old CRM tool", "an old CRM system"
    score = fuzz.token_sort_ratio(normalize_text(a), normalize_text(b))
    assert 60 <= score < 90

    llm = _StubLLM(verify_result=False)
    items, item1 = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=a, source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item2 = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="uses",
        object_text=b, source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 2
    assert item2.knowledge_id != item1.knowledge_id


def test_close_date_change_is_recorded_despite_sharing_a_coincidental_digit():
    # Regression: the numeric-equality carve-out in _apply_update must be restricted to
    # quantity-shaped fact_keys (seat_count). "close date of Nov 1, 2026" and "close date of
    # Jan 1, 2026" both extract a leading/first digit of "1" (the day-of-month) -- if the
    # carve-out applied here, the month change (the actual contradiction) would be silently
    # swallowed as an "unchanged" value instead of being recorded in history.
    a, b = "close date of Nov 1, 2026", "close date of Jan 1, 2026"
    assert classify_fact_key("targets", a) == "close_date"
    assert classify_fact_key("targets", b) == "close_date"

    llm = MockLLMProvider()
    items, _ = process_new_fact(
        items=[], thread_id="thread_001", subject="ABC Corp", predicate="targets",
        object_text=a, source_email_id="msg_001", basis="stated", llm=llm, now=_now(),
    )
    items, item = process_new_fact(
        items=items, thread_id="thread_001", subject="ABC Corp", predicate="targets",
        object_text=b, source_email_id="msg_002", basis="stated", llm=llm, now=_now(),
    )
    assert len(items) == 1  # same knowledge item, exact fact_key match
    assert item.current_value == b
    assert [h.value for h in item.history] == [a, b]
