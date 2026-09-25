import json

import mongomock
import pytest

import app.scheduler as scheduler_module


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(scheduler_module, "get_client", lambda uri: fake_client)
    yield fake_client


@pytest.fixture(autouse=True)
def _mock_providers(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")


def _message(**overrides):
    base = {
        "id": "18f348ce69f386be",
        "threadId": "18f348ce69f386be",
        "subject": "sales/capabilities materials",
        "sender": "john.t@614group.com",
        "toRecipients": ["ashok@databeat.io"],
        "ccRecipients": None,
        "date": "2024-05-01T14:26:28Z",
        "labelIds": ["IMPORTANT", "INBOX"],
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


def _write_export(folder, name, messages):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text(json.dumps({"messages": messages}), encoding="utf-8")
    return path


def test_scheduler_cli_accepts_once_flag():
    args = scheduler_module.build_arg_parser().parse_args(["--once"])
    assert args.once is True


def test_scheduler_cli_once_runs_a_single_poll_and_exits(monkeypatch, tmp_path, _patch_mongo_client):
    folder = tmp_path / "inbox"
    _write_export(folder, "a.json", [_message(id="m1", threadId="t1")])
    monkeypatch.setenv("INGESTION_FOLDER", str(folder))

    exit_code = scheduler_module.main(["--once"])

    assert exit_code == 0
    from app.config.settings import Settings

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({"message_id": "m1"}) == 1
    assert db.ingested_files.count_documents({"filename": "a.json"}) == 1


def test_scheduler_cli_detects_a_newly_added_file_across_two_once_runs(monkeypatch, tmp_path, _patch_mongo_client):
    folder = tmp_path / "inbox"
    _write_export(folder, "a.json", [_message(id="m1", threadId="t1")])
    monkeypatch.setenv("INGESTION_FOLDER", str(folder))

    scheduler_module.main(["--once"])

    _write_export(folder, "b.json", [_message(id="m2", threadId="t2")])
    scheduler_module.main(["--once"])

    from app.config.settings import Settings

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({}) == 2
    assert db.emails.count_documents({"message_id": "m2"}) == 1


def test_scheduler_cli_default_mode_calls_run_forever_with_configured_interval_without_looping(
    monkeypatch, tmp_path, _patch_mongo_client
):
    monkeypatch.setenv("INGESTION_FOLDER", str(tmp_path / "inbox"))
    monkeypatch.setenv("INGESTION_INTERVAL_MINUTES", "7")
    captured = {}

    def fake_run_forever(self, interval_seconds):
        captured["interval_seconds"] = interval_seconds

    monkeypatch.setattr(scheduler_module.Scheduler, "run_forever", fake_run_forever)

    exit_code = scheduler_module.main([])

    assert exit_code == 0
    assert captured["interval_seconds"] == 7 * 60


def test_scheduler_cli_reads_ingestion_folder_from_settings(monkeypatch, tmp_path, _patch_mongo_client):
    configured_folder = tmp_path / "configured_inbox"
    other_folder = tmp_path / "unrelated_folder"
    _write_export(other_folder, "a.json", [_message(id="m1", threadId="t1")])
    monkeypatch.setenv("INGESTION_FOLDER", str(configured_folder))

    exit_code = scheduler_module.main(["--once"])

    assert exit_code == 0
    from app.config.settings import Settings

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    # The file sitting in the unconfigured folder must never be ingested -- proves
    # the scheduler actually reads its watched folder from settings, not a hardcoded
    # or coincidental path.
    assert db.emails.count_documents({}) == 0
    assert configured_folder.is_dir()
