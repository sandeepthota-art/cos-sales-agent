from datetime import datetime, timedelta, timezone

import mongomock
import pytest

from app.database.indexes import initialize_indexes
from app.database.repositories import PersonalItemRepository
from app.reminders import ReminderScheduler

_NOW = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


def _personal_item(item_id, due, status="open", **overrides):
    doc = {
        "id": item_id,
        "type": "reminder",
        "description": "follow up if no response",
        "date_or_deadline": due,
        "status": status,
    }
    doc.update(overrides)
    return doc


def test_triggers_a_due_open_item_and_marks_it_reminded(db):
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        _personal_item("PSN-001", (_NOW - timedelta(minutes=5)).isoformat()),
    )
    scheduler = ReminderScheduler(db)

    triggered = scheduler.run_once(now=_NOW)

    assert [t["id"] for t in triggered] == ["PSN-001"]
    stored = PersonalItemRepository(db).find_one({"id": "PSN-001"})
    assert stored["status"] == "reminded"


def test_does_not_trigger_an_item_not_yet_due(db):
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        _personal_item("PSN-001", (_NOW + timedelta(hours=1)).isoformat()),
    )
    scheduler = ReminderScheduler(db)

    triggered = scheduler.run_once(now=_NOW)

    assert triggered == []
    assert PersonalItemRepository(db).find_one({"id": "PSN-001"})["status"] == "open"


def test_does_not_re_trigger_an_already_reminded_item(db):
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        _personal_item("PSN-001", (_NOW - timedelta(hours=1)).isoformat(), status="reminded"),
    )
    scheduler = ReminderScheduler(db)

    triggered = scheduler.run_once(now=_NOW)

    assert triggered == []


def test_two_poll_cycles_never_trigger_the_same_item_twice(db):
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        _personal_item("PSN-001", (_NOW - timedelta(minutes=1)).isoformat()),
    )
    scheduler = ReminderScheduler(db)

    first = scheduler.run_once(now=_NOW)
    second = scheduler.run_once(now=_NOW + timedelta(minutes=1))

    assert len(first) == 1
    assert second == []


def test_skips_an_item_with_no_date_or_deadline(db):
    PersonalItemRepository(db).upsert_by_key({"id": "PSN-001"}, _personal_item("PSN-001", None))
    scheduler = ReminderScheduler(db)

    assert scheduler.run_once(now=_NOW) == []


def test_skips_an_item_with_unparseable_date_without_crashing(db):
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"}, _personal_item("PSN-001", "not-a-real-date")
    )
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-002"},
        _personal_item("PSN-002", (_NOW - timedelta(minutes=1)).isoformat()),
    )
    scheduler = ReminderScheduler(db)

    triggered = scheduler.run_once(now=_NOW)

    assert [t["id"] for t in triggered] == ["PSN-002"]


def test_handles_a_zulu_suffixed_timestamp(db):
    due = (_NOW - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    PersonalItemRepository(db).upsert_by_key({"id": "PSN-001"}, _personal_item("PSN-001", due))
    scheduler = ReminderScheduler(db)

    triggered = scheduler.run_once(now=_NOW)

    assert [t["id"] for t in triggered] == ["PSN-001"]


def test_run_once_returns_empty_and_does_not_raise_when_lookup_fails(db, monkeypatch):
    scheduler = ReminderScheduler(db)

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated mongo failure")

    monkeypatch.setattr(scheduler._repo, "find_many", _raise)

    assert scheduler.run_once(now=_NOW) == []


class _StopLoop(Exception):
    pass


def test_run_forever_sleeps_for_the_exact_interval_and_survives_exceptions(db, monkeypatch):
    import app.reminders as reminders_module

    calls = []

    def fake_run_once(self, now=None):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated poll cycle failure")
        return []

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise _StopLoop

    monkeypatch.setattr(reminders_module.ReminderScheduler, "run_once", fake_run_once)
    monkeypatch.setattr(reminders_module.time, "sleep", fake_sleep)
    scheduler = ReminderScheduler(db)

    with pytest.raises(_StopLoop):
        scheduler.run_forever(interval_seconds=42)

    assert len(calls) == 2  # survived the first cycle's exception
    assert sleep_calls == [42, 42]
