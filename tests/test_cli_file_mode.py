import json

import mongomock
import pytest

import main as main_module
from app.config.settings import Settings


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(main_module, "get_client", lambda uri: fake_client)
    yield fake_client


def _write_export(tmp_path, messages):
    path = tmp_path / "export.json"
    path.write_text(
        json.dumps({"label": "BD", "labelId": "L1", "threadCount": 1, "messageCount": len(messages), "messages": messages}),
        encoding="utf-8",
    )
    return str(path)


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
        "snippet": "Hi Ashok...",
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami. Can we set up a call to discuss pricing?",
    }
    base.update(overrides)
    return base


def test_file_mode_requires_input(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    exit_code = main_module.main(["--mode=file"])

    assert exit_code == 1


def test_file_mode_ingests_export_through_existing_pipeline(monkeypatch, tmp_path, capsys, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = _write_export(tmp_path, [_message()])

    exit_code = main_module.main(["--mode=file", "--input", path])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "File ingestion complete." in output
    assert "Total messages found in file: 1" in output
    assert "Messages successfully completed: 1" in output

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({"message_id": "18f348ce69f386be"}) == 1
    assert db.threads.count_documents({"thread_id": "18f348ce69f386be"}) == 1


def test_file_mode_ignores_email_limit_and_processes_whole_export(monkeypatch, tmp_path, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMAIL_LIMIT", "1")  # deliberately smaller than the export
    messages = [_message(id=f"msg_{i}", threadId=f"thread_{i}") for i in range(3)]
    path = _write_export(tmp_path, messages)

    exit_code = main_module.main(["--mode=file", "--input", path])

    assert exit_code == 0
    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({}) == 3


def test_file_mode_is_idempotent_across_repeated_runs(monkeypatch, tmp_path, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = _write_export(tmp_path, [_message()])

    first_exit = main_module.main(["--mode=file", "--input", path])
    assert first_exit == 0

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    counts_after_first_run = {
        name: db[name].count_documents({})
        for name in ["emails", "threads", "context_snapshots", "knowledge_items", "reply_drafts", "calendar_actions"]
    }

    second_exit = main_module.main(["--mode=file", "--input", path])
    assert second_exit == 0

    counts_after_second_run = {
        name: db[name].count_documents({})
        for name in ["emails", "threads", "context_snapshots", "knowledge_items", "reply_drafts", "calendar_actions"]
    }
    assert counts_after_second_run == counts_after_first_run

    second_run_output_run_doc = list(db.processing_runs.find({}))[-1]
    assert second_run_output_run_doc["skipped"] == 1
    assert second_run_output_run_doc["completed"] == 0


def test_file_mode_reports_error_for_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    exit_code = main_module.main(["--mode=file", "--input", str(tmp_path / "does_not_exist.json")])

    assert exit_code == 1


def test_file_mode_reports_error_for_invalid_json_structure(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"not_messages": []}), encoding="utf-8")

    exit_code = main_module.main(["--mode=file", "--input", str(path)])

    assert exit_code == 1
