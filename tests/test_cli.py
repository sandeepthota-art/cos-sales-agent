import mongomock
import pytest

import main as main_module
from app.config.settings import Settings


@pytest.fixture(autouse=True)
def _patch_mongo_client(monkeypatch):
    fake_client = mongomock.MongoClient()
    monkeypatch.setattr(main_module, "get_client", lambda uri: fake_client)
    yield fake_client


def test_healthcheck_reports_ok_with_default_mock_demo_settings(monkeypatch, capsys):
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    exit_code = main_module.main(["--healthcheck"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "MongoDB connected" in output
    assert "System ready" in output


def test_demo_mode_populates_database(monkeypatch, capsys, _patch_mongo_client):
    # This is the regression net for Fix 1 (Step-4 LLM verification threshold wrongly
    # merging distinct short facts) and Fix 4 (drafting replies addressed to our own sales
    # rep) -- it asserts concrete final-state facts about the actual demo run, not just
    # that some output contains the word "processed".
    monkeypatch.setenv("EMAIL_PROVIDER", "demo")
    monkeypatch.setenv("CALENDAR_PROVIDER", "mock")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("EMAIL_LIMIT", "9")
    exit_code = main_module.main(["--mode=demo"])
    assert exit_code == 0
    output = capsys.readouterr().out
    assert "processed" in output.lower()

    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]

    assert db.processing_runs.count_documents({}) == 1
    run_doc = db.processing_runs.find_one({})
    assert run_doc["completed"] == 9
    assert run_doc["failed"] == 0

    assert db.threads.count_documents({}) == 1
    thread_id = db.threads.find_one({})["thread_id"]
    assert db.context_snapshots.count_documents({"thread_id": thread_id}) == 9

    # Fix 1 regression: "pricing request"/"proposal request" and "demo request"/
    # "procurement request" must remain distinct knowledge items, not wrongly merged.
    buying_signal_fact_keys = {
        doc["fact_key"]
        for doc in db.knowledge_items.find({"thread_id": thread_id, "predicate": "showed_buying_signal"})
    }
    assert {"pricing_request", "proposal_request", "demo_request", "procurement_request"} <= buying_signal_fact_keys

    # The demo's seat-count-change narrative beat: 100 seats -> 150 seats.
    seat_count_item = db.knowledge_items.find_one({"thread_id": thread_id, "fact_key": "seat_count"})
    assert seat_count_item is not None
    history_values = [h["value"] for h in seat_count_item["history"]]
    assert any("100" in v for v in history_values)
    assert any("150" in v for v in history_values)
    assert "150" in seat_count_item["current_value"]

    assert db.calendar_actions.count_documents({}) >= 1

    # Fix 4 regression: no reply draft may be a reply to our own sales rep's own outbound
    # email.
    for draft in db.reply_drafts.find({}):
        source_email = db.emails.find_one({"message_id": draft["source_email_id"]})
        assert source_email is not None
        assert source_email["from"]["email"].lower() != settings.agent_email.lower()


def test_reset_demo_requires_simulation_mode(monkeypatch):
    monkeypatch.setenv("SIMULATION_MODE", "false")
    monkeypatch.setenv("APP_ENV", "production")
    exit_code = main_module.main(["--reset-demo"])
    assert exit_code == 1


def test_reset_demo_clears_ingested_files(monkeypatch, _patch_mongo_client):
    # ingested_files must be wiped alongside every other collection -- otherwise a
    # stale fingerprint record would wrongly make FolderSource skip re-ingesting a
    # file whose actual emails were just deleted by this same reset.
    monkeypatch.setenv("SIMULATION_MODE", "true")
    settings = Settings()
    db = _patch_mongo_client[settings.mongodb_database]
    db.ingested_files.insert_one({"filename": "a.json", "all_completed": True})

    exit_code = main_module.main(["--reset-demo"])

    assert exit_code == 0
    assert db.ingested_files.count_documents({}) == 0
