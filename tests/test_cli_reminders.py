from datetime import datetime, timedelta, timezone

import mongomock
import pytest

import app.reminders as reminders_module
from app.database.repositories import PersonalItemRepository


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(reminders_module, "get_client", lambda uri: fake_client)
    yield fake_client


def test_reminders_cli_accepts_once_flag():
    args = reminders_module.build_arg_parser().parse_args(["--once"])
    assert args.once is True


def test_reminders_cli_once_triggers_a_due_item_and_exits(monkeypatch, _patch_mongo_client):
    from app.config.settings import Settings

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        {"id": "PSN-001", "type": "reminder", "description": "check in", "date_or_deadline": due, "status": "open"},
    )

    exit_code = reminders_module.main(["--once"])

    assert exit_code == 0
    assert db.personal_items.find_one({"id": "PSN-001"})["status"] == "reminded"


def test_reminders_cli_second_run_does_not_re_trigger(monkeypatch, _patch_mongo_client):
    from app.config.settings import Settings

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    due = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    PersonalItemRepository(db).upsert_by_key(
        {"id": "PSN-001"},
        {"id": "PSN-001", "type": "reminder", "description": "check in", "date_or_deadline": due, "status": "open"},
    )

    reminders_module.main(["--once"])
    reminders_module.main(["--once"])

    assert db.personal_items.find_one({"id": "PSN-001"})["status"] == "reminded"


def test_reminders_cli_default_mode_calls_run_forever_with_configured_interval_without_looping(
    monkeypatch, _patch_mongo_client
):
    monkeypatch.setenv("REMINDER_INTERVAL_MINUTES", "9")
    captured = {}

    def fake_run_forever(self, interval_seconds):
        captured["interval_seconds"] = interval_seconds

    monkeypatch.setattr(reminders_module.ReminderScheduler, "run_forever", fake_run_forever)

    exit_code = reminders_module.main([])

    assert exit_code == 0
    assert captured["interval_seconds"] == 9 * 60
