# tests/test_query_schemas.py
"""Phase 22A: QueryRequest and related contract validation."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.query.schemas import QueryFilters, QueryIntentType, QueryRequest


def test_query_request_requires_text_and_reference_datetime():
    request = QueryRequest(text="What meetings do I have today?", reference_datetime=datetime(2026, 3, 12, tzinfo=timezone.utc))
    assert request.timezone == "UTC"  # default
    assert request.filters.limit == 50  # default


def test_query_request_rejects_missing_text():
    with pytest.raises(ValidationError):
        QueryRequest(reference_datetime=datetime(2026, 3, 12, tzinfo=timezone.utc))


def test_query_request_rejects_missing_reference_datetime():
    with pytest.raises(ValidationError):
        QueryRequest(text="hello")


def test_query_filters_default_limit_is_bounded_and_positive():
    filters = QueryFilters()
    assert filters.limit > 0


def test_query_intent_type_rejects_unknown_values():
    with pytest.raises(ValueError):
        QueryIntentType("not_a_real_intent")
