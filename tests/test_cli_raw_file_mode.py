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
        "plaintextBody": "Hi Ashok, good to see you at Possible in Miami.",
    }
    base.update(overrides)
    return base


def test_cli_accepts_raw_file_mode_argument():
    args = main_module.build_arg_parser().parse_args(["--mode=raw-file", "--input", "x.json"])
    assert args.mode == "raw-file"


def test_raw_file_mode_requires_input(monkeypatch):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")

    exit_code = main_module.main(["--mode=raw-file"])

    assert exit_code == 1


def test_raw_file_mode_ingests_without_any_llm_configuration(monkeypatch, tmp_path, capsys, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    path = _write_export(tmp_path, [_message()])

    exit_code = main_module.main(["--mode=raw-file", "--input", path])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "## RAW INGESTION COMPLETE" in output
    assert "Inserted:             1" in output
    assert "Claude API calls:     0" in output
    assert "LLM calls:            0" in output

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({"message_id": "18f348ce69f386be"}) == 1
    assert db.threads.count_documents({"thread_id": "18f348ce69f386be"}) == 1


def test_raw_file_mode_never_writes_knowledge_or_entity_collections(monkeypatch, tmp_path, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = _write_export(tmp_path, [_message()])

    main_module.main(["--mode=raw-file", "--input", path])

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    for collection in [
        "knowledge_items", "people", "projects", "commitments", "follow_ups",
        "meetings", "personal_items", "reply_drafts", "calendar_actions",
    ]:
        assert db[collection].count_documents({}) == 0


def test_raw_file_mode_is_idempotent_end_to_end(monkeypatch, tmp_path, capsys, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = _write_export(tmp_path, [_message()])

    main_module.main(["--mode=raw-file", "--input", path])
    capsys.readouterr()  # discard first run's output
    exit_code = main_module.main(["--mode=raw-file", "--input", path])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Inserted:             0" in output
    assert "Already existed:      1" in output

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    assert db.emails.count_documents({}) == 1
    assert db.threads.count_documents({}) == 1


# --- Existing demo/file modes remain unaffected by adding raw-file mode ---


def test_demo_mode_still_works_after_adding_raw_file_mode(monkeypatch, capsys, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMAIL_LIMIT", "3")

    exit_code = main_module.main(["--mode=demo"])

    assert exit_code == 0
    assert "processed" in capsys.readouterr().out.lower()


def test_file_mode_still_works_after_adding_raw_file_mode(monkeypatch, tmp_path, capsys, _patch_mongo_client):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    path = _write_export(tmp_path, [_message()])

    exit_code = main_module.main(["--mode=file", "--input", path])

    assert exit_code == 0
    assert "File ingestion complete." in capsys.readouterr().out
