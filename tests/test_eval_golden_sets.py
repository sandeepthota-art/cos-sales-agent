"""Phase 5 golden-set structural tests -- no LLM, no network. Validates
eval/golden/labeling_cases.jsonl and eval/golden/context_cases.jsonl
parse/validate, cover the required categories/labels, use only reserved
domains, and have no duplicate case_ids. Also covers the eval-database
isolation guard and load_golden.py's dry-run/idempotency."""

import sys
from pathlib import Path

import mongomock
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.db import initialize_indexes  # noqa: E402
from eval.eval_models import (  # noqa: E402
    ALL_LABELS,
    REQUIRED_LABELING_CATEGORIES,
    is_reserved_domain,
    load_context_cases,
    load_labeling_cases,
)

LABELING_PATH = ROOT / "eval" / "golden" / "labeling_cases.jsonl"
CONTEXT_PATH = ROOT / "eval" / "golden" / "context_cases.jsonl"


@pytest.fixture
def labeling_cases():
    return load_labeling_cases(LABELING_PATH)


@pytest.fixture
def context_cases():
    return load_context_cases(CONTEXT_PATH)


def test_labeling_cases_parse_and_validate(labeling_cases):
    assert 40 <= len(labeling_cases) <= 60


def test_context_cases_parse_and_validate(context_cases):
    assert 15 <= len(context_cases) <= 25


def test_all_six_labels_appear_at_least_three_times(labeling_cases):
    counts = {}
    for case in labeling_cases:
        counts[case.expected_label] = counts.get(case.expected_label, 0) + 1
    for label in ALL_LABELS:
        assert counts.get(label, 0) >= 3, f"{label} appears only {counts.get(label, 0)} times"


def test_every_required_category_is_present(labeling_cases):
    present = {case.category for case in labeling_cases}
    missing = REQUIRED_LABELING_CATEGORIES - present
    assert not missing, f"missing categories: {missing}"


def test_no_case_id_duplicates_in_labeling_cases(labeling_cases):
    ids = [case.case_id for case in labeling_cases]
    assert len(ids) == len(set(ids))


def test_no_case_id_duplicates_in_context_cases(context_cases):
    ids = [case.case_id for case in context_cases]
    assert len(ids) == len(set(ids))


def test_every_labeling_address_uses_a_reserved_domain(labeling_cases):
    for case in labeling_cases:
        assert is_reserved_domain(case.owner_email), case.case_id
        for message in case.messages:
            addrs = [message.from_] + message.to + message.cc
            for a in addrs:
                assert is_reserved_domain(a.email), f"{case.case_id}: {a.email}"


def test_every_context_address_uses_a_reserved_domain(context_cases):
    for case in context_cases:
        assert is_reserved_domain(case.owner_email), case.case_id
        for message in case.messages:
            addrs = [message.from_] + message.to + message.cc
            for a in addrs:
                assert is_reserved_domain(a.email), f"{case.case_id}: {a.email}"


def test_is_reserved_domain_rejects_real_looking_domains():
    assert not is_reserved_domain("someone@realcompany.com")
    assert not is_reserved_domain("someone@gmail.com")


def test_is_reserved_domain_accepts_the_reserved_set():
    assert is_reserved_domain("a@example.com")
    assert is_reserved_domain("a@b.example")
    assert is_reserved_domain("a@b.test")
    assert is_reserved_domain("a@b.invalid")


# ---------------------------------------------------------------------------
# Isolation guard
# ---------------------------------------------------------------------------


def test_eval_database_guard_refuses_non_eval_database_name():
    from _common import require_eval_database

    with pytest.raises(RuntimeError):
        require_eval_database("cos_sales")


def test_eval_database_guard_accepts_local_uri_with_eval_database():
    from _common import require_eval_database

    require_eval_database("raw_dump_eval", uri="mongodb://localhost:27017")  # must not raise
    require_eval_database("raw_dump_eval", uri="mongodb://127.0.0.1:27017")  # must not raise
    require_eval_database("raw_dump_eval", uri="mongodb://[::1]:27017")  # must not raise


def test_eval_database_guard_refuses_non_local_host():
    from _common import require_eval_database

    with pytest.raises(RuntimeError, match="non-local host refused"):
        require_eval_database("raw_dump_eval", uri="mongodb+srv://user:secretpass@cluster0.example.net/?appName=x")


def test_eval_database_guard_accepts_non_local_host_with_flag():
    from _common import require_eval_database

    require_eval_database(
        "raw_dump_eval", uri="mongodb+srv://user:secretpass@cluster0.example.net/?appName=x", allow_remote=True
    )  # must not raise


def test_eval_database_guard_refuses_non_eval_database_regardless_of_host():
    from _common import require_eval_database

    with pytest.raises(RuntimeError):
        require_eval_database("cos_sales", uri="mongodb://localhost:27017")
    with pytest.raises(RuntimeError):
        require_eval_database("cos_sales", uri="mongodb://localhost:27017", allow_remote=True)


def test_eval_database_guard_never_prints_uri_or_credentials_on_refusal():
    from _common import require_eval_database

    secret_uri = "mongodb+srv://topsecretuser:topsecretpass@cluster0.example.net/?appName=x"
    with pytest.raises(RuntimeError) as excinfo:
        require_eval_database("raw_dump_eval", uri=secret_uri)

    message = str(excinfo.value)
    assert message == "non-local host refused"
    assert "topsecretuser" not in message
    assert "topsecretpass" not in message
    assert "cluster0.example.net" not in message


def test_eval_database_guard_accepts_eval_suffixed_name():
    from _common import require_eval_database

    require_eval_database("raw_dump_eval")  # must not raise


# ---------------------------------------------------------------------------
# load_golden.py
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    database = mongomock.MongoClient().db
    initialize_indexes(database)
    return database


def test_load_golden_dry_run_reports_right_counts(labeling_cases, context_cases):
    from load_golden import collect_messages

    labeling_messages = collect_messages(labeling_cases)
    context_messages = collect_messages(context_cases)

    assert len(labeling_messages) == sum(len(c.messages) for c in labeling_cases)
    assert len(context_messages) == sum(len(c.messages) for c in context_cases)


def test_ingesting_golden_cases_is_idempotent(db, labeling_cases):
    from app.config import Settings
    from load_golden import collect_messages, load_messages

    settings = Settings(agent_email="owner@ourcompany.example")
    messages = collect_messages(labeling_cases[:5])

    first_count = load_messages(db, settings, messages)
    total_after_first = db.raw_emails_dump.count_documents({})
    second_count = load_messages(db, settings, messages)
    total_after_second = db.raw_emails_dump.count_documents({})

    assert first_count == second_count == len(messages)
    assert total_after_first == total_after_second == len(messages)
