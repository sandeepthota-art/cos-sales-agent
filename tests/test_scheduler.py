import mongomock
import pytest

from app.config.settings import Settings
from app.database.indexes import initialize_indexes
from app.interfaces.source import Source
from app.providers.calendar.mock import MockCalendarProvider
from app.providers.llm.mock import MockLLMProvider
from app.scheduler import Scheduler
import app.scheduler as scheduler_module


@pytest.fixture
def db():
    client = mongomock.MongoClient()
    database = client["cos_sales_test"]
    initialize_indexes(database)
    return database


@pytest.fixture
def settings():
    return Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock")


def _raw_email(message_id, body="We currently use Salesforce.", **overrides):
    raw = {
        "message_id": message_id,
        "from": {"name": "John", "email": "john@example.com"},
        "to": [{"name": "Ashok", "email": "ashok@example.com"}],
        "subject": "Enterprise CRM Proposal",
        "body": body,
        "timestamp": "2026-09-13T10:30:00Z",
    }
    raw.update(overrides)
    return raw


class _FakeSource(Source):
    def __init__(self, batches):
        self._batches = batches
        self.marked: list[tuple] = []
        self.discovery_error: Exception | None = None

    def get_new_batches(self):
        if self.discovery_error:
            raise self.discovery_error
        return self._batches

    def mark_batch_processed(self, source_ref, failed_count):
        self.marked.append((source_ref, failed_count))


class _StopLoop(Exception):
    pass


def test_run_once_processes_a_discovered_batch_through_the_real_pipeline(db, settings):
    source = _FakeSource([{"source_ref": "batch-1", "emails": [_raw_email("m1")]}])
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert len(summaries) == 1
    assert summaries[0].completed == 1
    assert db.emails.count_documents({"message_id": "m1"}) == 1
    assert source.marked == [("batch-1", 0)]


def test_run_once_returns_empty_list_when_no_new_items(db, settings):
    source = _FakeSource([])
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    assert scheduler.run_once() == []


def test_run_once_overrides_email_limit_so_a_large_batch_is_not_truncated(db):
    settings = Settings(email_provider="mock", calendar_provider="mock", llm_provider="mock", email_limit=1)
    emails = [_raw_email(f"m{i}") for i in range(3)]
    source = _FakeSource([{"source_ref": "batch-1", "emails": emails}])
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert summaries[0].processed == 3
    assert db.emails.count_documents({}) == 3


def test_run_once_does_not_mark_batch_processed_when_pipeline_run_raises(db, settings, monkeypatch):
    def _raise(*args, **kwargs):
        raise RuntimeError("simulated transport error")

    monkeypatch.setattr(scheduler_module, "run_pipeline", _raise)
    source = _FakeSource([{"source_ref": "batch-1", "emails": [_raw_email("m1")]}])
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert summaries == []
    assert source.marked == []


def test_run_once_continues_remaining_batches_after_one_batch_raises(db, settings, monkeypatch):
    real_run_pipeline = scheduler_module.run_pipeline
    calls = []

    def _flaky(db_, provider, llm, calendar, settings_):
        calls.append(provider)
        if len(calls) == 1:
            raise RuntimeError("simulated transport error")
        return real_run_pipeline(db_, provider, llm, calendar, settings_)

    monkeypatch.setattr(scheduler_module, "run_pipeline", _flaky)
    source = _FakeSource(
        [
            {"source_ref": "batch-1", "emails": [_raw_email("m1")]},
            {"source_ref": "batch-2", "emails": [_raw_email("m2")]},
        ]
    )
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    summaries = scheduler.run_once()

    assert len(summaries) == 1
    assert source.marked == [("batch-2", 0)]
    assert db.emails.count_documents({"message_id": "m2"}) == 1
    assert db.emails.count_documents({"message_id": "m1"}) == 0


def test_run_once_returns_empty_and_does_not_raise_when_source_discovery_raises(db, settings):
    source = _FakeSource([])
    source.discovery_error = RuntimeError("folder unreadable")
    scheduler = Scheduler(db, source, MockLLMProvider(), MockCalendarProvider(), settings)

    assert scheduler.run_once() == []


def test_run_forever_calls_run_once_then_sleeps_each_iteration_in_strict_order(db, settings, monkeypatch):
    order = []

    def fake_run_once(self):
        order.append("run_once")
        return []

    def fake_sleep(seconds):
        order.append("sleep")
        if order.count("sleep") >= 3:
            raise _StopLoop

    monkeypatch.setattr(scheduler_module.Scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler_module.time, "sleep", fake_sleep)
    scheduler = Scheduler(db, _FakeSource([]), MockLLMProvider(), MockCalendarProvider(), settings)

    with pytest.raises(_StopLoop):
        scheduler.run_forever(interval_seconds=1)

    assert order == ["run_once", "sleep", "run_once", "sleep", "run_once", "sleep"]


def test_run_forever_survives_a_poll_cycle_exception_and_continues(db, settings, monkeypatch):
    calls = []

    def fake_run_once(self):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("simulated poll cycle failure")
        return []

    sleep_calls = []

    def fake_sleep(seconds):
        sleep_calls.append(seconds)
        if len(sleep_calls) >= 2:
            raise _StopLoop

    monkeypatch.setattr(scheduler_module.Scheduler, "run_once", fake_run_once)
    monkeypatch.setattr(scheduler_module.time, "sleep", fake_sleep)
    scheduler = Scheduler(db, _FakeSource([]), MockLLMProvider(), MockCalendarProvider(), settings)

    with pytest.raises(_StopLoop):
        scheduler.run_forever(interval_seconds=1)

    assert len(calls) == 2  # the loop continued after the first call's exception


def test_run_forever_sleeps_for_the_exact_interval_passed_in(db, settings, monkeypatch):
    monkeypatch.setattr(scheduler_module.Scheduler, "run_once", lambda self: [])
    captured = []

    def fake_sleep(seconds):
        captured.append(seconds)
        raise _StopLoop

    monkeypatch.setattr(scheduler_module.time, "sleep", fake_sleep)
    scheduler = Scheduler(db, _FakeSource([]), MockLLMProvider(), MockCalendarProvider(), settings)

    with pytest.raises(_StopLoop):
        scheduler.run_forever(interval_seconds=123)

    assert captured == [123]
