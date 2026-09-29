from datetime import datetime, timedelta, timezone

from app.email.models import parse_email
from app.email.threading import ThreadCandidate, resolve_thread_id


def _email(**overrides):
    raw = {
        "message_id": "msg_002",
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Re: Enterprise CRM Proposal",
        "body": "Following up...",
        "timestamp": "2026-09-14T10:30:00Z",
    }
    raw.update(overrides)
    return parse_email(raw)


def test_reuses_existing_thread_when_provider_thread_id_matches_a_candidate():
    candidate = ThreadCandidate(
        thread_id="THR-001",
        source_thread_id="thread_provider_123",
        normalized_subject="Unrelated",
        participant_emails=set(),
        source_message_ids=set(),
        last_message_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    email = _email(thread_id="thread_provider_123")
    assert resolve_thread_id(email, candidates=[candidate]) == "THR-001"


def test_provider_thread_id_with_no_matching_candidate_returns_none_for_fresh_allocation():
    # A genuinely new Gmail thread never seen before -- the caller
    # (app.pipeline._upsert_thread) allocates a fresh THR-nnn; this function
    # never trusts the raw provider value as canonical identity on its own,
    # and never falls through to heuristic matching once a source_thread_id
    # was supplied (mirrors the old code's "trust it absolutely" behavior,
    # just pointed at canonical ids instead of raw ones).
    email = _email(thread_id="thread_provider_123")
    assert resolve_thread_id(email, candidates=[]) is None


def test_matches_via_in_reply_to():
    candidate = ThreadCandidate(
        thread_id="THR-001",
        normalized_subject="Unrelated Subject",
        participant_emails={"john@example.com", "ashok@example.com"},
        source_message_ids={"msg_001"},
        last_message_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    email = _email(in_reply_to="msg_001")
    assert resolve_thread_id(email, candidates=[candidate]) == "THR-001"


def test_matches_via_references():
    candidate = ThreadCandidate(
        thread_id="THR-001",
        normalized_subject="Unrelated",
        participant_emails={"john@example.com"},
        source_message_ids={"msg_000", "msg_001"},
        last_message_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    email = _email(references=["msg_000"])
    assert resolve_thread_id(email, candidates=[candidate]) == "THR-001"


def test_matches_via_subject_and_participant_overlap():
    candidate = ThreadCandidate(
        thread_id="THR-001",
        normalized_subject="Enterprise CRM Proposal",
        participant_emails={"john@example.com", "ashok@example.com"},
        source_message_ids={"msg_001"},
        last_message_at=datetime(2026, 9, 13, tzinfo=timezone.utc),
    )
    email = _email()  # subject normalizes to same text, no in_reply_to/references
    assert resolve_thread_id(email, candidates=[candidate]) == "THR-001"


def test_falls_back_to_participant_overlap_within_window():
    candidate = ThreadCandidate(
        thread_id="THR-001",
        normalized_subject="Totally Different Subject",
        participant_emails={"john@example.com", "ashok@example.com"},
        source_message_ids={"msg_001"},
        last_message_at=datetime(2026, 9, 14, 9, 0, tzinfo=timezone.utc),
    )
    email = _email(subject="A New Subject Entirely")
    assert resolve_thread_id(email, candidates=[candidate], window_days=14) == "THR-001"


def test_participant_overlap_outside_window_creates_new_thread():
    old_time = datetime(2026, 9, 14, 10, 30, tzinfo=timezone.utc) - timedelta(days=20)
    candidate = ThreadCandidate(
        thread_id="THR-001",
        normalized_subject="Totally Different Subject",
        participant_emails={"john@example.com", "ashok@example.com"},
        source_message_ids={"msg_001"},
        last_message_at=old_time,
    )
    email = _email(subject="A New Subject Entirely")
    result = resolve_thread_id(email, candidates=[candidate], window_days=14)
    assert result is None


def test_no_match_returns_none_for_fresh_allocation():
    # Canonical ID refactor: no synthetic thread_{message_id} fallback anymore --
    # a genuinely new thread with no matching signal returns None, and the caller
    # allocates a real THR-nnn via the same atomic counter every other internal
    # id uses.
    email = _email()
    result = resolve_thread_id(email, candidates=[])
    assert result is None
